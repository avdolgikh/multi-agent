from __future__ import annotations

import ast
import asyncio
import json
import logging
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Sequence, TypeVar, cast

from pydantic import BaseModel, ValidationError

from core.agents import AgentResult, AgentTask, BaseAgent
from core.messaging import Message
from core.tracing import traced

from .models import (
    AnalysisReport,
    ClassDescriptor,
    FunctionDescriptor,
    ParseResult,
    QualityResult,
    SecurityResult,
)

__all__ = [
    "ParserAgent",
    "SecurityAgent",
    "QualityAgent",
    "ReportAgent",
    "LLMResponseFormatError",
]

_LOGGER = logging.getLogger(__name__)
_SOURCE_CHAR_LIMIT = 8000

ModelT = TypeVar("ModelT", bound=BaseModel)
FunctionEntry = FunctionDescriptor | dict[str, Any]
ClassEntry = ClassDescriptor | dict[str, Any]
EntryT = TypeVar("EntryT", FunctionEntry, ClassEntry)


class LLMResponseFormatError(RuntimeError):
    """Raised when an LLM response cannot be parsed into the expected schema."""

    def __init__(self, agent_name: str, reason: str) -> None:
        super().__init__(f"{agent_name} received invalid LLM response: {reason}")
        self.agent_name = agent_name
        self.reason = reason


async def _gather_sources(input_path: str) -> dict[Path, str]:
    path = Path(input_path)
    files: Iterable[Path]
    if path.is_file():
        files = [path]
    else:
        files = sorted(p for p in path.rglob("*.py") if p.is_file())
    loop = asyncio.get_running_loop()
    contents: dict[Path, str] = {}
    for file_path in files:
        contents[file_path] = await loop.run_in_executor(None, file_path.read_text)
    return contents


def _json_dumps(value: Any) -> str:
    return json.dumps(value, indent=2, default=str)


def _strip_code_fence(payload: str) -> str:
    clean = payload.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if len(lines) >= 2 and lines[0].lstrip("`").startswith("json"):
            lines = lines[1:]
        if lines and lines[-1].strip("`") == "":
            lines = lines[:-1]
        clean = "\n".join(lines).strip("` \n")
    return clean


def _parse_llm_response(
    *,
    agent_name: str,
    model_type: type[ModelT],
    raw_content: str,
) -> ModelT:
    clean = _strip_code_fence(raw_content)
    if not clean:
        raise LLMResponseFormatError(agent_name, "empty response")
    try:
        data = json.loads(clean)
    except json.JSONDecodeError as exc:  # pragma: no cover - exercised in new tests
        raise LLMResponseFormatError(agent_name, "non-JSON response") from exc
    try:
        return model_type.model_validate(data)
    except ValidationError as exc:  # pragma: no cover - schema enforcement exercised
        raise LLMResponseFormatError(agent_name, "response did not match schema") from exc


@contextmanager
def _suppress_agent_system_prompt(agent: BaseAgent):
    original_prompt = agent.system_prompt
    agent.system_prompt = ""
    try:
        yield original_prompt
    finally:
        agent.system_prompt = original_prompt


async def _call_llm_for_model(
    *,
    agent: BaseAgent,
    user_content: str,
    model_type: type[ModelT],
) -> ModelT:
    with _suppress_agent_system_prompt(agent) as system_prompt:
        message_payloads = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        response = await agent.call_llm(cast("Sequence[Message]", message_payloads))
    return _parse_llm_response(
        agent_name=agent.name,
        model_type=model_type,
        raw_content=response.content,
    )


def _format_source_snippets(sources: dict[Path, str]) -> str:
    parts: list[str] = []
    for path, content in sources.items():
        snippet = content[:_SOURCE_CHAR_LIMIT]
        if len(content) > _SOURCE_CHAR_LIMIT:
            snippet = f"{snippet}\n...<truncated>"
        parts.append(f"### {path}\n{snippet}")
    return "\n\n".join(parts)


def _merge_entries(
    *,
    base_items: Sequence[EntryT],
    llm_items: Sequence[EntryT],
) -> list[EntryT]:
    merged: dict[str, EntryT] = {}

    def _store_entry(entry: EntryT) -> None:
        name = _extract_descriptor_name(entry)
        if name:
            merged[name] = entry
        else:
            merged[f"llm_{len(merged)}"] = entry

    for candidate in base_items:
        _store_entry(candidate)
    for candidate in llm_items:
        _store_entry(candidate)
    return list(merged.values())


def _extract_descriptor_name(value: FunctionEntry | ClassEntry) -> str | None:
    if isinstance(value, dict):
        name = value.get("name")
        return str(name) if isinstance(name, str) else None
    return getattr(value, "name", None)


def _serialize_model(model: BaseModel | dict[str, Any] | list[Any]) -> Any:
    if isinstance(model, BaseModel):
        return model.model_dump()
    if isinstance(model, list):
        return [_serialize_model(item) for item in model]
    return model


class ParserAgent(BaseAgent):
    @traced  # pyright: ignore[reportIncompatibleMethodOverride]
    async def execute(self, task: AgentTask) -> AgentResult:  # pyright: ignore[reportIncompatibleMethodOverride]
        _LOGGER.info("ParserAgent.execute task_id=%s", task.task_id)
        input_path = task.input_data.get("input_path")
        if not input_path:
            raise ValueError("ParserAgent requires an input_path")
        sources = await _gather_sources(input_path)
        functions: list[FunctionDescriptor | dict[str, Any]] = []
        classes: list[ClassDescriptor | dict[str, Any]] = []
        imports: set[str] = set()
        dependencies: dict[str, str] = {}
        for file_path, content in sources.items():
            module = ast.parse(content or "", filename=str(file_path))
            for node in module.body:
                if isinstance(node, ast.FunctionDef):
                    functions.append(self._build_function_descriptor(node))
                elif isinstance(node, ast.ClassDef):
                    classes.append(self._build_class_descriptor(node))
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    module_name = self._extract_import(node)
                    if module_name:
                        imports.add(module_name)
                        dependencies[module_name] = "external"
        heuristic = ParseResult(
            functions=functions,
            classes=classes,
            imports=sorted(imports),
            dependencies=dependencies,
        )
        user_content = "\n\n".join(
            [
                "Confirm, correct, or enrich the structural summary of the repository. "
                "Return JSON that matches the ParseResult schema.",
                "Respond with JSON only. Do not wrap the payload in code fences.",
                f"JSON schema:\n{_json_dumps(ParseResult.model_json_schema())}",
                f"AST context:\n{_json_dumps(heuristic.model_dump())}",
                f"Source code (truncated to {_SOURCE_CHAR_LIMIT} chars per file):\n"
                f"{_format_source_snippets(sources)}",
            ]
        )
        llm_result = await _call_llm_for_model(
            agent=self,
            user_content=user_content,
            model_type=ParseResult,
        )
        merged = ParseResult(
            functions=_merge_entries(
                base_items=heuristic.functions,
                llm_items=llm_result.functions,
            ),
            classes=_merge_entries(
                base_items=heuristic.classes,
                llm_items=llm_result.classes,
            ),
            imports=sorted(set(heuristic.imports).union(llm_result.imports)),
            dependencies={**heuristic.dependencies, **llm_result.dependencies},
        )
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data={"result": merged},
            status="success",
            error=None,
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )

    def _build_function_descriptor(self, node: ast.FunctionDef) -> FunctionDescriptor:
        params = [arg.arg for arg in node.args.args]
        return FunctionDescriptor(
            name=node.name,
            params=params,
            return_type=self._annotation_to_str(node.returns),
            line_range=self._line_range(node),
        )

    def _build_class_descriptor(self, node: ast.ClassDef) -> ClassDescriptor:
        methods = [item.name for item in node.body if isinstance(item, ast.FunctionDef)]
        return ClassDescriptor(name=node.name, line_range=self._line_range(node), methods=methods)

    def _annotation_to_str(self, annotation: ast.AST | None) -> str | None:
        if annotation is None:
            return None
        if isinstance(annotation, ast.Name):
            return annotation.id
        if isinstance(annotation, ast.Attribute):
            return annotation.attr
        return ast.unparse(annotation)

    def _line_range(self, node: ast.AST) -> str:
        start = getattr(node, "lineno", 0)
        end = getattr(node, "end_lineno", start)
        return f"{start}-{end}"

    def _extract_import(self, node: ast.stmt) -> str | None:
        if isinstance(node, ast.Import):
            if node.names:
                return node.names[0].name
        if isinstance(node, ast.ImportFrom):
            return node.module
        return None


class SecurityAgent(BaseAgent):
    @traced  # pyright: ignore[reportIncompatibleMethodOverride]
    async def execute(self, task: AgentTask) -> AgentResult:  # pyright: ignore[reportIncompatibleMethodOverride]
        _LOGGER.info("SecurityAgent.execute task_id=%s", task.task_id)
        input_path = task.input_data.get("input_path")
        if not input_path:
            raise ValueError("SecurityAgent requires an input_path")
        sources = await _gather_sources(input_path)
        candidates = self._collect_candidates(sources)
        context = {
            "candidate_summary": {"count": len(candidates)},
            "candidates": candidates,
        }
        user_content = "\\n\\n".join(
            [
                "Review the Python source for OWASP issues, hardcoded secrets, and risky APIs. "
                "Use the candidate list as hints but verify findings yourself.",
                "Return JSON that matches the SecurityResult schema with severity, location, description, "
                "and recommendation for each finding.",
                "Respond with JSON only.",
                f"JSON schema:\\n{_json_dumps(SecurityResult.model_json_schema())}",
                f"AST context:\\n{_json_dumps(context)}",
                f"Source code (truncated to {_SOURCE_CHAR_LIMIT} chars per file):\\n"
                f"{_format_source_snippets(sources)}",
            ]
        )
        llm_result = await _call_llm_for_model(
            agent=self,
            user_content=user_content,
            model_type=SecurityResult,
        )
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data={"result": llm_result},
            status="success",
            error=None,
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )

    def _collect_candidates(self, sources: dict[Path, str]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for path, content in sources.items():
            candidates.extend(self._scan_content(path, content))
        return candidates

    def _scan_content(self, path: Path, content: str) -> list[dict[str, Any]]:
        lowered = content.lower()
        records: list[dict[str, Any]] = []
        if "password" in lowered or "secret" in lowered:
            lineno, line = self._find_line(content, ["password", "secret"])
            records.append(
                self._build_candidate(
                    path=path,
                    lineno=lineno,
                    indicator="credential",
                    reason="Possible hardcoded credential detected.",
                    snippet=line,
                )
            )
        if "os.system" in lowered or "subprocess" in lowered:
            lineno, line = self._find_line(content, ["os.system", "subprocess"])
            records.append(
                self._build_candidate(
                    path=path,
                    lineno=lineno,
                    indicator="shell_execution",
                    reason="Shell execution detected; ensure inputs are sanitized.",
                    snippet=line,
                )
            )
        if "eval(" in lowered or "exec(" in lowered:
            lineno, line = self._find_line(content, ["eval(", "exec("])
            records.append(
                self._build_candidate(
                    path=path,
                    lineno=lineno,
                    indicator="dynamic_code",
                    reason="Dynamic code execution function referenced.",
                    snippet=line,
                )
            )
        for lineno, line in enumerate(content.splitlines(), start=1):
            normalized = line.lower()
            if "select" in normalized and "+" in line:
                records.append(
                    self._build_candidate(
                        path=path,
                        lineno=lineno,
                        indicator="sql_concat",
                        reason="SQL query appears to be built via string concatenation.",
                        snippet=line.strip(),
                    )
                )
        return records

    def _build_candidate(
        self,
        *,
        path: Path,
        lineno: int,
        indicator: str,
        reason: str,
        snippet: str | None,
    ) -> dict[str, Any]:
        return {
            "path": str(path),
            "line": lineno,
            "indicator": indicator,
            "reason": reason,
            "snippet": snippet,
        }

    def _find_line(self, content: str, keywords: list[str]) -> tuple[int, str]:
        for lineno, line in enumerate(content.splitlines(), start=1):
            lowered = line.lower()
            if any(keyword in lowered for keyword in keywords):
                return lineno, line.strip()
        return 1, ""


class QualityAgent(BaseAgent):
    @traced  # pyright: ignore[reportIncompatibleMethodOverride]
    async def execute(self, task: AgentTask) -> AgentResult:  # pyright: ignore[reportIncompatibleMethodOverride]
        _LOGGER.info("QualityAgent.execute task_id=%s", task.task_id)
        input_path = task.input_data.get("input_path")
        if not input_path:
            raise ValueError("QualityAgent requires an input_path")
        sources = await _gather_sources(input_path)
        complexity_scores: Counter[str] = Counter()
        long_functions: list[dict[str, Any]] = []
        for path, content in sources.items():
            module = ast.parse(content or "", filename=str(path))
            for node in module.body:
                if isinstance(node, ast.FunctionDef):
                    complexity = self._estimate_complexity(node)
                    complexity_scores[node.name] += complexity
                    length = (getattr(node, "end_lineno", node.lineno) - node.lineno) + 1
                    if length > 50:
                        long_functions.append(
                            {
                                "path": str(path),
                                "function": node.name,
                                "line_range": self._line_range(node),
                                "length": length,
                            }
                        )
        heuristics = {
            "cyclomatic_complexity": dict(complexity_scores),
            "long_functions": long_functions,
        }
        user_content = "\\n\\n".join(
            [
                "Review the code for maintainability issues, logic bugs, and boundary mistakes. "
                "Return a QualityResult JSON payload with score, issues, and metrics.",
                "Use the metrics to ground your reasoning but make independent judgments.",
                "For every issue, `location` MUST be a function or class name taken "
                "from the AST context (e.g., 'compute_average_off_by_one'). Do NOT use "
                "file paths, line numbers, or other formats.",
                "Respond with JSON only.",
                f"JSON schema:\\n{_json_dumps(QualityResult.model_json_schema())}",
                f"AST context:\\n{_json_dumps(heuristics)}",
                f"Source code (truncated to {_SOURCE_CHAR_LIMIT} chars per file):\\n"
                f"{_format_source_snippets(sources)}",
            ]
        )
        llm_result = await _call_llm_for_model(
            agent=self,
            user_content=user_content,
            model_type=QualityResult,
        )
        metrics = dict(llm_result.metrics)
        metrics["cyclomatic_complexity"] = heuristics["cyclomatic_complexity"]
        quality_result = QualityResult(
            score=llm_result.score,
            issues=llm_result.issues,
            metrics=metrics,
        )
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data={"result": quality_result},
            status="success",
            error=None,
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )

    def _estimate_complexity(self, node: ast.FunctionDef) -> int:
        complexity = 1
        for child in ast.walk(node):
            if isinstance(
                child, (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.BoolOp, ast.Match)
            ):
                complexity += 1
        return complexity

    def _line_range(self, node: ast.AST) -> str:
        start = getattr(node, "lineno", 0)
        end = getattr(node, "end_lineno", start)
        return f"{start}-{end}"


class ReportAgent(BaseAgent):
    @traced  # pyright: ignore[reportIncompatibleMethodOverride]
    async def execute(self, task: AgentTask) -> AgentResult:  # pyright: ignore[reportIncompatibleMethodOverride]
        _LOGGER.info("ReportAgent.execute task_id=%s", task.task_id)
        input_path = task.input_data.get("input_path")
        if not input_path:
            raise ValueError("ReportAgent requires an input_path")
        sources = await _gather_sources(input_path)
        results_map = task.input_data.get("results") or {}
        parse_result = self._find_result(results_map, ParseResult)
        security_result = self._find_result(results_map, SecurityResult)
        quality_result = self._find_result(results_map, QualityResult)
        context = {
            "input_path": input_path,
            "parse_result": _serialize_model(parse_result),
            "security_result": _serialize_model(security_result),
            "quality_result": _serialize_model(quality_result),
        }
        user_content = "\\n\\n".join(
            [
                "Produce an AnalysisReport JSON payload summarizing the code analysis. "
                "Executive summary must cite specific findings, the security section must capture key risks, "
                "the quality section must describe scores/issues, and recommendations must be prioritized with reasoning.",
                "Respond with JSON only.",
                f"JSON schema:\\n{_json_dumps(AnalysisReport.model_json_schema())}",
                f"AST context:\\n{_json_dumps(context)}",
                f"Source code (truncated to {_SOURCE_CHAR_LIMIT} chars per file):\\\n"
                f"{_format_source_snippets(sources)}",
            ]
        )
        report = await _call_llm_for_model(
            agent=self,
            user_content=user_content,
            model_type=AnalysisReport,
        )
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data={"result": report},
            status="success",
            error=None,
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )

    def _find_result(self, values: dict[str, Any], model_type: type[Any]) -> Any:
        for candidate in values.values():
            if isinstance(candidate, model_type):
                return candidate
            if isinstance(candidate, dict):
                try:
                    return model_type.model_validate(candidate)
                except Exception:  # noqa: BLE001
                    continue
        return model_type()  # type: ignore[call-arg]
