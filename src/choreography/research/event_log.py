"""Event sourcing utilities for choreography research."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from core.state import Event, EventStore, InMemoryEventStore

__all__ = ["EVENT_STORE", "ResearchTimeline", "reconstruct_timeline"]

EVENT_STORE: EventStore = InMemoryEventStore()


@dataclass(slots=True)
class ResearchTimeline:
    research_id: str
    events: list[Event]
    findings_by_source: dict[str, list[dict[str, Any]]]
    cross_references: list[dict[str, Any]]
    duration_ms: float


async def reconstruct_timeline(
    research_id: str,
    *,
    event_store: EventStore | None = None,
) -> ResearchTimeline:
    """Replay all events for the given research id."""

    store = event_store or EVENT_STORE
    stream = f"research:{research_id}"
    events = await store.read(stream)
    # Use append order as the primary key so the initiator's ResearchRequested
    # event always anchors the timeline even if later events reuse earlier
    # timestamps (e.g., started_at fields on findings).
    events.sort(key=lambda event: (event.sequence, event.timestamp))
    findings_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cross_references: list[dict[str, Any]] = []

    for event in events:
        if event.event_type == "FindingDiscovered":
            source = str(event.data.get("source_type", "unknown"))
            findings_by_source[source].append(dict(event.data))
        elif event.event_type == "CrossReferenceFound":
            cross_references.append(dict(event.data))

    duration_ms = _calculate_duration(events)
    return ResearchTimeline(
        research_id=research_id,
        events=events,
        findings_by_source=dict(findings_by_source),
        cross_references=cross_references,
        duration_ms=duration_ms,
    )


def _calculate_duration(events: list[Event]) -> float:
    if not events:
        return 0.0
    start = events[0].timestamp
    end = events[-1].timestamp
    return (end - start).total_seconds() * 1000
