from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import Mapping
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.sdk.trace.export import InMemorySpanExporter, SimpleSpanProcessor
from pydantic import BaseModel

from core.agents import AgentResult, AgentTask, BaseAgent
from core.messaging import InMemoryBus, Message
from core.state import Event, InMemoryEventStore
from core.tracing import TracingManager, extract_context, inject_context, traced


def _load_hybrid_modules() -> SimpleNamespace:
    module_names = {
        "root": "hybrid",
        "package": "hybrid.project_analysis",
        "models": "hybrid.project_analysis.models",
        "events": "hybrid.project_analysis.events",
        "team": "hybrid.project_analysis.team",
        "stubs": "hybrid.project_analysis.stubs",
    }
    loaded: dict[str, Any] = {}
    try:
        for key, module_name in module_names.items():
            loaded[key] = importlib.import_module(module_name)
    except ModuleNotFoundError:  # pragma: no cover - exercised when task is incomplete
        pytest.fail(
            "hybrid.project_analysis is not implemented yet; the hybrid foundation "
            "spec requires this package and its public re-exports."
        )
    return SimpleNamespace(**loaded)


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


def _public_model_classes(module: Any) -> list[tuple[str, type[BaseModel]]]:
    return [
        (name, value)
        for name, value in vars(module).items()
        if not name.startswith("_") and inspect.isclass(value) and issubclass(value, BaseModel)
    ]


def _find_model_field_any(model_cls: type[BaseModel], *keywords: str) -> str:
    for field_name in model_cls.model_fields:
        lower = field_name.lower()
        if any(keyword.lower() in lower for keyword in keywords):
            return field_name
    raise AssertionError(
        f"Could not find a field on {model_cls.__name__} matching any of {keywords}"
    )


def _find_model_by_field_specs(
    module: Any,
    field_specs: list[tuple[tuple[str, ...], Any]],
) -> tuple[str, type[BaseModel], BaseModel]:
    for name, model_cls in _public_model_classes(module):
        payload: dict[str, Any] = {}
        for keywords, value in field_specs:
            try:
                field_name = _find_model_field_any(model_cls, *keywords)
            except AssertionError:
                break
            payload[field_name] = value
        else:
            try:
                instance = model_cls.model_validate(payload)
            except Exception:  # noqa: BLE001
                continue
            return name, model_cls, instance
    raise AssertionError(
        f"Could not find a public model in {getattr(module, '__name__', module)!r} "
        f"matching field semantics {[keywords for keywords, _ in field_specs]}"
    )


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


def _payload_matches_any(value: Any, candidates: tuple[str, ...]) -> bool:
    return any(_contains_value(value, candidate) for candidate in candidates)


def _setup_tracing(service_name: str) -> tuple[Any, InMemorySpanExporter]:
    provider = TracingManager.setup(service_name, endpoint=None)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _make_message(
    topic: str,
    payload: dict[str, Any],
    *,
    trace_context: dict[str, Any] | None = None,
) -> Message:
    return Message(
        message_id="message-id",
        topic=topic,
        payload=payload,
        timestamp=datetime.now(timezone.utc),
        trace_context=trace_context or {},
        source_agent="test",
    )


def _find_topic_helper(events_module: Any) -> tuple[str, Any]:
    for name, value in vars(events_module).items():
        if name.startswith("_") or not callable(value) or inspect.isclass(value):
            continue
        try:
            topic = value("structure", "completed")
        except Exception:  # noqa: BLE001
            continue
        if isinstance(topic, str) and "structure" in topic.lower() and "completed" in topic.lower():
            return name, value
    raise AssertionError("Expected a per-team topic helper in hybrid.project_analysis.events")


def _find_topic_constant(events_module: Any) -> tuple[str, str]:
    for name, value in vars(events_module).items():
        if name.startswith("_") or not isinstance(value, str):
            continue
        return name, value
    raise AssertionError(
        "Expected a team-complete topic constant in hybrid.project_analysis.events"
    )


def _find_event_payload_model(events_module: Any) -> tuple[str, type[BaseModel]]:
    for name, value in vars(events_module).items():
        if name.startswith("_") or not inspect.isclass(value) or not issubclass(value, BaseModel):
            continue
        return name, value
    raise AssertionError("Expected a public event payload model in hybrid.project_analysis.events")


def _public_method_names(cls: type[Any]) -> list[str]:
    return [
        name for name, value in vars(cls).items() if callable(value) and not name.startswith("_")
    ]


def _make_task(trace_context: dict[str, Any] | None = None) -> AgentTask:
    return AgentTask(
        task_id="task-1",
        input_data={"input_path": "demo.py"},
        metadata={},
        trace_context=trace_context or {},
    )


def _invoke_agent_execute(agent: Any, task: AgentTask) -> Any:
    _, execute = _find_coroutine_method(agent, "execute")
    signature = inspect.signature(execute)
    if "task" in signature.parameters:
        return execute(task=task)
    if "agent_task" in signature.parameters:
        return execute(agent_task=task)
    return execute(task)


def _invoke_team_entry(team: Any, task: AgentTask) -> Any:
    for keyword in ("run", "execute"):
        try:
            _, entry = _find_coroutine_method(team, keyword)
        except AssertionError:
            continue
        signature = inspect.signature(entry)
        call_kwargs: dict[str, Any] = {}
        if "task" in signature.parameters:
            call_kwargs["task"] = task
        else:
            if "input_data" in signature.parameters:
                call_kwargs["input_data"] = task.input_data
            if "trace_context" in signature.parameters:
                call_kwargs["trace_context"] = task.trace_context
        if call_kwargs:
            return entry(**call_kwargs)
        return entry(task)
    raise AssertionError("Could not find a public async run or execute method on Team")


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
        fail: bool = False,
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
        self._fail = fail
        self.seen_trace_contexts: list[dict[str, Any]] = []

    @traced
    async def execute(self, task: AgentTask) -> AgentResult:
        self.seen_trace_contexts.append(task.trace_context or {})
        self._started.set()
        await self._release.wait()
        if self._fail:
            raise RuntimeError("boom")
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data=dict(self._canned_output),
            status="success",
            error=None,
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )


def test_public_api_reexports_from_package_root():
    modules = _load_hybrid_modules()

    _, team_cls = _find_public_member(modules.team, "team", kind="class")
    _, stub_cls = _find_public_member(modules.stubs, "stub", "agent", kind="class")
    _, make_stub_team = _find_public_member(modules.root, "stub", "team", kind="callable")
    topic_name, topic_value = _find_topic_constant(modules.events)
    payload_name, payload_cls = _find_event_payload_model(modules.events)

    assert getattr(modules.root, "Team") is team_cls
    assert getattr(modules.root, "StubAgent") is stub_cls
    assert getattr(modules.root, "make_stub_team") is make_stub_team
    assert getattr(modules.root, topic_name) == topic_value
    assert getattr(modules.root, payload_name) is payload_cls
    assert inspect.isclass(team_cls)
    assert inspect.isclass(stub_cls)


def test_domain_models_are_pydantic_data_containers():
    modules = _load_hybrid_modules()

    _, agent_model_cls, agent_model = _find_model_by_field_specs(
        modules.models,
        [
            (("agent",), "agent-1"),
            (("output", "result"), {"marker": "one"}),
        ],
    )
    _, team_model_cls, team_model = _find_model_by_field_specs(
        modules.models,
        [
            (("team",), "team-1"),
            (("result", "output"), {"marker": "merged"}),
        ],
    )

    for model_cls in (agent_model_cls, team_model_cls):
        assert issubclass(model_cls, BaseModel)
        assert _public_method_names(model_cls) == []

    assert _contains_value(agent_model, "agent-1")
    assert _contains_value(agent_model, "one")
    assert _contains_value(team_model, "team-1")
    assert _contains_value(team_model, "merged")


def test_event_vocab_exposes_topic_helper_and_payload_model():
    modules = _load_hybrid_modules()

    _topic_helper_name, topic_helper = _find_topic_helper(modules.events)
    topic = topic_helper("structure", "completed")
    assert isinstance(topic, str)
    assert "structure" in topic.lower()
    assert "completed" in topic.lower()

    _payload_name, payload_cls = _find_event_payload_model(modules.events)
    assert issubclass(payload_cls, BaseModel)


@pytest.mark.asyncio
async def test_stub_agent_returns_canned_output_without_llm_call_and_spans_execute(monkeypatch):
    modules = _load_hybrid_modules()
    provider, exporter = _setup_tracing("hybrid-stub-agent-tests")
    StubAgent = modules.stubs.StubAgent

    class _BlockedOpenAI:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("LLM call blocked")

    async def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("StubAgent must not call the LLM")

    monkeypatch.setattr(BaseAgent, "call_llm", _fail_if_called)
    core_agents_module = importlib.import_module("core.agents")
    if hasattr(core_agents_module, "AsyncOpenAI"):
        monkeypatch.setattr(core_agents_module, "AsyncOpenAI", _BlockedOpenAI, raising=False)

    stub_agent = _build_with_semantics(
        StubAgent,
        {
            "agent_id": "stub-1",
            "name": "Stub Agent",
            "model": "stub-model",
            "provider": "ollama",
            "canned_output": {"answer": "ok"},
        },
    )

    parent_tracer = provider.get_tracer("hybrid.tests")
    with parent_tracer.start_as_current_span("parent") as parent_span:
        task = _make_task(trace_context=inject_context())
        result = await _invoke_agent_execute(stub_agent, task)

    provider.force_flush()
    spans = exporter.get_finished_spans()
    child_spans = [
        span
        for span in spans
        if span.parent is not None
        and span.parent.trace_id == parent_span.get_span_context().trace_id
    ]

    assert child_spans
    assert any("execute" in span.name.lower() for span in child_spans)
    normalized = _normalize_mapping(result)
    assert _contains_value(normalized, "ok")

    failing_stub_agent = _build_with_semantics(
        StubAgent,
        {
            "agent_id": "stub-fail",
            "name": "Failing Stub Agent",
            "model": "stub-model",
            "provider": "ollama",
            "canned_output": {"answer": "ignored"},
            "fail": True,
        },
    )
    failing_result = await _invoke_agent_execute(failing_stub_agent, _make_task())
    failing_normalized = _normalize_mapping(failing_result)
    assert _contains_value(failing_normalized, "failure") or _contains_value(
        failing_normalized, "boom"
    )


@pytest.mark.asyncio
async def test_make_stub_team_builds_merging_team_from_agent_pairs():
    modules = _load_hybrid_modules()
    provider, _exporter = _setup_tracing("hybrid-make-stub-team-tests")
    bus = RecordingBus()
    event_store = RecordingEventStore()
    _, topic_value = _find_topic_constant(modules.events)

    team = _build_with_semantics(
        modules.root.make_stub_team,
        {
            "name": "structure",
            "agent_pairs": [
                ("structure-agent", {"structure": {"marker": "one"}}),
                ("dependencies-agent", {"dependencies": {"marker": "two"}}),
            ],
            "bus": bus,
            "event_store": event_store,
            "tracer_provider": provider,
        },
    )
    assert isinstance(team, modules.team.Team)

    task = _make_task(trace_context={})
    task.trace_context = inject_context()
    result = await asyncio.wait_for(_invoke_team_entry(team, task), timeout=1.0)

    merged = _normalize_mapping(result)
    assert _contains_value(merged, "one")
    assert _contains_value(merged, "two")
    assert len(event_store.appended) == 2
    assert len({stream for stream, _ in event_store.appended}) == 1
    assert all("structure" in stream.lower() for stream, _ in event_store.appended)
    appended_payloads = [event.data for _, event in event_store.appended]
    agent_signatures = [("structure-agent", "one"), ("dependencies-agent", "two")]
    assert (
        sum(_payload_matches_any(payload, agent_signatures[0]) for payload in appended_payloads)
        == 1
    )
    assert (
        sum(_payload_matches_any(payload, agent_signatures[1]) for payload in appended_payloads)
        == 1
    )
    assert all(
        sum(_payload_matches_any(payload, signature) for signature in agent_signatures) == 1
        for payload in appended_payloads
    )
    assert bus.published
    assert len(bus.published) == 1
    assert bus.published[0][0] == topic_value
    completion_payload = bus.published[0][1].payload
    _, payload_cls = _find_public_member(modules.events, "team", "complete", kind="class")
    validated_completion = payload_cls.model_validate(_normalize_mapping(completion_payload))
    assert _contains_value(completion_payload, "one")
    assert _contains_value(completion_payload, "two")
    assert _contains_value(validated_completion, "one")
    assert _contains_value(validated_completion, "two")


@pytest.mark.asyncio
async def test_team_runs_member_agents_concurrently_publishes_completion_and_propagates_trace_context():
    modules = _load_hybrid_modules()
    provider, exporter = _setup_tracing("hybrid-team-tests")
    bus = RecordingBus()
    event_store = RecordingEventStore()
    _, topic_value = _find_topic_constant(modules.events)
    started_one = asyncio.Event()
    started_two = asyncio.Event()
    release = asyncio.Event()
    trace_contexts_one: list[dict[str, Any]] = []
    trace_contexts_two: list[dict[str, Any]] = []

    class RecordingBarrierAgent(BarrierAgent):
        def __init__(self, *, recorder: list[dict[str, Any]], **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._recorder = recorder

        async def execute(self, task: AgentTask) -> AgentResult:
            self._recorder.append(task.trace_context or {})
            return await super().execute(task)

    agent_one = RecordingBarrierAgent(
        agent_id="structure-1",
        name="Structure One",
        canned_output={"structure": {"marker": "one"}},
        started=started_one,
        release=release,
        recorder=trace_contexts_one,
    )
    agent_two = RecordingBarrierAgent(
        agent_id="structure-2",
        name="Structure Two",
        canned_output={"dependencies": {"marker": "two"}},
        started=started_two,
        release=release,
        recorder=trace_contexts_two,
    )

    observed_outputs: list[Any] = []

    def merge_outputs(outputs: list[Any]) -> dict[str, Any]:
        observed_outputs.extend(outputs)
        merged: dict[str, Any] = {}
        for output in outputs:
            data = _normalize_mapping(output)
            payload = (
                data.get("output_data")
                if isinstance(data.get("output_data"), Mapping)
                else data.get("output")
                if isinstance(data.get("output"), Mapping)
                else data.get("result")
                if isinstance(data.get("result"), Mapping)
                else data
            )
            if isinstance(payload, Mapping):
                merged.update(payload)
        return merged

    team = _build_with_semantics(
        modules.team.Team,
        {
            "name": "structure",
            "agents": [agent_one, agent_two],
            "bus": bus,
            "event_store": event_store,
            "aggregator": merge_outputs,
            "tracer_provider": provider,
        },
    )

    parent_tracer = provider.get_tracer("hybrid.tests")
    task = _make_task()
    with parent_tracer.start_as_current_span("team-parent") as parent_span:
        task.trace_context = inject_context()
        run_task = asyncio.create_task(_invoke_team_entry(team, task))
        await asyncio.wait_for(started_one.wait(), timeout=1.0)
        await asyncio.wait_for(started_two.wait(), timeout=1.0)
        release.set()
        result = await asyncio.wait_for(run_task, timeout=1.0)

    provider.force_flush()
    spans = exporter.get_finished_spans()
    normalized = _normalize_mapping(result)

    assert _contains_value(normalized, "one")
    assert _contains_value(normalized, "two")
    assert len(observed_outputs) == 2
    assert len(event_store.appended) == 2
    assert len({stream for stream, _ in event_store.appended}) == 1
    assert all("structure" in stream.lower() for stream, _ in event_store.appended)
    appended_payloads = [event.data for _, event in event_store.appended]
    agent_signatures = [("structure-agent", "one"), ("dependencies-agent", "two")]
    assert (
        sum(_payload_matches_any(payload, agent_signatures[0]) for payload in appended_payloads)
        == 1
    )
    assert (
        sum(_payload_matches_any(payload, agent_signatures[1]) for payload in appended_payloads)
        == 1
    )
    assert all(
        sum(_payload_matches_any(payload, signature) for signature in agent_signatures) == 1
        for payload in appended_payloads
    )
    assert bus.published
    assert len(bus.published) == 1
    assert bus.published[0][0] == topic_value
    completion_payload = bus.published[0][1].payload
    assert _contains_value(completion_payload, "one")
    assert _contains_value(completion_payload, "two")
    assert trace_contexts_one and trace_contexts_two
    assert (
        extract_context(trace_contexts_one[0]).trace_id == parent_span.get_span_context().trace_id
    )
    assert (
        extract_context(trace_contexts_two[0]).trace_id == parent_span.get_span_context().trace_id
    )

    team_spans = [
        span
        for span in spans
        if span.parent is not None
        and span.parent.span_id == parent_span.get_span_context().span_id
        and any(
            child.parent is not None and child.parent.span_id == span.context.span_id
            for child in spans
        )
    ]
    assert team_spans
    team_span = team_spans[0]
    execute_children = [
        span
        for span in spans
        if span.parent is not None
        and span.parent.span_id == team_span.context.span_id
        and "execute" in span.name.lower()
    ]
    assert len(execute_children) == 2


@pytest.mark.asyncio
async def test_team_failure_surfaces_on_completion_message_without_raising():
    modules = _load_hybrid_modules()
    provider, _exporter = _setup_tracing("hybrid-team-failure-tests")
    bus = RecordingBus()
    event_store = RecordingEventStore()
    _, topic_value = _find_topic_constant(modules.events)
    started = asyncio.Event()
    release = asyncio.Event()
    observed_outputs: list[Any] = []

    success_agent = BarrierAgent(
        agent_id="security-1",
        name="Security One",
        canned_output={"security": {"marker": "ok"}},
        started=started,
        release=release,
    )
    failure_agent = BarrierAgent(
        agent_id="security-2",
        name="Security Two",
        canned_output={},
        started=asyncio.Event(),
        release=release,
        fail=True,
    )

    def merge_outputs(outputs: list[Any]) -> dict[str, Any]:
        observed_outputs.extend(outputs)
        merged: dict[str, Any] = {}
        for output in outputs:
            data = _normalize_mapping(output)
            payload = (
                data.get("output_data") if isinstance(data.get("output_data"), Mapping) else data
            )
            if isinstance(payload, Mapping):
                merged.update(payload)
        return merged

    team = _build_with_semantics(
        modules.team.Team,
        {
            "name": "security",
            "agents": [success_agent, failure_agent],
            "bus": bus,
            "event_store": event_store,
            "aggregator": merge_outputs,
            "tracer_provider": provider,
        },
    )

    task = _make_task()
    task.trace_context = inject_context()
    run_task = asyncio.create_task(_invoke_team_entry(team, task))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    release.set()
    result = await asyncio.wait_for(run_task, timeout=1.0)

    normalized = _normalize_mapping(result)
    assert _contains_value(normalized, "ok")
    assert len(observed_outputs) == 2
    assert any(
        _contains_value(output, "failure") or _contains_value(output, "boom")
        for output in observed_outputs
    )
    assert len(event_store.appended) == 2
    assert len({stream for stream, _ in event_store.appended}) == 1
    assert all("security" in stream.lower() for stream, _ in event_store.appended)
    appended_payloads = [event.data for _, event in event_store.appended]
    agent_signatures = [("security-1", "ok"), ("security-2", "boom", "failure")]
    assert (
        sum(_payload_matches_any(payload, agent_signatures[0]) for payload in appended_payloads)
        == 1
    )
    assert (
        sum(_payload_matches_any(payload, agent_signatures[1]) for payload in appended_payloads)
        == 1
    )
    assert all(
        sum(_payload_matches_any(payload, signature) for signature in agent_signatures) == 1
        for payload in appended_payloads
    )
    assert bus.published
    assert len(bus.published) == 1
    assert bus.published[0][0] == topic_value
    completion_payload = bus.published[0][1].payload
    _, payload_cls = _find_public_member(modules.events, "team", "complete", kind="class")
    validated_completion = payload_cls.model_validate(_normalize_mapping(completion_payload))
    assert _contains_value(completion_payload, "ok")
    assert _contains_value(completion_payload, "failure") or _contains_value(
        completion_payload, "boom"
    )
    assert _contains_value(validated_completion, "ok")
    assert _contains_value(validated_completion, "failure") or _contains_value(
        validated_completion, "boom"
    )
