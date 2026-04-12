from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, Field

from core.agents import AgentResult, AgentTask, BaseAgent
from core.messaging import Message
from core.tracing import inject_context

from .models import (
    AnalysisReport,
    ParseResult,
    QualityResult,
    SecurityResult,
    StepResult,
)

__all__ = ["ValidationResult", "StepValidator", "ValidationAgent"]


_ALLOWED_SEVERITIES = {"critical", "high", "medium", "low"}


class ValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ValidationAgent(BaseAgent):
    """Optional agent that reviews results for consistency."""

    def __init__(self, *args: Any, use_llm: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._use_llm = use_llm

    async def execute(self, task: AgentTask) -> AgentResult:
        warnings: list[str] = []
        summary = str(task.input_data.get("summary", ""))
        if self._use_llm and summary.strip():  # pragma: no cover - exercised when LLM flag enabled
            message = Message(
                message_id=f"validation-{task.task_id}",
                topic="validation.review",
                payload={"role": "user", "content": summary},
                timestamp=datetime.now(timezone.utc),
                trace_context=task.trace_context or {},
                source_agent=self.agent_id,
            )
            response = await self.call_llm([message])
            content = response.content.strip()
            if content:
                warnings.append(content)
        else:
            if "todo" in summary.lower():
                warnings.append("Detected placeholder text in the result summary.")
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data={"warnings": warnings},
            status="success",
            error=None,
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )

    async def review(
        self,
        *,
        step: str,
        result: StepResult,
        previous_results: Mapping[str, StepResult] | None,
        input_path: str,
    ) -> list[str]:
        summary = self._summarize(step, result, previous_results)
        task = AgentTask(
            task_id=f"validation-{step.lower()}",
            input_data={
                "summary": summary,
                "input_path": input_path,
                "step": step,
            },
            metadata={"step": step},
            trace_context=inject_context(),
        )
        agent_result = await self.execute(task)
        warnings = agent_result.output_data.get("warnings")
        if not isinstance(warnings, Iterable):
            return []
        return [str(item) for item in warnings if str(item).strip()]

    def _summarize(
        self,
        step: str,
        result: StepResult,
        previous_results: Mapping[str, StepResult] | None,
    ) -> str:
        base = f"Step {step}: {result.__class__.__name__}"
        if isinstance(result, ParseResult):
            return base + f" with {len(result.functions)} functions"
        if isinstance(result, SecurityResult):
            return base + f" with {len(result.findings)} findings"
        if isinstance(result, QualityResult):
            return base + f" score {result.score}"
        if isinstance(result, AnalysisReport):
            return base + " generated report"
        return base


class StepValidator:
    """Deterministic validations applied after every step."""

    def __init__(self, validation_agent: ValidationAgent | None = None) -> None:
        self._validation_agent = validation_agent

    async def validate(
        self,
        step: str,
        result: StepResult,
        *,
        previous_results: Mapping[str, StepResult] | None = None,
        input_path: str,
    ) -> ValidationResult:
        normalized = step.upper()
        errors: list[str] = []
        warnings: list[str] = []

        if normalized == "PARSING":
            self._validate_parser(result, errors)
        elif normalized == "SCANNING":
            self._validate_security(result, errors)
        elif normalized == "CHECKING":
            self._validate_quality(result, errors, previous_results)
        elif normalized == "REPORTING":
            self._validate_report(result, errors)

        if self._validation_agent is not None:
            warnings.extend(
                await self._validation_agent.review(
                    step=step,
                    result=result,
                    previous_results=previous_results or {},
                    input_path=input_path,
                )
            )

        return ValidationResult(valid=not errors, errors=errors, warnings=warnings)

    def _validate_parser(self, result: StepResult, errors: list[str]) -> None:
        if not isinstance(result, ParseResult):
            errors.append("Parser output must be a ParseResult instance")
            return
        if not result.functions and not result.classes:
            errors.append("Parser output must include at least one function or class")

    def _validate_security(self, result: StepResult, errors: list[str]) -> None:
        if not isinstance(result, SecurityResult):
            errors.append("Security output must be a SecurityResult instance")
            return
        for finding in result.findings:
            if finding.severity not in _ALLOWED_SEVERITIES:
                errors.append(f"Invalid severity '{finding.severity}'")
            if not finding.description.strip():
                errors.append("Security finding description cannot be empty")
            if not finding.location.strip():
                errors.append("Security finding must include a location")
            if not finding.recommendation.strip():
                errors.append("Security finding must include a recommendation")

    def _validate_quality(
        self,
        result: StepResult,
        errors: list[str],
        previous_results: Mapping[str, StepResult] | None,
    ) -> None:
        if not isinstance(result, QualityResult):
            errors.append("Quality output must be a QualityResult instance")
            return
        if not 0 <= result.score <= 100:
            errors.append("Quality score must be between 0 and 100")
        parse_result = self._find_parse_result(previous_results)
        if parse_result is None:
            return
        valid_locations = self._collect_locations(parse_result)
        for issue in result.issues:
            if issue.location not in valid_locations:
                errors.append(f"Quality issue references unknown location '{issue.location}'")

    def _validate_report(self, result: StepResult, errors: list[str]) -> None:
        if not isinstance(result, AnalysisReport):
            errors.append("Report output must be an AnalysisReport instance")
            return
        if not result.executive_summary.strip():
            errors.append("Report requires an executive summary")
        if not result.security_section:
            errors.append("Report must include a security section")
        if not result.quality_section:
            errors.append("Report must include a quality section")
        if not result.recommendations:
            errors.append("Report must include recommendations")

    def _find_parse_result(
        self,
        previous_results: Mapping[str, StepResult] | None,
    ) -> ParseResult | None:
        if not previous_results:
            return None
        for value in previous_results.values():
            if isinstance(value, ParseResult):
                return value
        return None

    def _collect_locations(self, parse_result: ParseResult) -> set[str]:
        locations: set[str] = set()
        for fn in parse_result.functions:
            name = self._extract_name(fn)
            if name:
                locations.add(name)
        for cls in parse_result.classes:
            name = self._extract_name(cls)
            if name:
                locations.add(name)
        if not locations:
            locations.add("module")
        return locations

    def _extract_name(self, value: Any) -> str | None:
        if isinstance(value, Mapping):
            name = value.get("name")
            if isinstance(name, str):
                return name
            return None
        if hasattr(value, "name"):
            return getattr(value, "name")
        return None
