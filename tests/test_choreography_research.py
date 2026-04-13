"""Tests for the choreography research spec."""

from __future__ import annotations

import asyncio
import inspect
import re
import runpy
import sys
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from copy import deepcopy
from typing import Any, Iterable, Sequence
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

import core.agents as core_agents
from core.agents import BaseAgent
from core.messaging import InMemoryBus, Message
from core.resilience import DeadLetterQueue
from core.state import Event, InMemoryEventStore

import choreography.research.event_log as research_event_log
import choreography.research.events as research_events
import choreography.research.agents as research_agents
import choreography.research.runner as research_runner


def _required_fields(model: type[BaseModel], names: Iterable[str]) -> None:
    """Assert that the given fields are declared on the Pydantic model."""
    field_names = set(model.model_fields)
    missing = [name for name in names if name not in field_names]
    assert not missing, f"{model.__name__} is missing fields: {', '.join(missing)}"


def _build_event(
    *,
    event_type: str,
    research_id: str,
    timestamp: datetime,
    trace_context: dict[str, str],
    data: dict[str, object],
) -> Event:
    return Event(
        event_id=str(uuid4()),
        stream=f"research:{research_id}",
        event_type=event_type,
        data=data,
        timestamp=timestamp,
        trace_context=trace_context,
    )


def _snake_topic(event_type: str) -> str:
    """Convert CamelCase event types into choreography research topic strings."""
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", event_type).lower()
    return f"choreography.research.{snake}"


def _build_event_payload(
    *,
    event_type: str,
    research_id: str = "research-123",
    timestamp: datetime | None = None,
    trace_context: dict[str, str] | None = None,
) -> dict[str, object]:
    timestamp = timestamp or datetime.now(timezone.utc)
    trace_context = trace_context or {"trace_id": "trace-test", "span_id": "span-test"}
    return {
        "message_id": f"{event_type.lower()}",
        "topic": _snake_topic(event_type),
        "payload": {},
        "timestamp": timestamp,
        "trace_context": trace_context,
        "research_id": research_id,
        "event_type": event_type,
    }


def _build_research_requested_message(
    *,
    research_topic: str = "AI choreography",
    scope: str = "global",
    deadline: datetime | None = None,
    research_id: str = "research-123",
    timestamp: datetime | None = None,
    trace_context: dict[str, str] | None = None,
) -> dict[str, object]:
    payload = _build_event_payload(
        event_type="ResearchRequested",
        research_id=research_id,
        timestamp=timestamp,
        trace_context=trace_context,
    )
    payload["payload"] = {
        "topic": research_topic,
        "scope": scope,
        "deadline": deadline,
    }
    return payload


def _build_finding_payload(
    *,
    source_type: str,
    research_id: str = "research-123",
    finding_id: str | None = None,
    timestamp: datetime | None = None,
    trace_context: dict[str, str] | None = None,
    extra_fields: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = _build_event_payload(
        event_type="FindingDiscovered",
        research_id=research_id,
        timestamp=timestamp,
        trace_context=trace_context,
    )
    payload.update(
        {
            "finding_id": finding_id or f"{source_type}-finding",
            "source_type": source_type,
            "title": f"{source_type} insight",
            "summary": "summary text",
            "url": f"https://example.com/{source_type}",
            "relevance_score": 0.85,
            "raw_content": "raw text",
        }
    )
    if extra_fields:
        payload.update(extra_fields)
    return payload


def _build_research_complete_payload(
    *,
    research_id: str = "research-123",
    summary: str = "Structured brief",
    timestamp: datetime | None = None,
    trace_context: dict[str, str] | None = None,
) -> dict[str, object]:
    payload = _build_event_payload(
        event_type="ResearchComplete",
        research_id=research_id,
        timestamp=timestamp,
        trace_context=trace_context,
    )
    payload["brief"] = {
        "topic": "AI choreography",
        "summary": summary,
        "key_findings": [
            {"finding_id": "f1", "source_type": "web"},
        ],
        "cross_references": [
            {
                "finding_a_id": "f1",
                "finding_b_id": "f2",
                "relationship": "corroborates",
                "explanation": "Cross-source alignment",
            }
        ],
        "sources_consulted": {"web": 1, "academic": 1, "code": 1, "news": 1},
        "confidence_score": 0.91,
    }
    return payload


def _collect_finding_identifiers(events: Sequence[Event]) -> dict[str, str]:
    """Map every known identifier for a finding event back to its source_type."""
    identifier_map: dict[str, str] = {}
    for event in events:
        if event.event_type != "FindingDiscovered":
            continue
        source_type = event.data.get("source_type")
        if not source_type:
            continue
        candidate_ids: set[str] = {event.event_id}
        for key in ("finding_id", "message_id"):
            candidate = event.data.get(key)
            if isinstance(candidate, str) and candidate:
                candidate_ids.add(candidate)
        for identifier in candidate_ids:
            identifier_map.setdefault(identifier, source_type)
    return identifier_map


def _get_research_complete_event(
    timeline: research_event_log.ResearchTimeline,
) -> Event:
    for event in reversed(timeline.events):
        if event.event_type == "ResearchComplete":
            return event
    raise AssertionError("No ResearchComplete event found in timeline")


def _instantiate_with_candidates(
    cls: type[Any],
    **candidate_kwargs: Any,
) -> Any:
    signature = inspect.signature(cls)
    kwargs: dict[str, Any] = {}
    for name, param in signature.parameters.items():
        if name == "self" or param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if name in candidate_kwargs:
            kwargs[name] = candidate_kwargs[name]
        elif param.default is inspect._empty:
            raise AssertionError(f"Cannot instantiate {cls.__name__} without '{name}'")
    return cls(**kwargs)


async def _build_sample_choreography_timeline(
    monkeypatch,
) -> tuple[research_event_log.ResearchTimeline, dict[str, object]]:
    research_id = "research-123"
    store = InMemoryEventStore()
    monkeypatch.setattr(research_event_log, "EVENT_STORE", store)

    base_time = datetime(2026, 4, 12, 12, 0, 0, tzinfo=timezone.utc)
    trace_context = {"trace_id": "trace-abc", "span_id": "span-root"}
    deadline = base_time + timedelta(hours=1)

    stream_name = f"research:{research_id}"
    await store.append(
        stream_name,
        _build_event(
            event_type="ResearchRequested",
            research_id=research_id,
            timestamp=base_time,
            trace_context=trace_context,
            data={
                "topic": "AI agents",
                "scope": "global",
                "deadline": deadline.isoformat(),
            },
        ),
    )

    sources = ("web", "academic", "code", "news")
    finding_ids: list[str] = []
    finding_source_map: dict[str, str] = {}
    finding_timestamp = base_time + timedelta(seconds=1)
    for source_type in sources:
        finding_id = f"finding-{source_type}"
        finding_ids.append(finding_id)
        finding_source_map[finding_id] = source_type
        await store.append(
            stream_name,
            _build_event(
                event_type="FindingDiscovered",
                research_id=research_id,
                timestamp=finding_timestamp,
                trace_context=trace_context,
                data={
                    "finding_id": finding_id,
                    "source_type": source_type,
                    "title": f"{source_type} insight",
                    "summary": "Summary text",
                    "url": f"https://example.com/{source_type}",
                    "relevance_score": 0.9,
                    "raw_content": "raw text",
                },
            ),
        )

    await store.append(
        stream_name,
        _build_event(
            event_type="CrossReferenceFound",
            research_id=research_id,
            timestamp=finding_timestamp + timedelta(milliseconds=500),
            trace_context=trace_context,
            data={
                "finding_a_id": finding_ids[0],
                "finding_b_id": finding_ids[1],
                "relationship": "corroborates",
                "explanation": "Both sources report rising adoption.",
            },
        ),
    )

    for source_type in sources:
        await store.append(
            stream_name,
            _build_event(
                event_type="SourceExhausted",
                research_id=research_id,
                timestamp=finding_timestamp + timedelta(seconds=2),
                trace_context=trace_context,
                data={"source_type": source_type},
            ),
        )

    await store.append(
        stream_name,
        _build_event(
            event_type="AgentError",
            research_id=research_id,
            timestamp=finding_timestamp + timedelta(seconds=0.7),
            trace_context=trace_context,
            data={
                "agent_id": "news-search",
                "error": "timeout",
                "topic": "AI agents",
            },
        ),
    )

    await store.append(
        stream_name,
        _build_event(
            event_type="ResearchComplete",
            research_id=research_id,
            timestamp=finding_timestamp + timedelta(seconds=4),
            trace_context=trace_context,
            data={
                "brief": {
                    "topic": "AI agents",
                    "summary": "Partial brief due to data source failure.",
                    "key_findings": [
                        {"finding_id": finding_ids[0], "source_type": "web"},
                        {"finding_id": finding_ids[1], "source_type": "academic"},
                    ],
                    "cross_references": [
                        {
                            "finding_a_id": finding_ids[0],
                            "finding_b_id": finding_ids[1],
                            "relationship": "corroborates",
                            "explanation": "Combined observation.",
                        }
                    ],
                    "sources_consulted": {
                        "web": 2,
                        "academic": 1,
                        "code": 1,
                        "news": 0,
                    },
                    "confidence_score": 0.72,
                }
            },
        ),
    )

    timeline = await research_event_log.reconstruct_timeline(research_id)
    return timeline, {
        "sources": sources,
        "trace_context": trace_context,
    }


def _patch_fake_llm(monkeypatch, *, response_content: str = "simulated summary") -> None:
    """Stub out AsyncOpenAI so agents never hit a real LLM endpoint."""

    class _FakeChatResponse:
        def __init__(self, *, model: str, content: str) -> None:
            self.choices = [
                SimpleNamespace(message=SimpleNamespace(role="assistant", content=content))
            ]
            self.usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0)
            self.model = model

    class _FakeClient:
        def __init__(self, **config: Any) -> None:
            self.config = dict(config)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        async def _create(self, **kwargs: Any) -> _FakeChatResponse:
            return _FakeChatResponse(
                model=kwargs.get("model", "stub-model"),
                content=response_content,
            )

    monkeypatch.setattr(core_agents, "AsyncOpenAI", _FakeClient, raising=False)
    if hasattr(core_agents, "openai"):
        monkeypatch.setattr(core_agents.openai, "AsyncOpenAI", _FakeClient, raising=False)


async def _run_research_runner(
    monkeypatch,
    *,
    fail_agent: type[BaseAgent] | None = None,
) -> tuple[
    research_event_log.ResearchTimeline,
    DeadLetterQueue,
]:
    """Execute the runner with deterministic agents so CLI/public tests can introspect the timeline."""
    _patch_fake_llm(monkeypatch)
    bus = InMemoryBus()
    store = InMemoryEventStore()
    monkeypatch.setattr(research_event_log, "EVENT_STORE", store)
    dlq = DeadLetterQueue(bus=bus)

    runner = research_runner.ResearchRunner(
        bus=bus,
        event_store=store,
        dead_letter_queue=dlq,
    )

    if fail_agent is not None:
        monkeypatch.setattr(
            fail_agent,
            "execute",
            AsyncMock(side_effect=RuntimeError("simulated failure")),
        )

    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    await runner.run(
        topic="AI choreography",
        scope="global",
        deadline=deadline,
    )
    streams = list(store._streams.keys())
    assert streams, "No research stream was recorded during the run"
    stream_name = streams[0]
    research_id = stream_name.split(":", 1)[-1]
    timeline = await research_event_log.reconstruct_timeline(research_id)
    return timeline, dlq


def test_event_models_expose_core_fields_and_provenance() -> None:
    """Every choreography event is a Message + BaseModel with core strings."""
    event_models = [
        research_events.ResearchRequested,
        research_events.FindingDiscovered,
        research_events.CrossReferenceFound,
        research_events.SourceExhausted,
        research_events.ResearchComplete,
        research_events.AgentError,
    ]

    for model in event_models:
        assert issubclass(model, BaseModel), f"{model.__name__} must be a Pydantic model"
        assert issubclass(model, Message), f"{model.__name__} must extend core.messaging.Message"
        _required_fields(model, ("research_id", "event_type", "timestamp", "trace_context"))

    _required_fields(
        research_events.FindingDiscovered,
        (
            "source_type",
            "title",
            "summary",
            "url",
            "relevance_score",
            "raw_content",
            "authors",
            "year",
            "repository",
            "language",
            "published_date",
        ),
    )
    _required_fields(
        research_events.ResearchRequested,
        ("topic", "scope", "deadline"),
    )
    _required_fields(
        research_events.ResearchComplete,
        ("brief",),
    )
    _required_fields(
        research_events.SourceExhausted,
        ("source_type",),
    )
    _required_fields(
        research_events.CrossReferenceFound,
        ("finding_a_id", "finding_b_id", "relationship", "explanation"),
    )
    _required_fields(
        research_events.AgentError,
        ("agent_id", "error"),
    )


@pytest.mark.asyncio
async def test_reconstruct_timeline_tracks_parallel_findings_cross_refs_and_trace(
    monkeypatch,
) -> None:
    """Event sourcing recreates a full, concurrent research narrative."""
    timeline, context = await _build_sample_choreography_timeline(monkeypatch)
    sources = context["sources"]
    trace_context = context["trace_context"]

    assert timeline.events, "Timeline should include every persisted event"
    request_events = [event for event in timeline.events if event.event_type == "ResearchRequested"]
    assert len(request_events) == 1
    assert request_events[0].event_type == "ResearchRequested"
    assert timeline.events[-1].event_type == "ResearchComplete"
    assert timeline.duration_ms >= 4000
    assert any(event.event_type == "AgentError" for event in timeline.events)

    finding_events = [event for event in timeline.events if event.event_type == "FindingDiscovered"]
    assert len(finding_events) >= 3
    timestamps = {event.timestamp for event in finding_events}
    assert len(timestamps) == 1, "At least three search agents should report findings concurrently"

    assert set(timeline.findings_by_source) == set(sources)
    for source in sources:
        assert timeline.findings_by_source[source], f"{source} should contribute findings"

    finding_events = [event for event in timeline.events if event.event_type == "FindingDiscovered"]
    identifier_map = _collect_finding_identifiers(finding_events)
    assert timeline.cross_references, "CrossReferenceAgent must publish correlations"
    cross_ref = timeline.cross_references[0]
    assert cross_ref["finding_a_id"] in identifier_map
    assert cross_ref["finding_b_id"] in identifier_map
    assert identifier_map[cross_ref["finding_a_id"]] != identifier_map[cross_ref["finding_b_id"]]

    source_exhausted_events = [
        event for event in timeline.events if event.event_type == "SourceExhausted"
    ]
    assert {event.data["source_type"] for event in source_exhausted_events} == set(sources)
    last_source_exhausted = max(event.timestamp for event in source_exhausted_events)
    assert timeline.events[-1].timestamp > last_source_exhausted

    for event in timeline.events:
        assert event.trace_context.get("trace_id") == trace_context["trace_id"]

    brief_data = timeline.events[-1].data.get("brief", {})
    assert "sources_consulted" in brief_data
    consulted = brief_data["sources_consulted"]
    assert set(consulted) >= set(sources)


@pytest.mark.asyncio
async def test_cross_reference_agent_links_distinct_sources(monkeypatch) -> None:
    timeline, context = await _build_sample_choreography_timeline(monkeypatch)
    finding_events = [event for event in timeline.events if event.event_type == "FindingDiscovered"]
    identifier_map = _collect_finding_identifiers(finding_events)
    assert timeline.cross_references, "CrossReferenceAgent must publish correlations"
    assert any(
        ref["finding_a_id"] in identifier_map
        and ref["finding_b_id"] in identifier_map
        and identifier_map[ref["finding_a_id"]] != identifier_map[ref["finding_b_id"]]
        for ref in timeline.cross_references
    )


@pytest.mark.asyncio
async def test_aggregator_completes_after_every_source_is_exhausted(
    monkeypatch,
) -> None:
    timeline, context = await _build_sample_choreography_timeline(monkeypatch)
    sources = context["sources"]
    source_exhausted_events = [
        event for event in timeline.events if event.event_type == "SourceExhausted"
    ]
    assert len(source_exhausted_events) == len(sources)
    assert {event.data["source_type"] for event in source_exhausted_events} == set(sources)
    completion_events = [
        event for event in timeline.events if event.event_type == "ResearchComplete"
    ]
    assert len(completion_events) == 1
    completion_event = completion_events[0]
    assert completion_event.timestamp > max(event.timestamp for event in source_exhausted_events)
    assert timeline.events[-1] is completion_event


def test_event_models_raise_validation_error_for_missing_fields() -> None:
    base_time = datetime.now(timezone.utc)
    request_payload = _build_research_requested_message(
        research_topic="AI review",
        research_id="research-123",
        timestamp=base_time,
        trace_context={"trace_id": "trace-err"},
    )

    missing_id = deepcopy(request_payload)
    missing_id.pop("research_id")
    with pytest.raises(ValidationError):
        research_events.ResearchRequested.model_validate(missing_id)

    missing_topic = deepcopy(request_payload)
    missing_topic_payload = dict(missing_topic["payload"])
    missing_topic_payload.pop("topic")
    missing_topic["payload"] = missing_topic_payload
    with pytest.raises(ValidationError):
        research_events.ResearchRequested.model_validate(missing_topic)

    with pytest.raises(ValidationError):
        research_events.FindingDiscovered.model_validate(
            {
                "message_id": "missing-source-type",
                "topic": _snake_topic("FindingDiscovered"),
                "payload": {},
                "timestamp": base_time,
                "trace_context": {"trace_id": "trace-err"},
                "event_type": "FindingDiscovered",
                "research_id": "research-123",
                "finding_id": "finding-web",
                "title": "Title",
                "summary": "Summary",
                "url": "https://example.com",
                "relevance_score": 0.7,
                "raw_content": "raw text",
            }
        )
    with pytest.raises(ValidationError):
        research_events.SourceExhausted.model_validate(
            _build_event_payload(event_type="SourceExhausted")
        )

    cross_ref_payload = _build_event_payload(event_type="CrossReferenceFound")
    cross_ref_payload.update(
        {
            "finding_a_id": "f1",
            "finding_b_id": "f2",
            "relationship": "corroborates",
            "explanation": "Aligned coverage",
        }
    )
    for field in ("finding_a_id", "finding_b_id", "relationship", "explanation"):
        with pytest.raises(ValidationError):
            invalid = dict(cross_ref_payload)
            invalid.pop(field)
            research_events.CrossReferenceFound.model_validate(invalid)

    agent_error_payload = _build_event_payload(event_type="AgentError")
    agent_error_payload.update(
        {
            "agent_id": "web-search",
            "error": "timeout",
        }
    )
    for field in ("agent_id", "error"):
        with pytest.raises(ValidationError):
            invalid = dict(agent_error_payload)
            invalid.pop(field)
            research_events.AgentError.model_validate(invalid)


@pytest.mark.parametrize(
    "source_type,extra_fields,required_fields",
    [
        (
            "academic",
            {"authors": ["Dr. Research"], "year": 2024},
            ("authors", "year"),
        ),
        (
            "code",
            {"repository": "https://example.com/repo", "language": "python"},
            ("repository", "language"),
        ),
        (
            "news",
            {"published_date": datetime(2026, 4, 12, tzinfo=timezone.utc)},
            ("published_date",),
        ),
    ],
)
def test_finding_discovered_requires_source_specific_metadata(
    source_type,
    extra_fields,
    required_fields,
) -> None:
    payload = _build_finding_payload(source_type=source_type, extra_fields=extra_fields)
    event = research_events.FindingDiscovered.model_validate(deepcopy(payload))
    for field in required_fields:
        assert getattr(event, field) is not None

    for field in required_fields:
        invalid_payload = deepcopy(payload)
        invalid_payload.pop(field)
        with pytest.raises(ValidationError):
            research_events.FindingDiscovered.model_validate(invalid_payload)


def test_research_complete_brief_requires_all_fields() -> None:
    payload = _build_research_complete_payload()
    event = research_events.ResearchComplete.model_validate(payload)
    brief = event.brief
    required_fields = (
        "topic",
        "summary",
        "key_findings",
        "cross_references",
        "sources_consulted",
        "confidence_score",
    )
    for field in required_fields:
        value = brief.get(field) if isinstance(brief, dict) else getattr(brief, field, None)
        assert value is not None

    for field in required_fields:
        invalid_payload = deepcopy(payload)
        invalid_payload["brief"].pop(field)
        with pytest.raises(ValidationError):
            research_events.ResearchComplete.model_validate(invalid_payload)


@pytest.mark.asyncio
async def test_dlq_monitor_agent_receives_agent_error_events() -> None:
    bus = InMemoryBus()
    dlq = DeadLetterQueue(bus=bus)
    errors: list[research_events.AgentError] = []

    async def monitor(message: research_events.AgentError) -> None:
        errors.append(message)

    await bus.subscribe(_snake_topic("AgentError"), monitor)

    agent_error = research_events.AgentError.model_validate(
        {
            "message_id": "agent-error",
            "topic": _snake_topic("AgentError"),
            "payload": {"research_id": "research-123"},
            "timestamp": datetime.now(timezone.utc),
            "trace_context": {"trace_id": "trace-dlq"},
            "research_id": "research-123",
            "event_type": "AgentError",
            "agent_id": "web-search",
            "error": "timeout",
        }
    )

    await dlq.send(agent_error, error="timeout", source="web-search")
    failed = await dlq.list_failed()
    assert failed and failed[0].original_message == agent_error

    await bus.publish(agent_error.topic, agent_error)
    await asyncio.sleep(0)
    assert errors
    assert errors[-1].agent_id == "web-search"
    assert errors[-1].error == "timeout"


@pytest.mark.asyncio
async def test_dlq_monitor_agent_logs_agent_error_context(monkeypatch) -> None:
    bus = InMemoryBus()
    dlq = DeadLetterQueue(bus=bus)
    store = InMemoryEventStore()
    subscriptions: list[str] = []

    original_subscribe = bus.subscribe

    async def recording_subscribe(topic: str, handler):
        subscriptions.append(topic)
        return await original_subscribe(topic, handler)

    monkeypatch.setattr(bus, "subscribe", recording_subscribe)

    class _CapturingLogger:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []

        def error(self, message: str, **kwargs: object) -> None:
            self.records.append({"message": message, "kwargs": kwargs})

    logger = _CapturingLogger()
    candidate_kwargs = {
        "bus": bus,
        "message_bus": bus,
        "event_bus": bus,
        "dead_letter_queue": dlq,
        "dlq": dlq,
        "logger": logger,
        "log": logger,
        "agent_id": "dlq-monitor",
        "name": "DLQ Monitor Agent",
        "model": "dlq-monitor",
        "provider": "ollama",
        "tools": [],
        "system_prompt": "Monitor DLQ",
        "event_store": store,
    }
    monitor = _instantiate_with_candidates(research_agents.DLQMonitorAgent, **candidate_kwargs)

    start = getattr(monitor, "start", None)
    if callable(start):
        start_result = start()
        if inspect.isawaitable(start_result):
            await start_result

    agent_error_payload = _build_event_payload(event_type="AgentError")
    agent_error_payload.update(
        {
            "agent_id": "news-search",
            "error": "timeout",
            "payload": {"research_id": "research-456"},
        }
    )
    agent_error = research_events.AgentError.model_validate(agent_error_payload)

    await bus.publish(agent_error.topic, agent_error)
    await asyncio.sleep(0)

    assert _snake_topic("AgentError") in subscriptions
    assert logger.records
    assert any(
        agent_error.agent_id in str(record["kwargs"]) and agent_error.error in str(record["kwargs"])
        for record in logger.records
    )

    stop = getattr(monitor, "stop", None)
    if callable(stop):
        stop_result = stop()
        if inspect.isawaitable(stop_result):
            await stop_result


@pytest.mark.asyncio
async def test_dead_letter_queue_records_agent_errors_and_retries() -> None:
    bus = InMemoryBus()
    dlq = DeadLetterQueue(bus=bus)
    received: list[Message] = []

    async def handler(message: Message) -> None:
        received.append(message)

    subscription = await bus.subscribe(_snake_topic("AgentError"), handler)

    message = Message(
        message_id="agent-error",
        topic=_snake_topic("AgentError"),
        payload={"research_id": "research-123", "agent": "news-agent"},
        timestamp=datetime.now(timezone.utc),
        trace_context={"trace_id": "trace-abc"},
    )

    await dlq.send(message, error="timeout", source="NewsSearchAgent")
    failed = await dlq.list_failed()
    assert len(failed) == 1
    dead_letter = failed[0]
    assert dead_letter.error == "timeout"
    assert dead_letter.original_message == message

    success = await dlq.retry(dead_letter.id)
    assert success
    await asyncio.sleep(0)
    assert received and received[-1] == message

    extra_message = Message(
        message_id="agent-error-2",
        topic=_snake_topic("AgentError"),
        payload={"research_id": "research-123", "agent": "code-agent"},
        timestamp=datetime.now(timezone.utc),
        trace_context={"trace_id": "trace-abc"},
    )

    await bus.publish(extra_message.topic, extra_message)
    await asyncio.sleep(0)
    assert received[-1] == extra_message

    with suppress(Exception):
        await bus.unsubscribe(subscription)


@pytest.mark.asyncio
async def test_research_runner_builds_choreography_timeline(monkeypatch) -> None:
    timeline, dlq = await _run_research_runner(monkeypatch)

    assert await dlq.list_failed() == []

    request_events = [event for event in timeline.events if event.event_type == "ResearchRequested"]
    assert len(request_events) == 1
    request_event = request_events[0]
    research_topic_value = request_event.data.get("topic")
    assert research_topic_value == "AI choreography"
    assert research_topic_value != _snake_topic("ResearchRequested")
    completion_event = _get_research_complete_event(timeline)
    assert timeline.events[-1] is completion_event
    assert completion_event.event_type == "ResearchComplete"
    # Timeline events are stored as core.state.Event; validate the payload separately.
    research_events.ResearchComplete.model_validate(completion_event.data)

    finding_events = [event for event in timeline.events if event.event_type == "FindingDiscovered"]
    assert len(finding_events) >= 3
    sorted_timestamps = sorted(event.timestamp for event in finding_events)
    assert any(
        sorted_timestamps[i + 2] - sorted_timestamps[i] <= timedelta(milliseconds=150)
        for i in range(len(sorted_timestamps) - 2)
    ), "At least three search agents should overlap in time"

    sources = {"web", "academic", "code", "news"}
    source_exhausted = [event for event in timeline.events if event.event_type == "SourceExhausted"]
    assert {event.data["source_type"] for event in source_exhausted} >= sources
    final_event = completion_event
    assert final_event.timestamp > max(event.timestamp for event in source_exhausted)

    trace_id = request_events[0].trace_context["trace_id"]
    for event in timeline.events:
        assert event.trace_context.get("trace_id") == trace_id

    identifier_map = _collect_finding_identifiers(finding_events)
    assert timeline.cross_references, "CrossReferenceAgent must publish correlations"
    assert any(
        ref["finding_a_id"] in identifier_map
        and ref["finding_b_id"] in identifier_map
        and identifier_map[ref["finding_a_id"]] != identifier_map[ref["finding_b_id"]]
        for ref in timeline.cross_references
    )

    brief_data = final_event.data.get("brief", {})
    assert brief_data.get("sources_consulted", {}).keys() >= sources


@pytest.mark.asyncio
async def test_research_runner_handles_agent_failure_and_records_dlq(monkeypatch) -> None:
    timeline, dlq = await _run_research_runner(
        monkeypatch, fail_agent=research_agents.NewsSearchAgent
    )

    agent_errors = [event for event in timeline.events if event.event_type == "AgentError"]
    assert agent_errors, "AgentError must be emitted when an agent fails"
    failed_agent_id = agent_errors[0].data.get("agent_id")
    assert failed_agent_id, "Agent errors should include the failing agent's id"
    assert await dlq.list_failed(), "The DLQ should record failed research events"

    completion_event = _get_research_complete_event(timeline)
    final_event = completion_event
    brief_data = final_event.data.get("brief", {})
    assert brief_data.get("sources_consulted", {}).get("news", 0) == 0
    assert completion_event.event_type == "ResearchComplete"
    assert final_event.timestamp > max(
        event.timestamp for event in timeline.events if event.event_type == "SourceExhausted"
    )


def _build_completion_event(
    research_id: str,
    *,
    summary: str,
) -> research_events.ResearchComplete:
    return research_events.ResearchComplete.model_validate(
        {
            "message_id": "research-complete",
            "topic": _snake_topic("ResearchComplete"),
            "payload": {},
            "timestamp": datetime.now(timezone.utc),
            "trace_context": {"trace_id": "trace-cli", "span_id": "span-cli"},
            "research_id": research_id,
            "event_type": "ResearchComplete",
            "brief": {
                "topic": "AI choreography",
                "summary": summary,
                "key_findings": [
                    {"finding_id": "f1", "source_type": "web"},
                ],
                "cross_references": [],
                "sources_consulted": {
                    "web": 1,
                    "academic": 1,
                    "code": 1,
                    "news": 1,
                },
                "confidence_score": 0.96,
            },
        }
    )


def _invoke_research_cli(
    monkeypatch,
    capsys,
    *,
    runner_result: research_events.ResearchComplete | Exception,
    argv: Sequence[str] | None = None,
) -> tuple[int, str]:
    class DummyRunner:
        async def run(self, topic: str, scope: str, deadline: datetime | None):
            if isinstance(runner_result, Exception):
                raise runner_result
            return runner_result

    monkeypatch.setattr(research_runner, "ResearchRunner", DummyRunner)
    monkeypatch.setattr(sys, "argv", ["choreography.research"] + (argv or ["AI agents"]))

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("choreography.research", run_name="__main__")

    output = capsys.readouterr().out
    return exit_info.value.code, output


def test_research_cli_prints_brief_and_exits_successfully(monkeypatch, capsys) -> None:
    research_id = "cli-run-success"
    completion_event = _build_completion_event(research_id, summary="CLI structured summary")
    exit_code, output = _invoke_research_cli(
        monkeypatch,
        capsys,
        runner_result=completion_event,
        argv=["AI briefing"],
    )

    assert exit_code == 0
    assert "cli structured summary" in output.lower()


def test_research_cli_exits_nonzero_on_failure(monkeypatch, capsys) -> None:
    failure = RuntimeError("pipeline aborted")
    exit_code, output = _invoke_research_cli(
        monkeypatch,
        capsys,
        runner_result=failure,
        argv=["AI briefing"],
    )

    assert exit_code == 1
    assert "pipeline aborted" in output.lower()


def test_initiator_agent_has_no_direct_references_to_other_agents() -> None:
    """Choreography guarantee: InitiatorAgent does not couple to other agent classes.

    Spec §Constraints 1: "The InitiatorAgent publishes one event and is done.
    All other agents react to events autonomously. No agent calls another agent directly."
    Structural check — verifies the class source has no mention of sibling agent classes,
    enforcing that coordination can only happen via the bus.
    """
    initiator_source = inspect.getsource(research_agents.InitiatorAgent)
    for forbidden in (
        "WebSearchAgent",
        "AcademicSearchAgent",
        "CodeAnalysisAgent",
        "NewsSearchAgent",
        "CrossReferenceAgent",
        "AggregatorAgent",
    ):
        assert forbidden not in initiator_source, (
            f"InitiatorAgent references {forbidden} directly — violates choreography "
            "(agents must coordinate only through the bus, not direct calls)."
        )


@pytest.mark.asyncio
async def _test_initiator_publishes_research_requested_as_first_event(monkeypatch) -> None:
    """The runner flow starts with exactly one ResearchRequested publish from the initiator.

    Spec §Constraints 1: initiator fires-and-forgets a single ResearchRequested event;
    all subsequent events are produced by other agents reacting to the bus.
    """
    timeline, _dlq, _completion = await _run_research_runner(monkeypatch)
    assert timeline.events, "runner produced no events"
    first = timeline.events[0]
    assert first.event_type == "ResearchRequested", (
        f"First timeline event must be ResearchRequested; got {first.event_type}"
    )
    # Only one ResearchRequested should ever be published per research_id.
    requested_count = sum(1 for event in timeline.events if event.event_type == "ResearchRequested")
    assert requested_count == 1, (
        f"ResearchRequested must be published exactly once; found {requested_count}"
    )


@pytest.mark.asyncio
async def test_initiator_agent_is_fire_and_forget(monkeypatch) -> None:
    timeline, _dlq = await _run_research_runner(monkeypatch)
    assert timeline.events, "runner produced no events"
    first = timeline.events[0]
    assert first.event_type == "ResearchRequested", (
        f"First timeline event must be ResearchRequested; got {first.event_type}"
    )
    requested_count = sum(1 for event in timeline.events if event.event_type == "ResearchRequested")
    assert requested_count == 1, (
        f"ResearchRequested must be published exactly once; found {requested_count}"
    )


def test_research_brief_is_pydantic_model_with_required_fields() -> None:
    """Spec §3.7: ResearchBrief is a typed model, not a bare dict.

    Fields: topic, summary, key_findings, cross_references, sources_consulted, confidence_score.
    """
    brief_model = getattr(research_events, "ResearchBrief", None)
    assert brief_model is not None, (
        "choreography.research.events must expose a ResearchBrief model (spec §3.7)"
    )
    assert issubclass(brief_model, BaseModel), "ResearchBrief must be a Pydantic BaseModel"

    _required_fields(
        brief_model,
        (
            "topic",
            "summary",
            "key_findings",
            "cross_references",
            "sources_consulted",
            "confidence_score",
        ),
    )

    # ResearchComplete.brief must be typed as ResearchBrief (not dict / Any).
    brief_field = research_events.ResearchComplete.model_fields["brief"]
    annotation = brief_field.annotation
    assert annotation is brief_model, (
        f"ResearchComplete.brief must be typed as ResearchBrief, got {annotation!r}"
    )
