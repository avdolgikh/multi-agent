from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from .models import ProjectReport, ValidationOutcome

__all__ = ["Phase", "PhaseValidator"]


class Phase(str, Enum):
    DISCOVERY = "discovery"
    DEEP_DIVE = "deep_dive"
    SYNTHESIS = "synthesis"


class PhaseValidator:
    def __init__(self, discovery_team_names: set[str], deep_dive_team_names: set[str]) -> None:
        self._expected_team_names: dict[str, set[str]] = {
            Phase.DISCOVERY.value: set(discovery_team_names),
            Phase.DEEP_DIVE.value: set(deep_dive_team_names),
            Phase.SYNTHESIS.value: set(),
        }

    async def validate(self, phase: Any, result: Any) -> ValidationOutcome:
        phase_name = self._normalize_phase(phase)
        if phase_name in {Phase.DISCOVERY.value, Phase.DEEP_DIVE.value}:
            return self._validate_team_phase(phase_name=phase_name, result=result)
        if phase_name == Phase.SYNTHESIS.value:
            return self._validate_synthesis(result)
        return ValidationOutcome(is_valid=False, reason=f"Unsupported phase: {phase_name}")

    def _validate_team_phase(self, *, phase_name: str, result: Any) -> ValidationOutcome:
        expected = self._expected_team_names.get(phase_name, set())
        team_results = self._coerce_team_results(result)
        actual_team_names = set(team_results)

        missing = sorted(expected - actual_team_names)
        if missing:
            return ValidationOutcome(
                is_valid=False,
                reason=f"Missing team output for {', '.join(missing)}",
            )

        empty = sorted(
            team_name
            for team_name in expected
            if not self._has_content(team_results.get(team_name))
        )
        if empty:
            return ValidationOutcome(
                is_valid=False,
                reason=f"Empty team output for {', '.join(empty)}",
            )

        return ValidationOutcome(is_valid=True, reason="")

    def _validate_synthesis(self, result: Any) -> ValidationOutcome:
        report = self._coerce_report(result)
        required_sections = {
            "discovery_findings": report.discovery_findings,
            "deep_dive_findings": report.deep_dive_findings,
            "synthesis_summary": report.synthesis_summary,
        }
        for section_name, section_value in required_sections.items():
            if not self._has_content(section_value):
                return ValidationOutcome(
                    is_valid=False,
                    reason=f"Missing or empty {section_name}",
                )
        return ValidationOutcome(is_valid=True, reason="")

    def _normalize_phase(self, phase: Any) -> str:
        if isinstance(phase, Phase):
            return phase.value
        if isinstance(phase, str):
            return phase.lower()
        phase_name = getattr(phase, "name", None)
        if isinstance(phase_name, str):
            return phase_name.lower()
        phase_value = getattr(phase, "value", None)
        if isinstance(phase_value, str):
            return phase_value.lower()
        return str(phase).lower()

    def _coerce_team_results(self, result: Any) -> dict[str, Any]:
        if isinstance(result, Mapping):
            if {"team_name", "result"}.issubset(result.keys()):
                team_name = str(result["team_name"])
                return {team_name: result.get("result")}
            return {
                str(team_name): self._extract_team_payload(team_result)
                for team_name, team_result in result.items()
            }

        if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
            collected: dict[str, Any] = {}
            for team_result in result:
                if team_result is None:
                    continue
                team_name = getattr(team_result, "team_name", None)
                if team_name is None and isinstance(team_result, Mapping):
                    team_name = team_result.get("team_name")
                if not isinstance(team_name, str):
                    continue
                collected[team_name] = self._extract_team_payload(team_result)
            return collected

        team_name = getattr(result, "team_name", None)
        if isinstance(team_name, str):
            return {team_name: self._extract_team_payload(result)}
        return {}

    def _extract_team_payload(self, team_result: Any) -> Any:
        payload = getattr(team_result, "result", None)
        if payload is not None:
            return payload
        if isinstance(team_result, Mapping):
            return team_result.get("result")
        return None

    def _coerce_report(self, result: Any) -> ProjectReport:
        if isinstance(result, ProjectReport):
            return result
        if hasattr(result, "model_dump"):
            return ProjectReport.model_validate(result.model_dump())
        if isinstance(result, Mapping):
            return ProjectReport.model_validate(dict(result))
        raise TypeError("Synthesis result must be a project report")

    def _has_content(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, set, dict)):
            return len(value) > 0
        return True
