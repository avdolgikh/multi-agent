from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

__all__ = [
    "Event",
    "Snapshot",
    "EventStore",
    "InMemoryEventStore",
    "SnapshotStore",
]


class Event(BaseModel):
    event_id: str
    stream: str
    event_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime
    sequence: int = 0
    trace_context: dict = Field(default_factory=dict)


class EventStore(Protocol):
    async def append(self, stream: str, event: Event) -> int: ...

    async def read(self, stream: str, from_seq: int = 0) -> list[Event]: ...

    async def replay(self, stream: str) -> dict[str, Any]: ...


class InMemoryEventStore(EventStore):
    def __init__(self) -> None:
        self._streams: dict[str, list[Event]] = {}
        self._lock = asyncio.Lock()

    async def append(self, stream: str, event: Event) -> int:
        async with self._lock:
            events = self._streams.setdefault(stream, [])
            sequence = len(events)
            stored = event.model_copy(update={"sequence": sequence})
            events.append(stored)
            return sequence

    async def read(self, stream: str, from_seq: int = 0) -> list[Event]:
        async with self._lock:
            events = list(self._streams.get(stream, []))
        return events[from_seq:]

    async def replay(self, stream: str) -> dict[str, Any]:
        events = await self.read(stream)
        state: dict[str, Any] = {}
        for event in events:
            state.update(event.data)
        return state


class Snapshot(BaseModel):
    snapshot_id: str
    workflow_id: str
    step: str
    state: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime


class SnapshotStore:
    def __init__(self) -> None:
        self._snapshots: dict[str, Snapshot] = {}
        self._workflow_index: dict[str, list[Snapshot]] = {}
        self._lock = asyncio.Lock()

    async def save(self, workflow_id: str, step: str, state: dict[str, Any]) -> str:
        snapshot_id = str(uuid4())
        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            workflow_id=workflow_id,
            step=step,
            state=dict(state),
            timestamp=datetime.utcnow(),
        )
        async with self._lock:
            self._snapshots[snapshot_id] = snapshot
            self._workflow_index.setdefault(workflow_id, []).append(snapshot)
        return snapshot_id

    async def load(self, snapshot_id: str) -> dict[str, Any]:
        async with self._lock:
            snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None:
            raise KeyError(f"Snapshot {snapshot_id} not found")
        return dict(snapshot.state)

    async def history(self, workflow_id: str) -> list[Snapshot]:
        async with self._lock:
            history = list(self._workflow_index.get(workflow_id, []))
        return sorted(history, key=lambda snap: snap.timestamp)
