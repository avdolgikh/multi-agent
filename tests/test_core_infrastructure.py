import asyncio
import inspect
import random
from datetime import datetime
from typing import Any, Callable
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.trace.export import InMemorySpanExporter, SimpleSpanProcessor
from opentelemetry.trace.status import StatusCode

import core.agents as core_agents
from core.agents import (
    AgentResult,
    AgentTask,
    BaseAgent,
    FileReadTool,
    FileWriteTool,
    LLMResponse,
    TokenUsage,
    WebSearchTool,
)
from core.messaging import InMemoryBus, Message
from core.resilience import CircuitBreaker, CircuitOpenError, DeadLetterQueue, RetryPolicy
from core.state import Event, InMemoryEventStore, SnapshotStore
from core.tracing import TracingManager, traced, extract_context, inject_context


def make_message(topic: str, payload: dict, *, trace_context: dict | None = None) -> Message:
    """Build a core.messaging.Message with minimal required metadata."""
    return Message(
        message_id=str(uuid4()),
        topic=topic,
        payload=payload,
        timestamp=datetime.utcnow(),
        trace_context=trace_context or {},
        source_agent="test-agent",
    )


def _find_property_name(properties: dict[str, dict], keywords: list[str]) -> str | None:
    for name in properties:
        if any(keyword in name.lower() for keyword in keywords):
            return name
    return None


def _contains_value(payload: dict[str, Any], substring: str) -> bool:
    return any(substring in str(value) for value in payload.values())


def _extract_trace_context_from_message(recorded_message: Any) -> dict | None:
    if isinstance(recorded_message, dict):
        return recorded_message.get("trace_context")
    return getattr(recorded_message, "trace_context", None)


class DummyAgent(BaseAgent):
    """Simple agent for exercising BaseAgent helpers in tests."""

    async def execute(self, task: AgentTask) -> AgentResult:
        message = make_message(
            topic="dummy.llm",
            payload={"role": "user", "content": task.input_data.get("prompt", "")},
            trace_context=task.trace_context or {},
        )
        llm_response = await self.call_llm([message])
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data={"reply": llm_response.content},
            status="success",
            error=None,
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )


@pytest.mark.asyncio
async def test_base_agent_execute_returns_agent_result(monkeypatch):
    agent = DummyAgent(
        agent_id="agent-1",
        name="Dummy",
        model="qwen3:8b",
        provider="ollama",
        tools=[],
        system_prompt="system",
        base_url=None,
    )
    usage = TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    response = LLMResponse(
        content="pong",
        tool_calls=[],
        usage=usage,
        model=agent.model,
        provider=agent.provider,
    )
    monkeypatch.setattr(BaseAgent, "call_llm", AsyncMock(return_value=response))
    task = AgentTask(
        task_id="task-1", input_data={"prompt": "hello"}, metadata={}, trace_context={}
    )
    result = await agent.execute(task)

    assert result.task_id == "task-1"
    assert result.agent_id == agent.agent_id
    assert result.output_data["reply"] == "pong"
    assert result.status == "success"
    assert result.trace_context == task.trace_context


@pytest.mark.asyncio
async def test_base_agent_call_llm_routes_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-key")
    client_configurations: list[dict[str, Any]] = []

    class FakeChatResponse:
        def __init__(self, content: str, model: str, usage: dict[str, int]) -> None:
            self.choices = [
                SimpleNamespace(message=SimpleNamespace(role="assistant", content=content))
            ]
            self.usage = SimpleNamespace(**usage)
            self.model = model

    class FakeClient:
        def __init__(self, **config: Any):
            self.config = dict(config)
            client_configurations.append(self.config)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        async def _create(self, **kwargs: Any) -> FakeChatResponse:
            self.config["chat_call"] = kwargs
            content = "ok"
            messages = kwargs.get("messages")
            if isinstance(messages, list) and messages:
                first = messages[0]
                if isinstance(first, dict):
                    content = first.get("content", content)
                else:
                    content = getattr(first, "content", content)
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            return FakeChatResponse(
                content=content,
                model=kwargs.get("model", "stub-model"),
                usage=usage,
            )

    monkeypatch.setattr(core_agents, "AsyncOpenAI", FakeClient, raising=False)
    if hasattr(core_agents, "openai"):
        monkeypatch.setattr(core_agents.openai, "AsyncOpenAI", FakeClient)

    expected_defaults = {
        "ollama": "http://localhost:11434/v1",
        "openai": "https://api.openai.com/v1",
    }
    ollama_agent = DummyAgent(
        agent_id="agent-ollama",
        name="Ollama",
        model="qwen3:8b",
        provider="ollama",
        tools=[],
        system_prompt="system",
        base_url=None,
    )
    openai_agent = DummyAgent(
        agent_id="agent-openai",
        name="OpenAI",
        model="gpt-4o-mini",
        provider="openai",
        tools=[],
        system_prompt="system",
        base_url=None,
    )

    for agent in (ollama_agent, openai_agent):
        message = make_message(topic="core.llm", payload={"role": "user", "content": "ping"})
        response = await agent.call_llm([message])
        assert response.provider == agent.provider
        assert response.model == agent.model

    assert len(client_configurations) == 2
    assert client_configurations[0]["base_url"] == expected_defaults["ollama"]
    assert client_configurations[0]["api_key"] == "ollama"
    assert client_configurations[1]["base_url"] == expected_defaults["openai"]
    assert client_configurations[1]["api_key"] == "openai-test-key"
    assert client_configurations[0]["chat_call"]["model"] == ollama_agent.model
    assert client_configurations[1]["chat_call"]["model"] == openai_agent.model


@pytest.mark.asyncio
async def test_base_agent_call_llm_respects_circuit_breaker(monkeypatch):
    client_calls: list[dict[str, Any]] = []

    class FakeChatResponse:
        def __init__(self, content: str, model: str, usage: dict[str, int]) -> None:
            self.choices = [
                SimpleNamespace(message=SimpleNamespace(role="assistant", content=content))
            ]
            self.usage = SimpleNamespace(**usage)
            self.model = model

    class FailOnceClient:
        call_count = 0

        def __init__(self, **config: Any) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
            client_calls.append({"config": config})

        async def _create(self, **kwargs: Any) -> FakeChatResponse:
            FailOnceClient.call_count += 1
            client_calls.append({"call": kwargs})
            if FailOnceClient.call_count <= 2:
                raise RuntimeError("llm failure")
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            return FakeChatResponse(
                content="ok",
                model=kwargs.get("model", "stub-model"),
                usage=usage,
            )

    breaker_instances: list["TrackingCircuitBreaker"] = []

    class TrackingCircuitBreaker:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.failure_count = 0
            breaker_instances.append(self)

        async def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            if self.failure_count >= 2:
                raise CircuitOpenError("circuit is open")
            try:
                return await func(*args, **kwargs)
            except Exception:
                self.failure_count += 1
                raise

    monkeypatch.setattr(core_agents, "AsyncOpenAI", FailOnceClient, raising=False)
    monkeypatch.setattr(core_agents, "CircuitBreaker", TrackingCircuitBreaker, raising=False)
    if hasattr(core_agents, "openai"):
        monkeypatch.setattr(core_agents.openai, "AsyncOpenAI", FailOnceClient)

    agent = DummyAgent(
        agent_id="breaker-agent",
        name="Breaker",
        model="qwen3:8b",
        provider="ollama",
        tools=[],
        system_prompt="system",
        base_url=None,
    )
    message = make_message(topic="core.llm", payload={"role": "user", "content": "ping"})

    with pytest.raises(RuntimeError):
        await agent.call_llm([message])
    with pytest.raises(RuntimeError):
        await agent.call_llm([message])
    assert FailOnceClient.call_count == 2
    assert breaker_instances
    assert breaker_instances[0].failure_count == 2
    with pytest.raises(CircuitOpenError):
        await agent.call_llm([message])


@pytest.mark.asyncio
async def test_base_agent_call_llm_propagates_trace_context(monkeypatch):
    provider = TracingManager.setup("core-agent-tracing", endpoint=None)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("core.agents.tracing-test")

    class FakeChatResponse:
        def __init__(self, content: str, model: str, usage: dict[str, int]) -> None:
            self.choices = [
                SimpleNamespace(message=SimpleNamespace(role="assistant", content=content))
            ]
            self.usage = SimpleNamespace(**usage)
            self.model = model

    class RecordingClient:
        def __init__(self, **config: Any) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        async def _create(self, **kwargs: Any) -> FakeChatResponse:
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            return FakeChatResponse(
                content="ok",
                model=kwargs.get("model", "stub-model"),
                usage=usage,
            )

    monkeypatch.setattr(core_agents, "AsyncOpenAI", RecordingClient, raising=False)
    if hasattr(core_agents, "openai"):
        monkeypatch.setattr(core_agents.openai, "AsyncOpenAI", RecordingClient)

    agent = DummyAgent(
        agent_id="trace-agent",
        name="Trace",
        model="qwen3:8b",
        provider="ollama",
        tools=[],
        system_prompt="system",
        base_url=None,
    )

    with tracer.start_as_current_span("parent") as parent_span:
        parent_context = inject_context()
        message = make_message(
            topic="core.tracing",
            payload={"role": "user", "content": "trace"},
            trace_context=parent_context,
        )
        await agent.call_llm([message])

    parent_trace_id = parent_span.get_span_context().trace_id
    provider.force_flush()
    spans = exporter.get_finished_spans()

    # call_llm should create a child span under the active parent context
    child_spans = [
        span
        for span in spans
        if span.parent is not None
        and span.parent.trace_id == parent_trace_id
        and span.name != "parent"
    ]
    assert child_spans, "call_llm should produce at least one child span linked to the parent trace"


def test_builtin_tools_expose_metadata_and_async_execute():
    tools = [WebSearchTool(), FileReadTool(), FileWriteTool()]
    for tool in tools:
        assert isinstance(tool.name, str)
        assert isinstance(tool.description, str)
        assert isinstance(tool.parameters, dict)
        assert inspect.iscoroutinefunction(tool.execute)


@pytest.mark.asyncio
async def test_web_search_tool_calls_httpx(monkeypatch):
    request_calls: list[dict[str, Any]] = []

    class FakeResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self._text = "mock search results"

        @property
        def text(self) -> str:
            return self._text

        def json(self) -> dict[str, Any]:
            return {"results": ["mock"]}

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            request_calls.append({"init": {"args": args, "kwargs": kwargs}})

        async def get(self, url: str, params: dict[str, Any] | None = None) -> FakeResponse:
            request_calls.append({"url": url, "params": params})
            return FakeResponse()

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type, exc, exc_tb) -> None:  # type: ignore[override]
            return None

    monkeypatch.setattr(core_agents, "AsyncClient", FakeClient, raising=False)
    httpx_module = getattr(core_agents, "httpx", None)
    if httpx_module is not None:
        monkeypatch.setattr(httpx_module, "AsyncClient", FakeClient)

    tool = WebSearchTool()
    query_key = _find_property_name(
        tool.parameters.get("properties", {}), ["query", "term", "keywords"]
    )
    assert query_key is not None, "WebSearchTool must declare a query parameter"
    params = {query_key: "pytest search"}
    result = await tool.execute(params)
    assert isinstance(result, dict)
    search_calls = [call for call in request_calls if "params" in call]
    assert search_calls, "WebSearchTool should invoke httpx.AsyncClient.get"
    assert search_calls[-1]["params"].get(query_key) == "pytest search"


@pytest.mark.asyncio
async def test_file_read_and_write_tools_round_trip(tmp_path):
    write_tool = FileWriteTool()
    write_properties = write_tool.parameters.get("properties", {})
    write_path_key = _find_property_name(write_properties, ["path", "file"])
    write_content_key = _find_property_name(write_properties, ["content", "text", "body"])
    assert write_path_key, "FileWriteTool must expose a path-like parameter"
    assert write_content_key, "FileWriteTool must expose a content-like parameter"

    output_path = tmp_path / "output.txt"
    content_value = "hello from tests"
    write_params = {
        write_path_key: str(output_path),
        write_content_key: content_value,
    }
    write_result = await write_tool.execute(write_params)
    assert output_path.read_text() == content_value
    assert isinstance(write_result, dict)

    read_tool = FileReadTool()
    read_properties = read_tool.parameters.get("properties", {})
    read_path_key = _find_property_name(read_properties, ["path", "file"])
    assert read_path_key, "FileReadTool must expose a path-like parameter"

    read_result = await read_tool.execute({read_path_key: str(output_path)})
    assert isinstance(read_result, dict)
    assert _contains_value(read_result, content_value)


@pytest.mark.asyncio
async def test_inmemory_bus_pubsub_delivers_to_all():
    bus = InMemoryBus()
    received: dict[str, list[int]] = {"h1": [], "h2": []}

    async def handler(name: str, message: Message) -> None:
        received[name].append(message.payload["sequence"])

    sub1 = await bus.subscribe("core.topic", lambda message: handler("h1", message))
    sub2 = await bus.subscribe("core.topic", lambda message: handler("h2", message))

    for value in range(3):
        await bus.publish(
            "core.topic",
            make_message(topic="core.topic", payload={"sequence": value}),
        )
        await asyncio.sleep(0)  # yield control so handlers run

    assert received["h1"] == [0, 1, 2]
    assert received["h2"] == [0, 1, 2]

    await bus.unsubscribe(sub1)
    await bus.unsubscribe(sub2)


@pytest.mark.asyncio
async def test_inmemory_bus_request_reply_and_timeout():
    bus = InMemoryBus()

    async def responder(message: Message) -> Message:
        return make_message(
            topic=message.topic,
            payload={"reply": message.payload.get("ping")},
            trace_context=message.trace_context,
        )

    subscription = await bus.subscribe("req.topic", responder)
    request_message = make_message(topic="req.topic", payload={"ping": "pong"})
    reply = await bus.request("req.topic", request_message, timeout=1.0)
    assert reply.payload["reply"] == "pong"

    await bus.unsubscribe(subscription)
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await bus.request("req.topic", request_message, timeout=0.01)


@pytest.mark.asyncio
async def test_trace_context_propagates_with_message():
    provider = TracingManager.setup("core-tests", endpoint=None)
    tracer = provider.get_tracer("core.tracing.test")

    with tracer.start_as_current_span("parent") as parent_span:
        context_dict = inject_context()

    message = make_message(
        topic="trace.topic",
        payload={"data": "value"},
        trace_context=context_dict,
    )

    child_context = extract_context(message.trace_context)
    with tracer.start_as_current_span("child", context=child_context) as child_span:
        assert child_span.get_span_context().trace_id == parent_span.get_span_context().trace_id


@pytest.mark.asyncio
async def test_trace_context_propagates_over_inmemory_bus():
    provider = TracingManager.setup("core-bus-tracing", endpoint=None)
    tracer = provider.get_tracer("core.messaging.tracing-test")
    bus = InMemoryBus()
    received: asyncio.Queue[str] = asyncio.Queue()

    async def subscriber(message: Message) -> None:
        child_context = extract_context(message.trace_context)
        with tracer.start_as_current_span("subscriber", context=child_context) as child_span:
            await received.put(child_span.get_span_context().trace_id)

    subscription = await bus.subscribe("trace.bus.topic", subscriber)
    with tracer.start_as_current_span("publisher") as parent_span:
        await bus.publish(
            "trace.bus.topic",
            make_message(
                topic="trace.bus.topic",
                payload={"value": "bus"},
                trace_context=inject_context(),
            ),
        )
    parent_trace_id = parent_span.get_span_context().trace_id
    assert await asyncio.wait_for(received.get(), timeout=1.0) == parent_trace_id
    await bus.unsubscribe(subscription)


@pytest.mark.asyncio
async def test_traced_decorator_creates_child_spans_and_records_errors():
    provider = TracingManager.setup("core-tracing-tests", endpoint=None)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("core.tracing.decorator-test")

    @traced
    async def instrumented(value: int) -> int:
        if value < 0:
            raise RuntimeError("boom")
        return value * 2

    parent_context = None
    with tracer.start_as_current_span("parent") as parent_span:
        parent_context = parent_span.get_span_context()
        with pytest.raises(RuntimeError):
            await instrumented(-1)
        assert await instrumented(5) == 10

    assert parent_context is not None
    provider.force_flush()
    spans = exporter.get_finished_spans()

    child_spans = [
        span
        for span in spans
        if span.parent is not None
        and span.parent.trace_id == parent_context.trace_id
        and span.name != "parent"
    ]
    assert len(child_spans) >= 2

    error_span = next(
        (span for span in child_spans if span.status.status_code == StatusCode.ERROR),
        None,
    )
    assert error_span is not None
    assert any(event.name == "exception" for event in error_span.events)

    assert any(span.status.status_code != StatusCode.ERROR for span in child_spans)


@pytest.mark.asyncio
async def test_event_store_append_and_replay():
    store = InMemoryEventStore()
    stream = "core-stream"

    for value in range(5):
        event = Event(
            event_id=str(uuid4()),
            stream=stream,
            event_type="update",
            data={"value": value, f"flag_{value}": True},
            timestamp=datetime.utcnow(),
            sequence=value,
            trace_context={},
        )
        await store.append(stream, event)

    read_events = await store.read(stream)
    expected_state: dict[str, Any] = {}
    for event in read_events:
        expected_state.update(event.data)

    replay_state = await store.replay(stream)
    assert replay_state == expected_state
    assert len(read_events) == 5


@pytest.mark.asyncio
async def test_snapshot_store_round_trip():
    store = SnapshotStore()
    workflow_id = "workflow-1"
    steps = ["ingest", "process"]
    states = [{"value": 1}, {"value": 2}]
    snapshot_ids: list[str] = []

    for step, state in zip(steps, states):
        snapshot_id = await store.save(workflow_id, step, state)
        snapshot_ids.append(snapshot_id)
        loaded = await store.load(snapshot_id)
        assert loaded == state

    history = await store.history(workflow_id)
    assert [snapshot.step for snapshot in history] == steps
    assert [snapshot.snapshot_id for snapshot in history] == snapshot_ids


@pytest.mark.asyncio
async def test_circuit_breaker_trips_and_allows_probe():
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05, half_open_max_calls=1)

    async def failing():
        raise RuntimeError("boom")

    async def succeeding():
        return "ok"

    with pytest.raises(RuntimeError):
        await breaker.call(failing)

    with pytest.raises(RuntimeError):
        await breaker.call(failing)

    with pytest.raises(CircuitOpenError):
        await breaker.call(failing)

    await asyncio.sleep(0.06)
    assert await breaker.call(succeeding) == "ok"


@pytest.mark.asyncio
async def test_retry_policy_exponential_backoff(monkeypatch):
    sleep_durations: list[float] = []

    async def fake_sleep(duration: float) -> None:
        sleep_durations.append(duration)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(random, "uniform", lambda low, high: high)

    policy = RetryPolicy(max_retries=3, base_delay=0.01, max_delay=1.0, exponential_base=2.0)
    attempts = {"count": 0}

    async def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ValueError("fail")
        return "done"

    result = await policy.execute(flaky)
    assert result == "done"
    assert attempts["count"] == 3
    assert len(sleep_durations) == 2
    assert sleep_durations[1] >= sleep_durations[0]


@pytest.mark.asyncio
async def test_dead_letter_queue_capture_retry_and_purge():
    bus = InMemoryBus()
    dlq = DeadLetterQueue(bus=bus)
    original = make_message(topic="dlq.topic", payload={"event": "fail"})

    await dlq.send(original, error="boom", source="tester")
    failed = await dlq.list_failed()
    assert len(failed) == 1
    dead_letter = failed[0]

    queue: asyncio.Queue[Message] = asyncio.Queue()

    async def listener(message: Message) -> None:
        await queue.put(message)

    sub = await bus.subscribe(original.topic, listener)
    assert await dlq.retry(dead_letter.id)
    replayed = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert replayed.message_id == original.message_id

    await dlq.purge(dead_letter.id)
    assert await dlq.list_failed() == []
    await bus.unsubscribe(sub)


def test_core_package_reexports_public_api():
    import core

    from core.agents import AgentResult as AgentsAgentResult
    from core.agents import AgentTask as AgentsAgentTask
    from core.agents import BaseAgent as AgentsBaseAgent
    from core.agents import Tool as AgentsTool
    from core.messaging import InMemoryBus as MessagingInMemoryBus
    from core.messaging import Message as MessagingMessage
    from core.messaging import MessageBus as MessagingBus
    from core.resilience import CircuitBreaker as ResilienceCircuitBreaker
    from core.resilience import DeadLetterQueue as ResilienceDeadLetterQueue
    from core.resilience import RetryPolicy as ResilienceRetryPolicy
    from core.state import Event as StateEvent
    from core.state import EventStore as StateEventStore
    from core.state import InMemoryEventStore as StateInMemoryEventStore
    from core.state import Snapshot as StateSnapshot
    from core.state import SnapshotStore as StateSnapshotStore
    from core.tracing import TracingManager as TracingManagerClass
    from core.tracing import extract_context as tracing_extract_context
    from core.tracing import inject_context as tracing_inject_context
    from core.tracing import traced as tracing_traced

    reexports = {
        "BaseAgent": AgentsBaseAgent,
        "AgentTask": AgentsAgentTask,
        "AgentResult": AgentsAgentResult,
        "Tool": AgentsTool,
        "MessageBus": MessagingBus,
        "Message": MessagingMessage,
        "InMemoryBus": MessagingInMemoryBus,
        "TracingManager": TracingManagerClass,
        "traced": tracing_traced,
        "inject_context": tracing_inject_context,
        "extract_context": tracing_extract_context,
        "EventStore": StateEventStore,
        "SnapshotStore": StateSnapshotStore,
        "Event": StateEvent,
        "Snapshot": StateSnapshot,
        "InMemoryEventStore": StateInMemoryEventStore,
        "CircuitBreaker": ResilienceCircuitBreaker,
        "RetryPolicy": ResilienceRetryPolicy,
        "DeadLetterQueue": ResilienceDeadLetterQueue,
    }

    for name, reference in reexports.items():
        assert getattr(core, name) is reference, f"core.{name} must match {reference}"


def test_core_components_are_async():
    assert inspect.iscoroutinefunction(BaseAgent.execute)
    assert inspect.iscoroutinefunction(BaseAgent.call_llm)
