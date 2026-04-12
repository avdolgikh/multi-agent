from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ConfigDict

__all__ = [
    "FunctionDescriptor",
    "ClassDescriptor",
    "ParseResult",
    "SecurityFinding",
    "SecurityResult",
    "QualityIssue",
    "QualityResult",
    "Recommendation",
    "AnalysisReport",
    "StepResult",
]


class FunctionDescriptor(BaseModel):
    name: str
    params: list[str] = Field(default_factory=list)
    return_type: str | None = None
    line_range: str | None = None


class ClassDescriptor(BaseModel):
    name: str
    line_range: str | None = None
    methods: list[str] = Field(default_factory=list)


class ParseResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    functions: list[FunctionDescriptor | dict[str, Any]] = Field(default_factory=list)
    classes: list[ClassDescriptor | dict[str, Any]] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    dependencies: dict[str, str] = Field(default_factory=dict)


class SecurityFinding(BaseModel):
    severity: Literal["critical", "high", "medium", "low"]
    location: str
    description: str
    recommendation: str


class SecurityResult(BaseModel):
    findings: list[SecurityFinding] = Field(default_factory=list)


class QualityIssue(BaseModel):
    location: str
    description: str
    severity: Literal["critical", "high", "medium", "low"]


class QualityResult(BaseModel):
    score: int = 100
    issues: list[QualityIssue] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class Recommendation(BaseModel):
    title: str
    priority: Literal["critical", "high", "medium", "low"]
    detail: str | None = None


class AnalysisReport(BaseModel):
    executive_summary: str
    security_section: dict[str, Any] = Field(default_factory=dict)
    quality_section: dict[str, Any] = Field(default_factory=dict)
    recommendations: list[Recommendation] = Field(default_factory=list)


StepResult = ParseResult | SecurityResult | QualityResult | AnalysisReport
