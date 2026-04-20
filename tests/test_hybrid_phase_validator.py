from __future__ import annotations

import importlib
import inspect
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, get_args, get_origin

import pytest
from pydantic import BaseModel


def _load_modules() -> SimpleNamespace:
    module_names = {
        "package": "hybrid.project_analysis",
        "models": "hybrid.project_analysis.models",
        "validator": "hybrid.project_analysis.validator",
    }
    loaded: dict[str, Any] = {}
    try:
        for key, module_name in module_names.items():
            loaded[key] = importlib.import_module(module_name)
    except ModuleNotFoundError:  # pragma: no cover - exercised while the task is incomplete
        pytest.fail(
            "hybrid.project_analysis is not implemented yet; the hybrid foundation "
            "phase-validator spec requires this package, its validator, and its public re-exports."
        )
    return SimpleNamespace(**loaded)


def _public_model_classes(module: Any) -> list[tuple[str, type[BaseModel]]]:
    return [
        (name, value)
        for name, value in vars(module).items()
        if not name.startswith("_") and inspect.isclass(value) and issubclass(value, BaseModel)
    ]


def _bool_annotation(annotation: Any) -> bool:
    if annotation is bool:
        return True
    origin = get_origin(annotation)
    if origin is None:
        return annotation is bool
    return any(arg is bool for arg in get_args(annotation))


def _text_annotation(annotation: Any) -> bool:
    if annotation is str:
        return True
    origin = get_origin(annotation)
    if origin is None:
        return False
    return any(arg is str for arg in get_args(annotation))


def _find_validation_outcome_model(module: Any) -> tuple[str, type[BaseModel], str, str]:
    preferred_text_tokens = ("reason", "details", "explanation", "message", "error", "why")
    for name, model_cls in _public_model_classes(module):
        bool_fields = [
            field_name
            for field_name, field_info in model_cls.model_fields.items()
            if _bool_annotation(getattr(field_info, "annotation", Any))
        ]
        text_fields = [
            field_name
            for field_name in model_cls.model_fields
            if _text_annotation(getattr(model_cls.model_fields[field_name], "annotation", Any))
        ]
        if bool_fields and text_fields:
            reason_field = next(
                (
                    field_name
                    for field_name in text_fields
                    if any(token in field_name.lower() for token in preferred_text_tokens)
                ),
                text_fields[0],
            )
            return name, model_cls, bool_fields[0], reason_field
    raise AssertionError(
        "Could not find a validation-outcome model in hybrid.project_analysis.models"
    )


def _find_agent_output_model(module: Any) -> tuple[str, type[BaseModel]]:
    for name, model_cls in _public_model_classes(module):
        field_names = {field_name.lower() for field_name in model_cls.model_fields}
        if {"agent_id", "output"}.issubset(field_names):
            return name, model_cls
    raise AssertionError("Could not find an agent-output model in hybrid.project_analysis.models")


def _find_team_result_model(module: Any) -> tuple[str, type[BaseModel]]:
    for name, model_cls in _public_model_classes(module):
        field_names = {field_name.lower() for field_name in model_cls.model_fields}
        if {"team_name", "result"}.issubset(field_names):
            return name, model_cls
    raise AssertionError("Could not find a team-result model in hybrid.project_analysis.models")


def _find_project_report_model(module: Any) -> tuple[str, type[BaseModel]]:
    required_groups = [("discovery",), ("deep", "dive"), ("synthesis",)]
    for name, model_cls in _public_model_classes(module):
        field_names = list(model_cls.model_fields)
        if all(
            any(all(token in field_name.lower() for token in group) for field_name in field_names)
            for group in required_groups
        ):
            return name, model_cls
    raise AssertionError("Could not find a project-report model in hybrid.project_analysis.models")


def _find_validator_class(module: Any) -> tuple[str, type[Any], str]:
    for name, value in vars(module).items():
        if name.startswith("_") or not inspect.isclass(value):
            continue
        async_methods = [
            method_name
            for method_name, method in inspect.getmembers(value)
            if not method_name.startswith("_") and inspect.iscoroutinefunction(method)
        ]
        if async_methods:
            preferred = next(
                (
                    method_name
                    for method_name in async_methods
                    if method_name.lower() in {"validate", "check", "gate"}
                ),
                async_methods[0],
            )
            return name, value, preferred
    raise AssertionError("Could not find a public validator class with an async entry point")


def _build_validator(
    validator_cls: type[Any],
    *,
    discovery_team_names: set[str],
    deep_dive_team_names: set[str],
) -> Any:
    expected_team_names = {
        "discovery": set(discovery_team_names),
        "deep_dive": set(deep_dive_team_names),
        "synthesis": set(),
    }
    config = SimpleNamespace(
        expected_team_names=expected_team_names,
        team_names=expected_team_names,
        discovery_team_names=set(discovery_team_names),
        deep_dive_team_names=set(deep_dive_team_names),
        synthesis_team_names=set(),
    )
    candidate_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
        ((), {}),
        ((config,), {}),
        ((expected_team_names,), {}),
        ((expected_team_names["discovery"], expected_team_names["deep_dive"]), {}),
        ((expected_team_names["deep_dive"], expected_team_names["discovery"]), {}),
        (
            (
                expected_team_names["discovery"],
                expected_team_names["deep_dive"],
                expected_team_names["synthesis"],
            ),
            {},
        ),
        ((), {"config": config}),
        ((), {"settings": config}),
        ((), {"options": config}),
        ((), {"expected_team_names": expected_team_names}),
        ((), {"team_names": expected_team_names}),
        ((), {"mapping": expected_team_names}),
    ]
    last_error: Exception | None = None
    for args, kwargs in candidate_calls:
        try:
            return validator_cls(*args, **kwargs)
        except TypeError as exc:
            last_error = exc
    raise AssertionError(
        "Could not construct the validator with any behavioral config shape"
    ) from last_error


def _phase_identifier(module: Any, name: str) -> Any:
    target = name.upper()
    for candidate_name, candidate in vars(module).items():
        if candidate_name.startswith("_"):
            continue
        if inspect.isclass(candidate):
            members = {member.upper() for member in dir(candidate) if member.isupper()}
            if target in members:
                return getattr(candidate, target)
        if isinstance(candidate, str) and candidate_name.upper() == target:
            return candidate
        if candidate_name.lower() == name.lower():
            return candidate
    return name


def _sample_value(annotation: Any, field_name: str) -> Any:
    if annotation is Any:
        return f"{field_name}-value"
    if annotation is str:
        return f"{field_name}-value"
    if annotation is bool:
        return True
    if annotation is int:
        return 1
    if annotation is float:
        return 1.0

    origin = get_origin(annotation)
    if origin is None:
        if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
            return _build_model_payload(annotation)
        return f"{field_name}-value"

    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    if not args:
        return f"{field_name}-value"

    inner = args[0]
    inner_origin = get_origin(inner)
    if inner is str:
        return f"{field_name}-value"
    if inner is bool:
        return True
    if inner is int:
        return 1
    if inner is float:
        return 1.0
    if inner_origin is list:
        nested_args = [arg for arg in get_args(inner) if arg is not type(None)]
        nested = nested_args[0] if nested_args else Any
        return [_sample_value(nested, field_name)]
    if inner_origin is dict:
        nested_args = [arg for arg in get_args(inner) if arg is not type(None)]
        nested = nested_args[1] if len(nested_args) > 1 else Any
        return {"item": _sample_value(nested, field_name)}
    if inspect.isclass(inner) and issubclass(inner, BaseModel):
        return _build_model_payload(inner)
    return _sample_value(inner, field_name)


def _empty_value(annotation: Any) -> Any:
    if annotation is Any:
        return None
    if annotation is str:
        return ""
    if annotation is bool:
        return False
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0

    origin = get_origin(annotation)
    if origin is None:
        if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
            return annotation.model_construct(**_empty_model_payload(annotation))
        return None

    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    if not args:
        return None

    inner = args[0]
    inner_origin = get_origin(inner)
    if inner is str:
        return ""
    if inner is bool:
        return False
    if inner is int:
        return 0
    if inner is float:
        return 0.0
    if inner_origin is list:
        return []
    if inner_origin is dict:
        return {}
    if inspect.isclass(inner) and issubclass(inner, BaseModel):
        return inner.model_construct(**_empty_model_payload(inner))
    return _empty_value(inner)


def _empty_model_payload(model_cls: type[BaseModel]) -> dict[str, Any]:
    return {
        field_name: _empty_value(getattr(field_info, "annotation", Any))
        for field_name, field_info in model_cls.model_fields.items()
    }


def _build_model_payload(model_cls: type[BaseModel]) -> dict[str, Any]:
    return {
        field_name: _sample_value(getattr(field_info, "annotation", Any), field_name)
        for field_name, field_info in model_cls.model_fields.items()
    }


def _reason_text(outcome: Any, reason_field: str) -> str:
    if isinstance(outcome, BaseModel):
        return str(getattr(outcome, reason_field))
    if isinstance(outcome, Mapping):
        return str(outcome[reason_field])
    return str(getattr(outcome, reason_field))


def _bool_value(outcome: Any, bool_field: str) -> bool:
    if isinstance(outcome, BaseModel):
        return bool(getattr(outcome, bool_field))
    if isinstance(outcome, Mapping):
        return bool(outcome[bool_field])
    return bool(getattr(outcome, bool_field))


def _invoke_validator(validator_method: Any, phase: Any, result: Any) -> Any:
    if len(inspect.signature(validator_method).parameters) < 2:
        raise AssertionError("Expected an async validator method that accepts phase and result")
    return validator_method(phase, result)


def _build_team_result(
    *,
    agent_output_model_cls: type[BaseModel],
    team_result_model_cls: type[BaseModel],
    team_name: str,
    output: dict[str, Any],
) -> BaseModel:
    agent_output = agent_output_model_cls.model_validate(
        {
            "agent_id": f"{team_name}-agent",
            "output": output,
            "status": "success",
            "error": None,
        }
    )
    return team_result_model_cls.model_validate(
        {
            "team_name": team_name,
            "result": output,
            "agent_outputs": [agent_output],
            "failures": [],
        }
    )


def _build_report_with_empty_section(
    report_model_cls: type[BaseModel],
) -> tuple[BaseModel, BaseModel, str]:
    full_report_payload = _build_model_payload(report_model_cls)
    report = report_model_cls.model_validate(full_report_payload)

    required_section_name = next(
        (
            field_name
            for field_name, field_info in report_model_cls.model_fields.items()
            if field_info.is_required()
            and any(
                token in field_name.lower() for token in ("discovery", "deep", "dive", "synthesis")
            )
        ),
        next(
            field_name
            for field_name, field_info in report_model_cls.model_fields.items()
            if field_info.is_required()
        ),
    )
    empty_report_payload = dict(full_report_payload)
    empty_report_payload[required_section_name] = _empty_value(
        getattr(report_model_cls.model_fields[required_section_name], "annotation", Any)
    )
    incomplete_report = report.model_copy(
        update={required_section_name: empty_report_payload[required_section_name]}
    )
    return report, incomplete_report, required_section_name


@pytest.mark.asyncio
async def test_phase_validator_enforces_phase_rules_without_state_leakage():
    modules = _load_modules()
    _, outcome_model_cls, outcome_bool_field, outcome_reason_field = _find_validation_outcome_model(
        modules.models
    )
    _, agent_output_model_cls = _find_agent_output_model(modules.models)
    _, team_result_model_cls = _find_team_result_model(modules.models)
    _, report_model_cls = _find_project_report_model(modules.models)
    _, validator_cls, validator_method_name = _find_validator_class(modules.validator)
    validator = _build_validator(
        validator_cls,
        discovery_team_names={"atlas", "beacon"},
        deep_dive_team_names={"cipher", "delta"},
    )
    validator_method = getattr(validator, validator_method_name)

    discovery = _phase_identifier(modules.package, "discovery")
    deep_dive = _phase_identifier(modules.package, "deep_dive")
    synthesis = _phase_identifier(modules.package, "synthesis")

    discovery_ok = {
        "atlas": _build_team_result(
            agent_output_model_cls=agent_output_model_cls,
            team_result_model_cls=team_result_model_cls,
            team_name="atlas",
            output={"marker": "one"},
        ),
        "beacon": _build_team_result(
            agent_output_model_cls=agent_output_model_cls,
            team_result_model_cls=team_result_model_cls,
            team_name="beacon",
            output={"marker": "two"},
        ),
    }
    discovery_missing = [discovery_ok["atlas"]]
    discovery_empty = [
        discovery_ok["atlas"],
        _build_team_result(
            agent_output_model_cls=agent_output_model_cls,
            team_result_model_cls=team_result_model_cls,
            team_name="beacon",
            output={},
        ),
    ]

    first_outcome = await _invoke_validator(validator_method, discovery, discovery_missing)
    second_outcome = await _invoke_validator(
        validator_method, discovery, list(discovery_ok.values())
    )
    third_outcome = await _invoke_validator(validator_method, discovery, discovery_empty)

    assert isinstance(first_outcome, outcome_model_cls)
    assert isinstance(second_outcome, outcome_model_cls)
    assert isinstance(third_outcome, outcome_model_cls)
    assert _bool_value(first_outcome, outcome_bool_field) is False
    assert _bool_value(second_outcome, outcome_bool_field) is True
    assert _bool_value(third_outcome, outcome_bool_field) is False
    assert "beacon" in _reason_text(first_outcome, outcome_reason_field).lower()
    assert "missing" in _reason_text(first_outcome, outcome_reason_field).lower()
    assert "beacon" in _reason_text(third_outcome, outcome_reason_field).lower()
    assert "empty" in _reason_text(third_outcome, outcome_reason_field).lower()

    deep_dive_ok = {
        "cipher": _build_team_result(
            agent_output_model_cls=agent_output_model_cls,
            team_result_model_cls=team_result_model_cls,
            team_name="cipher",
            output={"marker": "three"},
        ),
        "delta": _build_team_result(
            agent_output_model_cls=agent_output_model_cls,
            team_result_model_cls=team_result_model_cls,
            team_name="delta",
            output={"marker": "four"},
        ),
    }
    deep_dive_missing = [deep_dive_ok["cipher"]]
    deep_dive_empty = [
        _build_team_result(
            agent_output_model_cls=agent_output_model_cls,
            team_result_model_cls=team_result_model_cls,
            team_name="cipher",
            output={},
        ),
        deep_dive_ok["delta"],
    ]

    fourth_outcome = await _invoke_validator(
        validator_method, deep_dive, list(deep_dive_ok.values())
    )
    fifth_outcome = await _invoke_validator(validator_method, deep_dive, deep_dive_missing)
    sixth_outcome = await _invoke_validator(validator_method, deep_dive, deep_dive_empty)

    assert _bool_value(fourth_outcome, outcome_bool_field) is True
    assert _bool_value(fifth_outcome, outcome_bool_field) is False
    assert _bool_value(sixth_outcome, outcome_bool_field) is False
    assert "delta" in _reason_text(fifth_outcome, outcome_reason_field).lower()
    assert "missing" in _reason_text(fifth_outcome, outcome_reason_field).lower()
    assert "cipher" in _reason_text(sixth_outcome, outcome_reason_field).lower()
    assert "empty" in _reason_text(sixth_outcome, outcome_reason_field).lower()

    report, incomplete_report, empty_section_name = _build_report_with_empty_section(
        report_model_cls
    )

    seventh_outcome = await _invoke_validator(validator_method, synthesis, report)
    eighth_outcome = await _invoke_validator(validator_method, synthesis, incomplete_report)
    ninth_outcome = await _invoke_validator(validator_method, synthesis, report)

    assert _bool_value(seventh_outcome, outcome_bool_field) is True
    assert _bool_value(eighth_outcome, outcome_bool_field) is False
    assert _bool_value(ninth_outcome, outcome_bool_field) is True
    assert empty_section_name.lower() in _reason_text(eighth_outcome, outcome_reason_field).lower()
    assert any(
        token in _reason_text(eighth_outcome, outcome_reason_field).lower()
        for token in ("empty", "missing")
    )


def test_public_api_reexports_validator_and_models_from_package_root():
    modules = _load_modules()
    package = modules.package

    outcome_name, outcome_model_cls, _, _ = _find_validation_outcome_model(modules.models)
    report_name, report_model_cls = _find_project_report_model(modules.models)
    validator_name, validator_cls, _ = _find_validator_class(modules.validator)

    assert getattr(package, outcome_name) is outcome_model_cls
    assert getattr(package, report_name) is report_model_cls
    assert getattr(package, validator_name) is validator_cls
