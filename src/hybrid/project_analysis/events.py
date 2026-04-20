from __future__ import annotations

from typing import Any

from pydantic import BaseModel as _BaseModel
from pydantic import Field

TEAM_COMPLETE_TOPIC = "hybrid.project_analysis.team_complete"

__all__ = ["TEAM_COMPLETE_TOPIC", "TeamCompleteEvent", "build_team_topic"]


def build_team_topic(team_name: str, event_name: str) -> str:
    return f"hybrid.project_analysis.team.{team_name}.{event_name}"


class TeamCompleteEvent(_BaseModel):
    team_name: str
    result: dict[str, Any] = Field(default_factory=dict)
    agent_outputs: list[dict[str, Any]] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
