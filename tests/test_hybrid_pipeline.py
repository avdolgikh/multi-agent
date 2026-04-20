from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.sdk.trace.export import InMemorySpanExporter, SimpleSpanProcessor
from pydantic import BaseModel

from core.agents import AgentResult, AgentTask, BaseAgent
from core.messaging import InMemoryBus, Message
from core.state import Event, InMemoryEventStore, SnapshotStore
from core.tracing import TracingManager, inject_context, traced


def _load_pipeline_modules() -> SimpleNamespace:
    module_names = {
        "root": "hybrid",
        "package": "hybrid.project_analysis",
        "models": "hybrid.project_analysis.models",
        "events": "hybrid.project_analysis.events",
        "team": "hybrid.project_analysis.team",
        "stubs": "hybrid.project_analysis.stubs",
        "orchestrator": "hybrid.project_analysis.orchestrator",
    }
    loaded: dict[str, Any] = {}
    try:
        for key, module_name in module_names.items():
            loaded[key] = importlib.import_module(module_name)
    except ModuleNotFoundError:
        pytest.fail(
            "hybrid.project_analysis is not implemented yet; the hybrid foundation "
            "pipeline spec requires its public modules and re-exports."
        )
    return SimpleNamespace(**loaded)


def _public_model_classes(module: Any) -> list[tuple[str, type[BaseModel]]]:
    return [
        (name, value)
        for name, value in vars(module).items()
        if not name.startswith("_")
        and inspect.isclass(value)
        and issubclass(value, BaseModel)
        and value is not BaseModel
    ]


def _contains_value(value: Any, needle: str) -> bool:
    needle = needle.lower()
    if isinstance(value, BaseModel):
        return _contains_value(value.model_dump(), needle)
    if isinstance(value, Mapping):
        return any(
            _contains_value(key, needle) or _contains_value(item, needle)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_contains_value(item, needle) for item in value)
    return needle in str(value).lower()


def _contains_exact_mapping(value: Any, expected: Mapping[str, Any]) -> bool:
    if isinstance(value, BaseModel):
        return _contains_exact_mapping(value.model_dump(), expected)
    if isinstance(value, Mapping):
        if dict(value) == dict(expected):
            return True
        return any(_contains_exact_mapping(item, expected) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_exact_mapping(item, expected) for item in value)
    return False


def _is_fully_populated(value: Any) -> bool:
    if isinstance(value, BaseModel):
        return _is_fully_populated(value.model_dump())
    if isinstance(value, Mapping):
        return bool(value) and all(_is_fully_populated(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return bool(value) and all(_is_fully_populated(item) for item in value)
    if isinstance(value, str):
        return value.strip() != ""
    return value is not None


def _normalize_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, BaseModel):
        dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if hasattr(value, "output_data"):
        output = getattr(value, "output_data")
        if isinstance(output, Mapping):
            return dict(output)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dumped
    raise AssertionError(f"Expected a mapping-like result, got {type(value)!r}")


def _public_method_names(cls: type[Any]) -> list[str]:
    return [
        name for name, value in vars(cls).items() if callable(value) and not name.startswith("_")
    ]


def _find_public_member(module: Any, *keywords: str, kind: str = "any") -> tuple[str, Any]:
    for name, value in vars(module).items():
        if name.startswith("_"):
            continue
        lower = name.lower()
        if not all(keyword.lower() in lower for keyword in keywords):
            continue
        if kind == "class" and not inspect.isclass(value):
            continue
        if kind == "callable" and (not callable(value) or inspect.isclass(value)):
            continue
        if kind == "module" and not inspect.ismodule(value):
            continue
        return name, value
    raise AssertionError(
        f"Could not find a public {kind} in {getattr(module, '__name__', module)!r} "
        f"matching keywords {keywords}"
    )


def _find_model_by_field_specs(
    module: Any,
    field_specs: list[tuple[tuple[str, ...], Any]],
    *,
    contains: tuple[str, ...] = (),
    exclude_keywords: tuple[str, ...] = (),
) -> tuple[str, type[BaseModel], BaseModel]:
    for name, model_cls in _public_model_classes(module):
        lower_name = name.lower()
        if any(keyword.lower() in lower_name for keyword in exclude_keywords):
            continue
        payload: dict[str, Any] = {}
        for keywords, value in field_specs:
            chosen_field = None
            for field_name in model_cls.model_fields:
                lower_field = field_name.lower()
                if any(keyword.lower() in lower_field for keyword in keywords):
                    chosen_field = field_name
                    break
            if chosen_field is None:
                break
            payload[chosen_field] = value
        else:
            try:
                instance = model_cls.model_validate(payload)
            except Exception:  # noqa: BLE001
                continue
            if all(_contains_value(instance, needle) for needle in contains):
                return name, model_cls, instance
    raise AssertionError(
        f"Could not find a public model in {getattr(module, '__name__', module)!r} "
        f"matching field semantics {[keywords for keywords, _ in field_specs]}"
    )


def _find_model_by_payload(
    module: Any,
    payload: dict[str, Any],
    *,
    contains: tuple[str, ...] = (),
    exclude_keywords: tuple[str, ...] = (),
) -> tuple[str, type[BaseModel], BaseModel]:
    for name, model_cls in _public_model_classes(module):
        lower_name = name.lower()
        if any(keyword.lower() in lower_name for keyword in exclude_keywords):
            continue
        try:
            instance = model_cls.model_validate(payload)
        except Exception:  # noqa: BLE001
            continue
        if all(_contains_value(instance, needle) for needle in contains):
            return name, model_cls, instance
    raise AssertionError(
        f"Could not find a public model in {getattr(module, '__name__', module)!r} "
        f"that validates payload keys {sorted(payload)}"
    )


def _setup_tracing(service_name: str) -> tuple[Any, InMemorySpanExporter]:
    provider = TracingManager.setup(service_name, endpoint=None)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _sample_structure_payload() -> dict[str, Any]:
    return {
        "functions": [{"name": "compute", "params": ["value"]}],
        "classes": [{"name": "Calculator"}],
        "imports": ["math"],
        "dependencies": {"math": "stdlib"},
    }


def _sample_dependency_payload() -> dict[str, Any]:
    return {
        "dependencies": {"requests": "2.31.0"},
        "packages": [{"name": "requests", "version": "2.31.0"}],
        "findings": [],
    }


def _sample_security_payload() -> dict[str, Any]:
    return {
        "findings": [
            {
                "severity": "medium",
                "location": "module.py:10",
                "description": "Potential secret usage",
                "recommendation": "Move secrets to environment variables",
            }
        ]
    }


def _sample_quality_payload() -> dict[str, Any]:
    return {
        "score": 92,
        "issues": [
            {
                "location": "module.py:1",
                "description": "Missing docstring",
                "severity": "low",
            }
        ],
        "metrics": {"cyclomatic_complexity": {"compute": 2}},
    }


def _sample_project_report_payload() -> dict[str, Any]:
    return {
        "summary": "All sections are present.",
        "sections": {
            "discovery": {
                "structure": _sample_structure_payload(),
                "dependencies": _sample_dependency_payload(),
            },
            "deep_dive": {
                "security": _sample_security_payload(),
                "quality": _sample_quality_payload(),
            },
        },
        "recommendations": ["Add tests"],
    }


def _build_with_semantics(callable_obj: Any, semantic_kwargs: dict[str, Any]) -> Any:
    signature = inspect.signature(callable_obj)
    param_names = list(signature.parameters)

    keyword_map = {
        "name": ["team_name", "name", "team"],
        "agent_id": ["agent_id", "agent", "id"],
        "agents": ["agents", "members", "agent_list"],
        "agent_pairs": ["pairs", "pair", "agent_pairs", "member_pairs", "members", "outputs"],
        "bus": ["message_bus", "bus"],
        "event_store": ["event_store", "events", "store"],
        "aggregator": ["aggregator", "merge", "combine"],
        "tracer_provider": ["tracer_provider", "tracer", "provider"],
        "canned_output": ["canned", "output", "payload", "result"],
        "fail": ["fail", "failure", "error", "broken"],
        "started": ["started"],
        "release": ["release"],
        "teams": ["teams", "team_map", "discovery"],
        "discovery_teams": ["discovery", "teams", "team_map"],
        "deep_dive_teams": ["deep", "teams", "team_map"],
        "synthesis_agent": ["synthesis", "report_agent", "report"],
        "validator": ["validator", "validation", "phase_validator"],
        "phase_validator": ["phase", "validator"],
        "snapshot_store": ["snapshot_store", "snapshot", "snapshots"],
        "expected_team_names": ["expected", "team_names"],
        "discovery_team_names": ["discovery", "team_names"],
        "deep_dive_team_names": ["deep", "team_names"],
        "workflow_id": ["workflow", "workflow_id", "analysis", "run"],
        "phase": ["phase", "state", "step"],
        "result": ["result", "results", "output", "report", "data"],
        "valid": ["valid", "passed", "success", "ok", "approved"],
        "errors": ["error", "errors", "issues", "problem", "message"],
        "path": ["path", "input", "project", "source"],
    }

    kwargs: dict[str, Any] = {}
    for semantic_name, value in semantic_kwargs.items():
        keywords = keyword_map.get(semantic_name, [semantic_name])
        for param_name in param_names:
            lower = param_name.lower()
            if any(keyword in lower for keyword in keywords):
                kwargs[param_name] = value
                break
    return callable_obj(**kwargs)


def _phase_value(orchestrator_module: Any, name: str) -> Any:
    target = name.upper()
    for candidate_name, candidate in vars(orchestrator_module).items():
        if candidate_name.startswith("_"):
            continue
        if candidate_name.upper() == target and isinstance(candidate, str):
            return candidate
        if hasattr(candidate, target):
            return getattr(candidate, target)
    return target


def _extract_bool_field(model: Any) -> bool:
    if isinstance(model, bool):
        return model
    if isinstance(model, BaseModel):
        for field_name, field_info in model.model_fields.items():
            lower = field_name.lower()
            if any(token in lower for token in ("valid", "pass", "success", "ok")):
                return bool(getattr(model, field_name))
            annotation = getattr(field_info, "annotation", None)
            if annotation is bool:
                return bool(getattr(model, field_name))
        dumped = model.model_dump()
        for key, value in dumped.items():
            if any(token in key.lower() for token in ("valid", "pass", "success", "ok")):
                return bool(value)
    raise AssertionError(f"Could not determine a boolean result from {model!r}")


def _find_coroutine_method(obj: Any, *keywords: str) -> tuple[str, Any]:
    cls = obj if inspect.isclass(obj) else obj.__class__
    for name, value in inspect.getmembers(cls):
        if name.startswith("_") or not callable(value):
            continue
        if not all(keyword.lower() in name.lower() for keyword in keywords):
            continue
        if inspect.iscoroutinefunction(value):
            return name, getattr(obj, name)
    raise AssertionError(f"Could not find an async public method matching {keywords}")


def _find_public_callable(obj: Any, *keywords: str) -> tuple[str, Any]:
    cls = obj if inspect.isclass(obj) else obj.__class__
    for name, value in inspect.getmembers(cls):
        if name.startswith("_") or not callable(value):
            continue
        if all(keyword.lower() in name.lower() for keyword in keywords):
            return name, getattr(obj, name)
    raise AssertionError(f"Could not find a public callable matching {keywords}")


def _normalize_phase(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "name"):
        return str(value.name)
    return str(value)


def _children_of(span: Any, spans: list[Any]) -> list[Any]:
    return [
        candidate
        for candidate in spans
        if candidate.parent is not None and candidate.parent.span_id == span.context.span_id
    ]


def _has_leaf_child(span: Any, spans: list[Any]) -> bool:
    return any(not _children_of(child, spans) for child in _children_of(span, spans))


def _descendant_count(span: Any, spans: list[Any]) -> int:
    children = _children_of(span, spans)
    return len(children) + sum(_descendant_count(child, spans) for child in children)


def _first_span_with_children_count(spans: list[Any], count: int) -> Any:
    for span in spans:
        if len(_children_of(span, spans)) == count:
            return span
    raise AssertionError(f"Could not find a span with {count} direct children")


class RecordingEventStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.appended: list[tuple[str, Event]] = []

    async def append(self, stream: str, event: Event) -> int:
        self.appended.append((stream, event))
        return await super().append(stream, event)


class RecordingBus(InMemoryBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, Message]] = []

    async def publish(self, topic: str, message: Message) -> None:
        self.published.append((topic, message))
        await super().publish(topic, message)


class BarrierAgent(BaseAgent):
    def __init__(
        self,
        *,
        agent_id: str,
        name: str,
        canned_output: dict[str, Any],
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            name=name,
            model="stub-model",
            provider="ollama",
            tools=[],
            system_prompt="",
            base_url=None,
        )
        self._canned_output = dict(canned_output)
        self._started = started
        self._release = release
        self.seen_trace_contexts: list[dict[str, Any]] = []

    @traced
    async def execute(self, task: AgentTask) -> AgentResult:
        self.seen_trace_contexts.append(task.trace_context or {})
        self._started.set()
        await self._release.wait()
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data=dict(self._canned_output),
            status="success",
            error=None,
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )


class RecordingSynthesisAgent(BaseAgent):
    def __init__(self, *, agent_id: str, name: str, canned_output: dict[str, Any]) -> None:
        super().__init__(
            agent_id=agent_id,
            name=name,
            model="stub-model",
            provider="ollama",
            tools=[],
            system_prompt="",
            base_url=None,
        )
        self._canned_output = dict(canned_output)
        self.seen_tasks: list[AgentTask] = []

    @traced
    async def execute(self, task: AgentTask) -> AgentResult:
        self.seen_tasks.append(task)
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data=dict(self._canned_output),
            status="success",
            error=None,
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )


def _make_team(
    team_module: Any,
    *,
    name: str,
    agents: list[Any],
    bus: Any,
    event_store: Any,
    aggregator: Any,
    tracer_provider: Any | None = None,
) -> Any:
    team_cls = team_module.Team
    return _build_with_semantics(
        team_cls,
        {
            "name": name,
            "agents": agents,
            "bus": bus,
            "event_store": event_store,
            "aggregator": aggregator,
            "tracer_provider": tracer_provider,
        },
    )


def _merge_agent_outputs(outputs: list[Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for output in outputs:
        if isinstance(output, AgentResult):
            merged.update(output.output_data)
            continue
        if isinstance(output, Mapping):
            merged.update(output)
            continue
        if hasattr(output, "model_dump"):
            dumped = output.model_dump()
            if isinstance(dumped, Mapping):
                merged.update(dumped)
    return merged


def _make_validator(orchestrator_module: Any) -> Any:
    _, validator_cls = _find_public_member(orchestrator_module, "validator", kind="class")
    return _build_with_semantics(
        validator_cls,
        {
            "expected_team_names": {
                "DISCOVERY": {"structure", "dependencies"},
                "DEEP_DIVE": {"security", "quality"},
                "SYNTHESIS": set(),
            },
            "discovery_team_names": {"structure", "dependencies"},
            "deep_dive_team_names": {"security", "quality"},
        },
    )


def _make_orchestrator(
    orchestrator_module: Any,
    *,
    discovery_structure_team: Any,
    discovery_dependencies_team: Any,
    deep_dive_security_team: Any,
    deep_dive_quality_team: Any,
    synthesis_agent: Any,
    validator: Any,
    event_store: Any,
    snapshot_store: Any,
    bus: Any,
    tracer_provider: Any,
) -> Any:
    _, orchestrator_cls = _find_public_member(orchestrator_module, "orchestrator", kind="class")
    return _build_with_semantics(
        orchestrator_cls,
        {
            "teams": {
                "DISCOVERY": {
                    "structure": discovery_structure_team,
                    "dependencies": discovery_dependencies_team,
                },
                "DEEP_DIVE": {
                    "security": deep_dive_security_team,
                    "quality": deep_dive_quality_team,
                },
            },
            "discovery_teams": {
                "structure": discovery_structure_team,
                "dependencies": discovery_dependencies_team,
            },
            "deep_dive_teams": {
                "security": deep_dive_security_team,
                "quality": deep_dive_quality_team,
            },
            "structure_team": discovery_structure_team,
            "dependencies_team": discovery_dependencies_team,
            "security_team": deep_dive_security_team,
            "quality_team": deep_dive_quality_team,
            "synthesis_agent": synthesis_agent,
            "validator": validator,
            "phase_validator": validator,
            "event_store": event_store,
            "snapshot_store": snapshot_store,
            "bus": bus,
            "tracer_provider": tracer_provider,
        },
    )


async def _invoke_validator_validate(validator: Any, phase: Any, result: Any) -> Any:
    validate = validator.validate
    signature = inspect.signature(validate)
    params = list(signature.parameters.values())
    kwargs: dict[str, Any] = {}
    if params:
        phase_param = params[0]
        if not phase_param.kind == inspect.Parameter.VAR_POSITIONAL:
            kwargs[phase_param.name] = phase
    if len(params) > 1:
        result_param = params[1]
        if not result_param.kind == inspect.Parameter.VAR_POSITIONAL:
            kwargs[result_param.name] = result
    outcome = validate(**kwargs) if kwargs else validate(phase, result)
    if inspect.isawaitable(outcome):
        return await outcome
    return outcome


async def _invoke_orchestrator_run(orchestrator: Any, input_path: str) -> Any:
    run = orchestrator.run
    signature = inspect.signature(run)
    if not signature.parameters:
        outcome = run()
    else:
        kwargs: dict[str, Any] = {}
        for parameter in signature.parameters.values():
            lower = parameter.name.lower()
            if any(token in lower for token in ("path", "input", "project", "source")):
                kwargs[parameter.name] = input_path
            elif "trace_context" in lower:
                kwargs[parameter.name] = inject_context()
        outcome = run(**kwargs) if kwargs else run(input_path)
    if inspect.isawaitable(outcome):
        return await outcome
    return outcome


def _phase_result_payloads() -> dict[str, dict[str, Any]]:
    return {
        "DISCOVERY": {
            "structure": _sample_structure_payload(),
            "dependencies": _sample_dependency_payload(),
        },
        "DEEP_DIVE": {
            "security": _sample_security_payload(),
            "quality": _sample_quality_payload(),
        },
        "SYNTHESIS": _sample_project_report_payload(),
    }


def _build_orchestrator_stack(
    modules: SimpleNamespace,
    *,
    discovery_started: tuple[asyncio.Event, asyncio.Event],
    discovery_release: asyncio.Event,
    deep_dive_started: tuple[asyncio.Event, asyncio.Event],
    deep_dive_release: asyncio.Event,
    report_payload: dict[str, Any] | None = None,
) -> tuple[
    Any,
    Any,
    InMemorySpanExporter,
    RecordingSynthesisAgent,
    SnapshotStore,
    RecordingBus,
    RecordingEventStore,
]:
    provider, exporter = _setup_tracing("hybrid-pipeline-tests")
    bus = RecordingBus()
    event_store = RecordingEventStore()
    snapshot_store = SnapshotStore()

    discovery_structure_team = _make_team(
        modules.team,
        name="structure",
        agents=[
            BarrierAgent(
                agent_id="structure-agent",
                name="Structure Agent",
                canned_output={"structure": _sample_structure_payload()},
                started=discovery_started[0],
                release=discovery_release,
            )
        ],
        bus=bus,
        event_store=event_store,
        aggregator=_merge_agent_outputs,
        tracer_provider=provider,
    )
    discovery_dependencies_team = _make_team(
        modules.team,
        name="dependencies",
        agents=[
            BarrierAgent(
                agent_id="dependencies-agent",
                name="Dependencies Agent",
                canned_output={"dependencies": _sample_dependency_payload()},
                started=discovery_started[1],
                release=discovery_release,
            )
        ],
        bus=bus,
        event_store=event_store,
        aggregator=_merge_agent_outputs,
        tracer_provider=provider,
    )
    deep_dive_security_team = _make_team(
        modules.team,
        name="security",
        agents=[
            BarrierAgent(
                agent_id="security-agent",
                name="Security Agent",
                canned_output={"security": _sample_security_payload()},
                started=deep_dive_started[0],
                release=deep_dive_release,
            )
        ],
        bus=bus,
        event_store=event_store,
        aggregator=_merge_agent_outputs,
        tracer_provider=provider,
    )
    deep_dive_quality_team = _make_team(
        modules.team,
        name="quality",
        agents=[
            BarrierAgent(
                agent_id="quality-agent",
                name="Quality Agent",
                canned_output={"quality": _sample_quality_payload()},
                started=deep_dive_started[1],
                release=deep_dive_release,
            )
        ],
        bus=bus,
        event_store=event_store,
        aggregator=_merge_agent_outputs,
        tracer_provider=provider,
    )
    synthesis_agent = RecordingSynthesisAgent(
        agent_id="report-agent",
        name="Report Agent",
        canned_output=report_payload or _sample_project_report_payload(),
    )
    validator = _make_validator(modules.orchestrator)
    orchestrator = _make_orchestrator(
        modules.orchestrator,
        discovery_structure_team=discovery_structure_team,
        discovery_dependencies_team=discovery_dependencies_team,
        deep_dive_security_team=deep_dive_security_team,
        deep_dive_quality_team=deep_dive_quality_team,
        synthesis_agent=synthesis_agent,
        validator=validator,
        event_store=event_store,
        snapshot_store=snapshot_store,
        bus=bus,
        tracer_provider=provider,
    )
    return orchestrator, provider, exporter, synthesis_agent, snapshot_store, bus, event_store


def _phase_validated_model(events_module: Any) -> type[BaseModel]:
    for name, model_cls in _public_model_classes(events_module):
        lower = name.lower()
        if "phase" not in lower or "valid" not in lower:
            continue
        field_names = {field.lower() for field in model_cls.model_fields}
        if any("next" in field for field in field_names) and any(
            "phase" in field for field in field_names
        ):
            return model_cls
    raise AssertionError("Expected a phase-validated event payload model")


def _topic_constants(module: Any) -> dict[str, str]:
    return {
        name: value
        for name, value in vars(module).items()
        if name.isupper() and isinstance(value, str)
    }


def _phase_validated_topic(module: Any) -> str:
    for value in _topic_constants(module).values():
        lower = value.lower()
        if "phase" in lower and "valid" in lower:
            return value
    raise AssertionError("Expected a phase-validated topic constant")


def _build_project_input_file(tmp_path: Any) -> str:
    path = tmp_path / "demo.py"
    path.write_text("def demo():\n    return 1\n", encoding="utf-8")
    return str(path)


def _analysis_state_model(models_module: Any) -> tuple[str, type[BaseModel], BaseModel]:
    return _find_model_by_field_specs(
        models_module,
        [
            (("workflow", "workflow_id", "analysis", "run"), "workflow-1"),
            (("phase", "state", "step"), "DISCOVERY"),
        ],
        contains=("workflow-1", "discovery"),
    )


def _phase_result_model(models_module: Any) -> tuple[str, type[BaseModel], BaseModel]:
    return _find_model_by_field_specs(
        models_module,
        [
            (("phase", "state", "step"), "DISCOVERY"),
            (("result", "results", "output", "report", "data"), {"structure": {"marker": "one"}}),
        ],
        contains=("DISCOVERY", "one"),
        exclude_keywords=("validation",),
    )


def _report_model_class(models_module: Any) -> tuple[str, type[BaseModel]]:
    for name, model_cls in _public_model_classes(models_module):
        if "report" in name.lower():
            return name, model_cls
    raise AssertionError(
        f"Could not find a public report model in {getattr(models_module, '__name__', models_module)!r}"
    )


def _validation_result_model(models_module: Any) -> tuple[str, type[BaseModel], BaseModel]:
    return _find_model_by_field_specs(
        models_module,
        [
            (("valid", "passed", "success", "ok", "approved"), True),
            (("error", "errors", "issues", "problem", "message"), []),
        ],
    )


def _team_complete_messages(bus: RecordingBus) -> list[tuple[str, Message]]:
    return [
        (topic, message)
        for topic, message in bus.published
        if "team" in topic.lower() and "complete" in topic.lower()
    ]


def _phase_validated_messages(
    bus: RecordingBus,
    phase_validated_cls: type[BaseModel],
    *,
    expected_topic: str | None = None,
) -> list[tuple[str, Message, BaseModel]]:
    validated: list[tuple[str, Message, BaseModel]] = []
    for published_topic, message in bus.published:
        if expected_topic is not None and published_topic != expected_topic:
            continue
        try:
            payload = phase_validated_cls.model_validate(_normalize_mapping(message.payload))
        except Exception:  # noqa: BLE001
            continue
        validated.append((published_topic, message, payload))
    return validated


def _next_phase_from_payload(payload: BaseModel) -> str:
    dumped = payload.model_dump()
    for key, value in dumped.items():
        lower = key.lower()
        if "next" in lower and "phase" in lower:
            return _normalize_phase(value)
    raise AssertionError("Could not find a next-phase field in the phase-validated payload")


def _snapshot_phases(history: list[Any]) -> list[str]:
    phases: list[str] = []
    for snapshot in history:
        for phase in ("DISCOVERY", "DEEP_DIVE", "SYNTHESIS", "COMPLETED"):
            if _contains_value(snapshot.state, phase):
                phases.append(phase)
                break
    return phases


def _spans_by_trace(spans: list[Any]) -> list[Any]:
    root = next((span for span in spans if span.parent is None), None)
    if root is None:
        return []
    trace_id = root.context.trace_id
    return [span for span in spans if span.context.trace_id == trace_id]


def test_domain_models_cover_phase_results_analysis_state_report_and_validation_result():
    modules = _load_pipeline_modules()

    phase_result_name, phase_result_cls, phase_result = _phase_result_model(modules.models)
    analysis_state_name, analysis_state_cls, analysis_state = _analysis_state_model(modules.models)
    report_name, report_cls = _report_model_class(modules.models)
    validation_name, validation_cls, validation_result = _validation_result_model(modules.models)

    for model_cls in (phase_result_cls, analysis_state_cls, report_cls, validation_cls):
        assert issubclass(model_cls, BaseModel)

    assert _contains_value(phase_result, "DISCOVERY")
    assert _contains_value(phase_result, "one")
    assert _contains_value(analysis_state, "workflow-1")
    assert _contains_value(analysis_state, "DISCOVERY")
    assert _contains_value(validation_result, "True") or _extract_bool_field(validation_result)

    assert phase_result_name
    assert analysis_state_name
    assert report_name
    assert validation_name


def test_event_vocab_exposes_phase_validated_topic_and_payload():
    modules = _load_pipeline_modules()
    constants = _topic_constants(modules.events)
    phase_validated_cls = _phase_validated_model(modules.events)
    phase_validated_topic = _phase_validated_topic(modules.events)

    assert any(
        "phase" in value.lower() and "valid" in value.lower() for value in constants.values()
    )
    assert phase_validated_topic in constants.values()

    topic_model_name, topic_model_cls, topic_model = _find_model_by_field_specs(
        modules.events,
        [
            (("phase",), "DISCOVERY"),
            (("next", "phase"), "DEEP_DIVE"),
        ],
        contains=("DISCOVERY", "DEEP_DIVE"),
        exclude_keywords=("team",),
    )

    assert issubclass(phase_validated_cls, BaseModel)
    assert issubclass(topic_model_cls, BaseModel)
    assert _contains_value(topic_model, "DISCOVERY")
    assert _contains_value(topic_model, "DEEP_DIVE")
    assert topic_model_name
    assert any("next" in field.lower() for field in phase_validated_cls.model_fields)


@pytest.mark.asyncio
async def test_phase_validator_is_async_and_enforces_expected_team_outputs():
    modules = _load_pipeline_modules()
    validator = _make_validator(modules.orchestrator)

    assert inspect.iscoroutinefunction(validator.validate)

    discovery = _phase_value(modules.orchestrator, "DISCOVERY")
    deep_dive = _phase_value(modules.orchestrator, "DEEP_DIVE")
    synthesis = _phase_value(modules.orchestrator, "SYNTHESIS")

    discovery_invalid = await _invoke_validator_validate(
        validator,
        discovery,
        {"structure": _sample_structure_payload()},
    )
    discovery_valid = await _invoke_validator_validate(
        validator,
        discovery,
        {
            "structure": _sample_structure_payload(),
            "dependencies": _sample_dependency_payload(),
        },
    )
    deep_dive_valid = await _invoke_validator_validate(
        validator,
        deep_dive,
        {
            "security": _sample_security_payload(),
            "quality": _sample_quality_payload(),
        },
    )
    deep_dive_invalid = await _invoke_validator_validate(
        validator,
        deep_dive,
        {"security": _sample_security_payload()},
    )
    synthesis_invalid = await _invoke_validator_validate(
        validator,
        synthesis,
        {
            "structure": _sample_structure_payload(),
            "dependencies": _sample_dependency_payload(),
            "security": _sample_security_payload(),
            "quality": _sample_quality_payload(),
            "summary": "",
        },
    )
    synthesis_valid = await _invoke_validator_validate(
        validator,
        synthesis,
        _sample_project_report_payload(),
    )

    assert _extract_bool_field(discovery_invalid) is False
    assert _extract_bool_field(discovery_valid) is True
    assert _extract_bool_field(deep_dive_valid) is True
    assert _extract_bool_field(deep_dive_invalid) is False
    assert _extract_bool_field(synthesis_invalid) is False
    assert _extract_bool_field(synthesis_valid) is True


@pytest.mark.asyncio
async def test_orchestrator_happy_path_progresses_concurrently_snapshots_and_traces(tmp_path):
    modules = _load_pipeline_modules()
    discovery_started = (asyncio.Event(), asyncio.Event())
    deep_dive_started = (asyncio.Event(), asyncio.Event())
    discovery_release = asyncio.Event()
    deep_dive_release = asyncio.Event()

    (
        orchestrator,
        provider,
        exporter,
        synthesis_agent,
        snapshot_store,
        bus,
        _event_store,
    ) = _build_orchestrator_stack(
        modules,
        discovery_started=discovery_started,
        discovery_release=discovery_release,
        deep_dive_started=deep_dive_started,
        deep_dive_release=deep_dive_release,
    )

    phase_validated_cls = _phase_validated_model(modules.events)
    phase_validated_topic = _phase_validated_topic(modules.events)
    path = _build_project_input_file(tmp_path)

    run_task = asyncio.create_task(_invoke_orchestrator_run(orchestrator, path))
    await asyncio.wait_for(discovery_started[0].wait(), timeout=1.0)
    await asyncio.wait_for(discovery_started[1].wait(), timeout=1.0)
    discovery_release.set()
    await asyncio.wait_for(deep_dive_started[0].wait(), timeout=1.0)
    await asyncio.wait_for(deep_dive_started[1].wait(), timeout=1.0)
    deep_dive_release.set()

    report = await asyncio.wait_for(run_task, timeout=2.0)
    provider.force_flush()

    assert isinstance(report, BaseModel) or isinstance(report, dict)
    assert _contains_value(report, "All sections are present.")
    assert _is_fully_populated(report)
    assert _contains_value(orchestrator.analysis_state, "COMPLETED")

    validated_messages = _phase_validated_messages(
        bus, phase_validated_cls, expected_topic=phase_validated_topic
    )
    assert len(validated_messages) == 3
    next_phases = [_next_phase_from_payload(payload) for _, _, payload in validated_messages]
    assert next_phases == ["DEEP_DIVE", "SYNTHESIS", "COMPLETED"]

    team_complete_messages = _team_complete_messages(bus)
    assert len(team_complete_messages) == 4

    assert len(synthesis_agent.seen_tasks) == 1
    synthesis_input = synthesis_agent.seen_tasks[0].input_data
    assert _contains_exact_mapping(synthesis_input, _sample_structure_payload())
    assert _contains_exact_mapping(synthesis_input, _sample_dependency_payload())
    assert _contains_exact_mapping(synthesis_input, _sample_security_payload())
    assert _contains_exact_mapping(synthesis_input, _sample_quality_payload())

    workflow_id = getattr(orchestrator.analysis_state, "workflow_id", None)
    assert workflow_id is not None
    history = await snapshot_store.history(workflow_id)
    assert len(history) == 4
    snapshot_phases = _snapshot_phases(history)
    assert snapshot_phases == ["DISCOVERY", "DEEP_DIVE", "SYNTHESIS", "COMPLETED"]

    spans = _spans_by_trace(exporter.get_finished_spans())
    root_spans = [span for span in spans if span.parent is None]
    assert root_spans
    root_span = max(root_spans, key=lambda span: _descendant_count(span, spans))
    phase_spans = sorted(_children_of(root_span, spans), key=lambda span: span.start_time)
    assert len(phase_spans) == 3
    phase_children = {span: _children_of(span, spans) for span in phase_spans}
    discovery_phase_span, deep_dive_phase_span, synthesis_phase_span = phase_spans

    for phase_span in (discovery_phase_span, deep_dive_phase_span):
        team_spans = [child for child in phase_children[phase_span] if "run" in child.name.lower()]
        assert len(team_spans) >= 2

    synthesis_children = phase_children[synthesis_phase_span]
    synthesis_agent_spans = [
        child for child in synthesis_children if "execute" in child.name.lower()
    ]
    assert len(synthesis_agent_spans) == 1


@pytest.mark.asyncio
async def test_orchestrator_invalid_transition_raises_after_completion(tmp_path):
    modules = _load_pipeline_modules()
    discovery_started = (asyncio.Event(), asyncio.Event())
    deep_dive_started = (asyncio.Event(), asyncio.Event())
    discovery_release = asyncio.Event()
    deep_dive_release = asyncio.Event()

    orchestrator, provider, _exporter, _synthesis_agent, _snapshot_store, _bus, _event_store = (
        _build_orchestrator_stack(
            modules,
            discovery_started=discovery_started,
            discovery_release=discovery_release,
            deep_dive_started=deep_dive_started,
            deep_dive_release=deep_dive_release,
        )
    )

    path = _build_project_input_file(tmp_path)
    run_task = asyncio.create_task(_invoke_orchestrator_run(orchestrator, path))
    await asyncio.wait_for(discovery_started[0].wait(), timeout=1.0)
    await asyncio.wait_for(discovery_started[1].wait(), timeout=1.0)
    discovery_release.set()
    await asyncio.wait_for(deep_dive_started[0].wait(), timeout=1.0)
    await asyncio.wait_for(deep_dive_started[1].wait(), timeout=1.0)
    deep_dive_release.set()
    await asyncio.wait_for(run_task, timeout=2.0)
    provider.force_flush()

    with pytest.raises(Exception) as excinfo:
        await _invoke_orchestrator_run(orchestrator, path)

    message = str(excinfo.value).lower()
    assert message
    assert "transition" in message or "phase" in message or "invalid" in message


@pytest.mark.asyncio
async def test_orchestrator_deep_dive_validation_failure_surfaces_before_synthesis(tmp_path):
    modules = _load_pipeline_modules()
    discovery_started = (asyncio.Event(), asyncio.Event())
    deep_dive_started = (asyncio.Event(), asyncio.Event())
    discovery_release = asyncio.Event()
    deep_dive_release = asyncio.Event()

    provider, _exporter = _setup_tracing("hybrid-pipeline-failure-tests")
    bus = RecordingBus()
    event_store = RecordingEventStore()
    snapshot_store = SnapshotStore()

    discovery_structure_team = _make_team(
        modules.team,
        name="structure",
        agents=[
            BarrierAgent(
                agent_id="structure-agent",
                name="Structure Agent",
                canned_output={"structure": _sample_structure_payload()},
                started=discovery_started[0],
                release=discovery_release,
            )
        ],
        bus=bus,
        event_store=event_store,
        aggregator=_merge_agent_outputs,
        tracer_provider=provider,
    )
    discovery_dependencies_team = _make_team(
        modules.team,
        name="dependencies",
        agents=[
            BarrierAgent(
                agent_id="dependencies-agent",
                name="Dependencies Agent",
                canned_output={"dependencies": _sample_dependency_payload()},
                started=discovery_started[1],
                release=discovery_release,
            )
        ],
        bus=bus,
        event_store=event_store,
        aggregator=_merge_agent_outputs,
        tracer_provider=provider,
    )
    deep_dive_security_team = _make_team(
        modules.team,
        name="security",
        agents=[
            BarrierAgent(
                agent_id="security-agent",
                name="Security Agent",
                canned_output={},
                started=deep_dive_started[0],
                release=deep_dive_release,
            )
        ],
        bus=bus,
        event_store=event_store,
        aggregator=_merge_agent_outputs,
        tracer_provider=provider,
    )
    deep_dive_quality_team = _make_team(
        modules.team,
        name="quality",
        agents=[
            BarrierAgent(
                agent_id="quality-agent",
                name="Quality Agent",
                canned_output={"quality": _sample_quality_payload()},
                started=deep_dive_started[1],
                release=deep_dive_release,
            )
        ],
        bus=bus,
        event_store=event_store,
        aggregator=_merge_agent_outputs,
        tracer_provider=provider,
    )
    synthesis_agent = RecordingSynthesisAgent(
        agent_id="report-agent",
        name="Report Agent",
        canned_output=_sample_project_report_payload(),
    )
    validator = _make_validator(modules.orchestrator)
    orchestrator = _make_orchestrator(
        modules.orchestrator,
        discovery_structure_team=discovery_structure_team,
        discovery_dependencies_team=discovery_dependencies_team,
        deep_dive_security_team=deep_dive_security_team,
        deep_dive_quality_team=deep_dive_quality_team,
        synthesis_agent=synthesis_agent,
        validator=validator,
        event_store=event_store,
        snapshot_store=snapshot_store,
        bus=bus,
        tracer_provider=provider,
    )

    path = _build_project_input_file(tmp_path)
    run_task = asyncio.create_task(_invoke_orchestrator_run(orchestrator, path))
    await asyncio.wait_for(discovery_started[0].wait(), timeout=1.0)
    await asyncio.wait_for(discovery_started[1].wait(), timeout=1.0)
    discovery_release.set()
    await asyncio.wait_for(deep_dive_started[0].wait(), timeout=1.0)
    await asyncio.wait_for(deep_dive_started[1].wait(), timeout=1.0)
    deep_dive_release.set()

    with pytest.raises(Exception) as excinfo:
        await asyncio.wait_for(run_task, timeout=2.0)

    phase_validated_cls = _phase_validated_model(modules.events)
    phase_validated_topic = _phase_validated_topic(modules.events)
    validated_messages = _phase_validated_messages(
        bus, phase_validated_cls, expected_topic=phase_validated_topic
    )
    assert len(validated_messages) == 1
    assert [_next_phase_from_payload(payload) for _, _, payload in validated_messages] == [
        "DEEP_DIVE",
    ]
    message = str(excinfo.value).lower()
    assert message
    assert "validation" in message or "output" in message or "missing" in message
    assert len(synthesis_agent.seen_tasks) == 0
    assert not _contains_value(orchestrator.analysis_state, "COMPLETED")
    assert not _contains_value(orchestrator.analysis_state, "SYNTHESIS")
    assert _contains_value(orchestrator.analysis_state, "DEEP_DIVE")
    workflow_id = getattr(orchestrator.analysis_state, "workflow_id", None)
    if workflow_id is not None:
        history = await snapshot_store.history(workflow_id)
        assert history


@pytest.mark.asyncio
async def test_orchestrator_rejects_invalid_synthesis_report_and_stays_precompletion(tmp_path):
    modules = _load_pipeline_modules()
    discovery_started = (asyncio.Event(), asyncio.Event())
    deep_dive_started = (asyncio.Event(), asyncio.Event())
    discovery_release = asyncio.Event()
    deep_dive_release = asyncio.Event()

    bad_report = _sample_project_report_payload()
    bad_report["summary"] = ""

    (
        orchestrator,
        provider,
        _exporter,
        synthesis_agent,
        snapshot_store,
        bus,
        _event_store,
    ) = _build_orchestrator_stack(
        modules,
        discovery_started=discovery_started,
        discovery_release=discovery_release,
        deep_dive_started=deep_dive_started,
        deep_dive_release=deep_dive_release,
        report_payload=bad_report,
    )

    phase_validated_cls = _phase_validated_model(modules.events)
    path = _build_project_input_file(tmp_path)

    run_task = asyncio.create_task(_invoke_orchestrator_run(orchestrator, path))
    await asyncio.wait_for(discovery_started[0].wait(), timeout=1.0)
    await asyncio.wait_for(discovery_started[1].wait(), timeout=1.0)
    discovery_release.set()
    await asyncio.wait_for(deep_dive_started[0].wait(), timeout=1.0)
    await asyncio.wait_for(deep_dive_started[1].wait(), timeout=1.0)
    deep_dive_release.set()

    with pytest.raises(Exception) as excinfo:
        await asyncio.wait_for(run_task, timeout=2.0)

    message = str(excinfo.value).lower()
    assert message
    assert "validation" in message or "report" in message or "summary" in message

    phase_validated_topic = _phase_validated_topic(modules.events)
    validated_messages = _phase_validated_messages(
        bus, phase_validated_cls, expected_topic=phase_validated_topic
    )
    assert len(validated_messages) == 2
    assert [_next_phase_from_payload(payload) for _, _, payload in validated_messages] == [
        "DEEP_DIVE",
        "SYNTHESIS",
    ]
    assert len(synthesis_agent.seen_tasks) == 1
    assert not _contains_value(orchestrator.analysis_state, "COMPLETED")
    assert _contains_value(orchestrator.analysis_state, "SYNTHESIS")

    workflow_id = getattr(orchestrator.analysis_state, "workflow_id", None)
    if workflow_id is not None:
        history = await snapshot_store.history(workflow_id)
        assert history
