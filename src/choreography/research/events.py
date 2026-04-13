"""Event models for the choreography research system."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, ClassVar, Literal
from uuid import uuid4

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    Field,
    field_validator,
    model_validator,
)

from core.messaging import Message

__all__ = [
    "ResearchEvent",
    "ResearchRequested",
    "FindingDiscovered",
    "CrossReferenceFound",
    "CrossReferenceStatus",
    "SourceExhausted",
    "ResearchComplete",
    "AgentError",
    "ResearchBrief",
    "FindingSummary",
    "CrossReferenceSummary",
]

BASE_TOPIC = "choreography.research"
SourceType = Literal["web", "academic", "code", "news"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_topic(name: str) -> str:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return f"{BASE_TOPIC}.{snake}"


class ResearchEvent(Message):
    """Base message for choreography events."""

    research_id: str = Field(default="")
    event_type: str = Field(default="")
    timestamp: datetime = Field(default_factory=_utcnow)
    trace_context: dict[str, Any] = Field(default_factory=dict)
    topic: str = Field(default="")
    message_id: str = Field(default="")

    _base_exclusions: ClassVar[set[str]] = {
        "message_id",
        "topic",
        "payload",
        "timestamp",
        "trace_context",
        "source_agent",
        "event_type",
    }

    @classmethod
    def event_type_name(cls) -> str:
        return cls.__name__

    @classmethod
    def topic_name(cls) -> str:
        return _to_topic(cls.event_type_name())

    @model_validator(mode="before")
    @classmethod
    def _apply_defaults(cls, data: Any) -> Any:
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        research_id = payload.get("research_id")
        if research_id is None:
            raise ValueError("research_id is required")
        if not isinstance(research_id, str):
            research_id = str(research_id)
        research_id = research_id.strip()
        if not research_id:
            raise ValueError("research_id is required")
        payload["research_id"] = research_id
        payload.setdefault("message_id", f"{cls.event_type_name().lower()}-{uuid4().hex}")
        payload.setdefault("topic", cls.topic_name())
        payload.setdefault("event_type", cls.event_type_name())
        timestamp = payload.get("timestamp")
        if timestamp is None:
            payload["timestamp"] = _utcnow()
        elif isinstance(timestamp, str):
            payload["timestamp"] = datetime.fromisoformat(timestamp)
        payload.setdefault("trace_context", {})
        payload.setdefault("payload", {})
        return payload

    @staticmethod
    def _coerce_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        return None


class ResearchRequested(ResearchEvent):
    scope: str = Field(
        validation_alias=AliasChoices(AliasPath("payload", "scope"), "scope"),
    )
    deadline: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices(AliasPath("payload", "deadline"), "deadline"),
    )

    @field_validator("deadline", mode="before")
    @classmethod
    def _parse_deadline(cls, value: Any) -> datetime | None:
        return cls._coerce_datetime(value)

    @property
    def topic_value(self) -> str:
        return str(self.payload.get("topic", ""))

    @model_validator(mode="after")
    def _ensure_topic(self) -> "ResearchRequested":
        topic_value = self.payload.get("topic")
        if not isinstance(topic_value, str) or not topic_value:
            raise ValueError("topic is required")
        return self


class FindingDiscovered(ResearchEvent):
    finding_id: str = Field(default_factory=lambda: f"finding-{uuid4().hex}")
    source_type: SourceType
    title: str
    summary: str
    url: str
    relevance_score: float = Field(ge=0.0, le=1.0)
    raw_content: str
    authors: list[str] | None = None
    year: int | None = None
    repository: str | None = None
    language: str | None = None
    published_date: datetime | None = None

    @field_validator("published_date", mode="before")
    @classmethod
    def _parse_published(cls, value: Any) -> datetime | None:
        return cls._coerce_datetime(value)

    @model_validator(mode="after")
    def _check_source_specific_fields(self) -> "FindingDiscovered":
        if self.source_type == "academic":
            if not self.authors:
                raise ValueError("academic findings require authors")
            if self.year is None:
                raise ValueError("academic findings require year")
        if self.source_type == "code":
            if not self.repository or not self.language:
                raise ValueError("code findings require repository and language")
        if self.source_type == "news":
            if self.published_date is None:
                raise ValueError("news findings require published_date")
        return self


class CrossReferenceFound(ResearchEvent):
    finding_a_id: str
    finding_b_id: str
    relationship: Literal["corroborates", "contradicts", "extends"]
    explanation: str


class CrossReferenceStatus(ResearchEvent):
    pending_findings: int = Field(ge=0)
    is_idle: bool = Field(default=False)
    all_sources_exhausted: bool = Field(default=False)


class SourceExhausted(ResearchEvent):
    source_type: SourceType
    available: bool = True
    reason: str | None = None


class FindingSummary(BaseModel):
    finding_id: str
    source_type: SourceType
    title: str | None = None
    summary: str | None = None
    url: str | None = None


class CrossReferenceSummary(BaseModel):
    finding_a_id: str
    finding_b_id: str
    relationship: Literal["corroborates", "contradicts", "extends"]
    explanation: str


class ResearchBrief(BaseModel):
    topic: str
    summary: str
    key_findings: list[FindingSummary]
    cross_references: list[CrossReferenceSummary]
    sources_consulted: dict[str, int]
    confidence_score: float


class ResearchComplete(ResearchEvent):
    brief: ResearchBrief


class AgentError(ResearchEvent):
    agent_id: str
    error: str
    details: dict[str, Any] | None = None
