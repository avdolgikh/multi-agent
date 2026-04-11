from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, TYPE_CHECKING, Literal, runtime_checkable

import httpx
from httpx import AsyncClient
import openai  # noqa: F401 — exposed on module for test monkeypatching
from openai import AsyncOpenAI
from opentelemetry import trace
from pydantic import BaseModel, Field

from core.resilience import CircuitBreaker
from core.tracing import extract_context

if TYPE_CHECKING:  # pragma: no cover - import-time guard
    from core.messaging import Message

__all__ = [
    "AgentTask",
    "AgentResult",
    "LLMResponse",
    "Tool",
    "BaseAgent",
    "WebSearchTool",
    "FileReadTool",
    "FileWriteTool",
    "TokenUsage",
    "ToolCall",
]


DEFAULT_BASE_URLS = {
    "ollama": "http://localhost:11434/v1",
    "openai": "https://api.openai.com/v1",
}


class AgentTask(BaseModel):
    task_id: str
    input_data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    trace_context: dict[str, Any] | None = None


class AgentResult(BaseModel):
    task_id: str
    agent_id: str
    output_data: dict[str, Any] = Field(default_factory=dict)
    status: Literal["success", "failure", "partial"]
    error: str | None = None
    duration_ms: float = 0.0
    trace_context: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ToolCall(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    content: str
    tool_calls: list[ToolCall] | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str
    provider: str


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]

    async def execute(self, params: dict[str, Any]) -> dict[str, Any]: ...


class BaseAgent(ABC):
    def __init__(
        self,
        *,
        agent_id: str,
        name: str,
        model: str,
        provider: str = "ollama",
        tools: Sequence[Tool] | None = None,
        system_prompt: str = "",
        base_url: str | None = None,
    ) -> None:
        provider_normalized = provider.lower()
        if provider_normalized not in DEFAULT_BASE_URLS:
            provider_normalized = "openai"
        self.agent_id = agent_id
        self.name = name
        self.model = model
        self.provider = provider_normalized
        self.tools = list(tools or [])
        self.system_prompt = system_prompt
        self.base_url = base_url or DEFAULT_BASE_URLS[self.provider]
        self._circuit_breaker = CircuitBreaker()
        self._tracer = trace.get_tracer("core.agents")

    @abstractmethod
    async def execute(self, task: AgentTask) -> AgentResult:
        raise NotImplementedError

    async def call_llm(self, messages: Sequence["Message"]) -> LLMResponse:
        chat_messages = self._prepare_chat_messages(messages)
        parent_context = self._resolve_trace_context(messages)
        span_kwargs: dict[str, Any] = {}
        if parent_context is not None:
            span_kwargs["context"] = parent_context
        with self._tracer.start_as_current_span(f"{self.name}.llm", **span_kwargs) as span:
            span.set_attribute("agent.id", self.agent_id)
            span.set_attribute("agent.model", self.model)
            span.set_attribute("agent.provider", self.provider)
            raw_response = await self._circuit_breaker.call(self._perform_chat, chat_messages)
        return self._build_llm_response(raw_response)

    async def _perform_chat(self, chat_messages: list[dict[str, Any]]) -> Any:
        client = self._create_client()
        response = await client.chat.completions.create(
            model=self.model,
            messages=chat_messages,
        )
        return response

    def _create_client(self) -> AsyncOpenAI:
        api_key = self._resolve_api_key()
        return AsyncOpenAI(api_key=api_key, base_url=self.base_url)

    def _resolve_api_key(self) -> str:
        if self.provider == "ollama":
            return "ollama"
        return os.getenv("OPENAI_API_KEY", "openai")

    def _prepare_chat_messages(self, messages: Sequence["Message"]) -> list[dict[str, Any]]:
        chat_messages: list[dict[str, Any]] = []
        if self.system_prompt:
            chat_messages.append({"role": "system", "content": self.system_prompt})
        for message in messages:
            payload = getattr(message, "payload", message)
            role = "user"
            content: Any = payload
            if isinstance(payload, Mapping):
                role = str(payload.get("role", "user"))
                content = payload.get("content", payload)
            chat_messages.append({"role": role, "content": str(content)})
        if not chat_messages:
            chat_messages.append({"role": "system", "content": self.system_prompt or ""})
        return chat_messages

    def _resolve_trace_context(self, messages: Sequence["Message"]):
        for message in reversed(messages):
            trace_ctx = getattr(message, "trace_context", None)
            if trace_ctx:
                return extract_context(trace_ctx)
        return None

    def _build_llm_response(self, raw_response: Any) -> LLMResponse:
        content = ""
        tool_calls: list[ToolCall] | None = None
        if getattr(raw_response, "choices", None):
            first_choice = raw_response.choices[0]
            message = getattr(first_choice, "message", None)
            if message is not None:
                content = getattr(message, "content", "") or ""
                parsed_tool_calls = self._parse_tool_calls(getattr(message, "tool_calls", None))
                if parsed_tool_calls:
                    tool_calls = parsed_tool_calls
        usage_model = self._build_usage(getattr(raw_response, "usage", None))
        response_model = getattr(raw_response, "model", self.model)
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage_model,
            model=response_model,
            provider=self.provider,
        )

    def _parse_tool_calls(self, tool_calls: Any) -> list[ToolCall] | None:
        if not tool_calls:
            return None
        parsed: list[ToolCall] = []
        for call in tool_calls:
            function = getattr(call, "function", None)
            if isinstance(call, dict):
                function = call.get("function", {})
            if not function:
                continue
            arguments_raw = (
                function.get("arguments")
                if isinstance(function, dict)
                else getattr(function, "arguments", "{}")
            )
            arguments = self._safe_parse_args(arguments_raw)
            name = (
                function.get("name")
                if isinstance(function, dict)
                else getattr(function, "name", "")
            )
            parsed.append(ToolCall(tool_name=name or "tool", arguments=arguments))
        return parsed or None

    def _safe_parse_args(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw}
        return {}

    def _build_usage(self, usage_obj: Any) -> TokenUsage:
        if usage_obj is None:
            return TokenUsage()
        data = (
            usage_obj.__dict__
            if hasattr(usage_obj, "__dict__")
            else dict(getattr(usage_obj, "_data", {}))
        )
        prompt = int(data.get("prompt_tokens", 0))
        completion = int(data.get("completion_tokens", 0))
        total = int(data.get("total_tokens", prompt + completion))
        return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


class WebSearchTool:
    name = "web_search"
    description = "Perform a lightweight web search via HTTP GET"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "limit": {"type": "integer", "description": "Maximum results", "minimum": 1},
        },
        "required": ["query"],
    }

    def __init__(
        self,
        *,
        endpoint: str = "https://ddg-webapp-prod-frontend-ajax.duckduckgo.com/search",
        timeout: float = 10.0,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout

    async def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        query_key, query_value = self._extract_query(params)
        limit = params.get("limit")

        request_params: dict[str, Any] = {query_key: query_value}
        if query_key != "q":
            request_params["q"] = query_value
        if limit is not None:
            request_params["max_results"] = limit

        async with AsyncClient(timeout=self.timeout) as client:
            response = await client.get(self.endpoint, params=request_params)
            data = self._coerce_response(response)
            return {
                "query": query_value,
                "results": data,
                "status_code": response.status_code,
            }

    def _coerce_response(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return {"text": response.text}

    def _extract_query(self, params: Mapping[str, Any]) -> tuple[str, str]:
        query_candidates = ("query", "term", "keywords", "q")
        for key in query_candidates:
            value = params.get(key)
            if value:
                return key, str(value)
        raise ValueError("WebSearchTool requires a query parameter")


class FileReadTool:
    name = "file_read"
    description = "Read the contents of a local file"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or relative file path"},
            "encoding": {"type": "string", "description": "File encoding", "default": "utf-8"},
        },
        "required": ["path"],
    }

    async def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        path_value = params.get("path")
        if not path_value:
            raise ValueError("FileReadTool requires a path parameter")
        encoding = params.get("encoding", "utf-8")
        path = Path(path_value)
        text = await asyncio.to_thread(path.read_text, encoding=encoding)
        return {"path": str(path), "content": text}


class FileWriteTool:
    name = "file_write"
    description = "Write content to a local file"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Destination file path"},
            "content": {"type": "string", "description": "Content to write"},
            "encoding": {"type": "string", "description": "File encoding", "default": "utf-8"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        path_value = params.get("path")
        content = params.get("content")
        if not path_value or content is None:
            raise ValueError("FileWriteTool requires path and content parameters")
        encoding = params.get("encoding", "utf-8")
        path = Path(path_value)
        await asyncio.to_thread(path.write_text, str(content), encoding=encoding)
        return {"path": str(path), "bytes_written": len(str(content))}
