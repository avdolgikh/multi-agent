from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = ["AgentOutput", "TeamResult", "ValidationOutcome", "ProjectReport"]


class AgentOutput(BaseModel):
    agent_id: str
    output: dict[str, Any] = Field(default_factory=dict)
    status: str = "success"
    error: str | None = None


class TeamResult(BaseModel):
    team_name: str
    result: dict[str, Any] = Field(default_factory=dict)
    agent_outputs: list[AgentOutput] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)


class ValidationOutcome(BaseModel):
    is_valid: bool
    reason: str


class ProjectReport(BaseModel):
    discovery_findings: str
    deep_dive_findings: str
    synthesis_summary: str
