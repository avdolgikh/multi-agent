from __future__ import annotations

import runpy
import sys
from typing import Any, Callable
from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.trace.export import InMemorySpanExporter, SimpleSpanProcessor

from core.agents import AgentResult, AgentTask, BaseAgent
from core.resilience import CircuitOpenError
from core.state import SnapshotStore
from core.tracing import TracingManager

from orchestration.code_analysis.agents import ParserAgent, QualityAgent, ReportAgent, SecurityAgent
from orchestration.code_analysis.models import (
    AnalysisReport,
    ParseResult,
    QualityIssue,
    QualityResult,
    Recommendation,
    SecurityFinding,
    SecurityResult,
    StepResult,
)
from orchestration.code_analysis.orchestrator import (
    CodeAnalysisOrchestrator,
    InvalidTransitionError,
    PipelineResult,
)
from orchestration.code_analysis.saga import CompensationResult as SagaCompensationResult, SagaCoordinator
from orchestration.code_analysis.validation import StepValidator

STEP_PARSING = CodeAnalysisOrchestrator.STEP_PARSING
STEP_SCANNING = CodeAnalysisOrchestrator.STEP_SCANNING
STEP_CHECKING = CodeAnalysisOrchestrator.STEP_CHECKING
STEP_REPORTING = CodeAnalysisOrchestrator.STEP_REPORTING


def _sample_parse_result() -> ParseResult:
    return ParseResult(
        functions=[
            {
                "name": "compute",
                "params": ["value"],
                "return_type": "int",
                "line_range": "1-3",
            }
        ],
        classes=[
            {
                "name": "Calculator",
                "line_range": "5-15",
            }
        ],
        imports=["math"],
        dependencies={"math": "stdlib"},
    )


def _sample_security_result(
    *,
    severity: str = "high",
    description: str = "Hardcoded secret detected",
) -> SecurityResult:
    return SecurityResult(
        findings=[
            SecurityFinding(
                severity=severity,
                location="module.py:3",
                description=description,
                recommendation="Rotate secrets and use env vars.",
            )
        ]
    )


def _sample_quality_result(
    *,
    score: int = 88,
    issues: list[QualityIssue] | None = None,
    metrics: dict[str, Any] | None = None,
) -> QualityResult:
    return QualityResult(
        score=score,
        issues=issues or [
            QualityIssue(
                location="compute",
                description="Function is missing documentation.",
                severity="medium",
            )
        ],
        metrics=metrics or {"cyclomatic_complexity": {"compute": 3}},
    )


def _sample_recommendations() -> list[Recommendation]:
    return [
        Recommendation(title="Add unit tests", priority="high"),
        Recommendation(title="Document public helpers", priority="medium"),
    ]


def _sample_analysis_report() -> AnalysisReport:
    return AnalysisReport(
        executive_summary="Code looks maintainable with minor follow-up actions.",
        security_section={"findings": ["Secrets are flagged for rotation."]},
        quality_section={"score_breakdown": {"average_complexity": 3}},
        recommendations=_sample_recommendations(),
    )


def _wrap_agent_result(agent: BaseAgent, result: StepResult) -> AgentResult:
    return AgentResult(
        task_id=f"{agent.agent_id}-task",
        agent_id=agent.agent_id,
        output_data={"result": result},
        status="success",
        error=None,
        duration_ms=0.0,
        trace_context={},
    )


AgentExecuteBuilder = Callable[[BaseAgent], AsyncMock]


class RecordingSnapshotStore(SnapshotStore):
    def __init__(self) -> None:
        super().__init__()
        self.workflow_ids: list[str] = []

    async def save(self, workflow_id: str, step: str, state: dict[str, Any]) -> str:
        if not self.workflow_ids or self.workflow_ids[-1] != workflow_id:
            self.workflow_ids.append(workflow_id)
        return await super().save(workflow_id, step, state)

    def latest_workflow_id(self) -> str | None:
        return self.workflow_ids[-1] if self.workflow_ids else None


class RecordingSagaCoordinator(SagaCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.compensation_results: list[SagaCompensationResult] = []

    async def compensate_all(self) -> SagaCompensationResult:
        result = await super().compensate_all()
        self.compensation_results.append(result)
        return result


def _normalize_step_result(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


def _find_results_map(input_data: dict[str, Any]) -> dict[str, Any]:
    for value in input_data.values():
        if isinstance(value, dict) and {STEP_PARSING, STEP_SCANNING, STEP_CHECKING}.intersection(value):
            return value
    raise AssertionError("Unable to locate the accumulating results map in task input_data")


def _build_orchestrator_components(
    *,
    validator: StepValidator | None = None,
    snapshot_store: SnapshotStore | None = None,
    saga: SagaCoordinator | None = None,
    tracer_provider=None,
) -> tuple[
    CodeAnalysisOrchestrator,
    ParserAgent,
    SecurityAgent,
    QualityAgent,
    ReportAgent,
    SnapshotStore,
    SagaCoordinator,
]:
    parser_agent = ParserAgent(
        agent_id="parser",
        name="Parser Agent",
        model="qwen3-coder:latest",
        provider="ollama",
        tools=[],
        system_prompt="Describe the structure of the code.",
        base_url=None,
    )
    security_agent = SecurityAgent(
        agent_id="security",
        name="Security Agent",
        model="qwen3-coder:latest",
        provider="ollama",
        tools=[],
        system_prompt="Find OWASP vulnerabilities.",
        base_url=None,
    )
    quality_agent = QualityAgent(
        agent_id="quality",
        name="Quality Agent",
        model="qwen3-coder:latest",
        provider="ollama",
        tools=[],
        system_prompt="Measure code quality metrics.",
        base_url=None,
    )
    report_agent = ReportAgent(
        agent_id="report",
        name="Report Agent",
        model="qwen3-coder:latest",
        provider="ollama",
        tools=[],
        system_prompt="Summarize the findings.",
        base_url=None,
    )

    _validator = validator or StepValidator()
    _snapshot_store = snapshot_store or SnapshotStore()
    _saga = saga or SagaCoordinator()

    orchestrator = CodeAnalysisOrchestrator(
        parser=parser_agent,
        security=security_agent,
        quality=quality_agent,
        report=report_agent,
        validator=_validator,
        saga=_saga,
        snapshot_store=_snapshot_store,
        tracer_provider=tracer_provider,
    )

    return (
        orchestrator,
        parser_agent,
        security_agent,
        quality_agent,
        report_agent,
        _snapshot_store,
        _saga,
    )


async def _run_sample_pipeline(
    tmp_path,
    *,
    parse_result: ParseResult | None = None,
    security_result: SecurityResult | None = None,
    quality_result: QualityResult | None = None,
    report_result: AnalysisReport | None = None,
    validator: StepValidator | None = None,
    tracer_provider=None,
    parser_execute: AsyncMock | None = None,
    security_execute: AsyncMock | None = None,
    quality_execute: AsyncMock | None = None,
    report_execute: AsyncMock | None = None,
    snapshot_store: SnapshotStore | None = None,
    saga: SagaCoordinator | None = None,
    parser_execute_builder: AgentExecuteBuilder | None = None,
    security_execute_builder: AgentExecuteBuilder | None = None,
    quality_execute_builder: AgentExecuteBuilder | None = None,
    report_execute_builder: AgentExecuteBuilder | None = None,
    ) -> tuple[
        PipelineResult,
        CodeAnalysisOrchestrator,
        SnapshotStore,
        SagaCoordinator,
    ]:
    parse_result = parse_result or _sample_parse_result()
    security_result = security_result or _sample_security_result()
    quality_result = quality_result or _sample_quality_result()
    report_result = report_result or _sample_analysis_report()

    (
        orchestrator,
        parser_agent,
        security_agent,
        quality_agent,
        report_agent,
        snapshot_store,
        saga,
    ) = _build_orchestrator_components(
        validator=validator,
        snapshot_store=snapshot_store,
        saga=saga,
        tracer_provider=tracer_provider,
    )

    parser_execute_mock = (
        parser_execute_builder(parser_agent)
        if parser_execute_builder is not None
        else parser_execute
    )
    security_execute_mock = (
        security_execute_builder(security_agent)
        if security_execute_builder is not None
        else security_execute
    )
    quality_execute_mock = (
        quality_execute_builder(quality_agent)
        if quality_execute_builder is not None
        else quality_execute
    )
    report_execute_mock = (
        report_execute_builder(report_agent)
        if report_execute_builder is not None
        else report_execute
    )

    parser_agent.execute = parser_execute_mock or AsyncMock(
        return_value=_wrap_agent_result(parser_agent, parse_result)
    )
    security_agent.execute = security_execute_mock or AsyncMock(
        return_value=_wrap_agent_result(security_agent, security_result)
    )
    quality_agent.execute = quality_execute_mock or AsyncMock(
        return_value=_wrap_agent_result(quality_agent, quality_result)
    )
    report_agent.execute = report_execute_mock or AsyncMock(
        return_value=_wrap_agent_result(report_agent, report_result)
    )

    target = tmp_path / "module.py"
    target.write_text("def target_function():\n    return 1\n")

    result = await orchestrator.run(str(target))
    return result, orchestrator, snapshot_store, saga


@pytest.mark.asyncio
async def test_state_machine_rejects_invalid_transitions():
    orchestrator, *_ = _build_orchestrator_components()
    with pytest.raises(InvalidTransitionError):
        await orchestrator.rollback(STEP_REPORTING)


@pytest.mark.asyncio
async def test_pipeline_completes_and_records_snapshots(tmp_path):
    recording_store = RecordingSnapshotStore()
    result, orchestrator, snapshot_store, _ = await _run_sample_pipeline(
        tmp_path,
        snapshot_store=recording_store,
    )

    assert result.status == "completed"
    assert isinstance(result.report, AnalysisReport)
    assert result.report.executive_summary
    assert result.report.security_section
    assert result.report.quality_section
    assert result.report.recommendations

    workflow_id = recording_store.latest_workflow_id()
    assert workflow_id is not None
    history = await snapshot_store.history(workflow_id)
    assert len(history) >= 4
    assert [snapshot.step for snapshot in history][:4] == [
        STEP_PARSING,
        STEP_SCANNING,
        STEP_CHECKING,
        STEP_REPORTING,
    ]
    assert result.snapshot_ids == [snapshot.snapshot_id for snapshot in history]

    scanning_snapshot = history[1]
    recovered_state = await snapshot_store.load(scanning_snapshot.snapshot_id)
    assert recovered_state["current_step"] == STEP_SCANNING
    assert set(recovered_state["results"].keys()) == {STEP_PARSING, STEP_SCANNING}

    assert STEP_PARSING in result.step_results
    assert STEP_REPORTING in result.step_results


@pytest.mark.asyncio
async def test_validation_failure_triggers_rollback(tmp_path):
    bad_security = _sample_security_result(description="")
    recording_saga = RecordingSagaCoordinator()
    result, orchestrator, snapshot_store, saga = await _run_sample_pipeline(
        tmp_path,
        security_result=bad_security,
        saga=recording_saga,
    )

    assert result.status == "rolled_back"
    assert isinstance(result.error, str) and result.error.strip()
    assert recording_saga.compensation_results
    assert (
        recording_saga.compensation_results[-1].steps_compensated
        == [STEP_PARSING]
    )
    assert STEP_REPORTING not in result.step_results


@pytest.mark.asyncio
async def test_security_validation_rejects_invalid_severity(tmp_path):
    invalid_finding = SecurityFinding.model_construct(
        severity="unknown",
        location="module.py:3",
        description="Severity outside allowed set.",
        recommendation="Use critical/high/medium/low.",
    )
    invalid_security = SecurityResult.model_construct(findings=[invalid_finding])
    result, *_ = await _run_sample_pipeline(
        tmp_path,
        security_result=invalid_security,
    )

    assert result.status == "rolled_back"
    assert STEP_CHECKING not in result.step_results
    assert STEP_REPORTING not in result.step_results
    assert isinstance(result.error, str) and result.error.strip()


@pytest.mark.asyncio
async def test_parser_validation_requires_non_empty_output(tmp_path):
    empty_parse = ParseResult(functions=[], classes=[], imports=[], dependencies={})
    result, *_ = await _run_sample_pipeline(tmp_path, parse_result=empty_parse)

    assert result.status == "rolled_back"
    assert STEP_SCANNING not in result.step_results
    assert result.error is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "quality_result",
    [
        _sample_quality_result(score=150),
        _sample_quality_result(
            issues=[
                QualityIssue(
                    location="missing_symbol",
                    description="References a field that does not exist.",
                    severity="low",
                )
            ]
        ),
    ],
    ids=["invalid-score", "invalid-location"],
)
async def test_quality_validation_rejects_bad_scores_and_locations(
    tmp_path,
    quality_result,
):
    result, *_ = await _run_sample_pipeline(tmp_path, quality_result=quality_result)

    assert result.status == "rolled_back"
    assert STEP_REPORTING not in result.step_results
    assert STEP_SCANNING in result.step_results
    assert result.error is not None


@pytest.mark.asyncio
async def test_report_validation_requires_all_sections(tmp_path):
    incomplete_report = AnalysisReport(
        executive_summary=_sample_analysis_report().executive_summary,
        security_section={"findings": ["Secrets are flagged for rotation."]},
        quality_section={},
        recommendations=_sample_recommendations(),
    )
    result, *_ = await _run_sample_pipeline(tmp_path, report_result=incomplete_report)

    assert result.status == "rolled_back"
    assert STEP_REPORTING not in result.step_results
    assert result.error is not None


@pytest.mark.asyncio
async def test_agents_receive_previous_results_and_input_path(tmp_path):
    parse_result = _sample_parse_result()
    security_result = _sample_security_result()
    quality_result = _sample_quality_result()
    report_result = _sample_analysis_report()

    captured_tasks: dict[str, AgentTask] = {}

    def _build_capture(result: StepResult | AnalysisReport) -> AgentExecuteBuilder:
        def builder(agent: BaseAgent) -> AsyncMock:
            async def _execute(task: AgentTask) -> AgentResult:
                captured_tasks[agent.agent_id] = task
                return _wrap_agent_result(agent, result)

            return AsyncMock(side_effect=_execute)

        return builder

    await _run_sample_pipeline(
        tmp_path,
        parse_result=parse_result,
        security_result=security_result,
        quality_result=quality_result,
        report_result=report_result,
        parser_execute_builder=_build_capture(parse_result),
        security_execute_builder=_build_capture(security_result),
        quality_execute_builder=_build_capture(quality_result),
        report_execute_builder=_build_capture(report_result),
    )

    expected_path = str(tmp_path / "module.py")
    assert set(captured_tasks.keys()) == {"parser", "security", "quality", "report"}
    for task in captured_tasks.values():
        assert task.input_data.get("input_path") == expected_path

    security_results = _find_results_map(captured_tasks["security"].input_data)
    assert (
        _normalize_step_result(security_results[STEP_PARSING])
        == _normalize_step_result(parse_result)
    )

    quality_results = _find_results_map(captured_tasks["quality"].input_data)
    assert (
        _normalize_step_result(quality_results[STEP_PARSING])
        == _normalize_step_result(parse_result)
    )
    assert (
        _normalize_step_result(quality_results[STEP_SCANNING])
        == _normalize_step_result(security_result)
    )

    report_results = _find_results_map(captured_tasks["report"].input_data)
    assert (
        _normalize_step_result(report_results[STEP_CHECKING])
        == _normalize_step_result(quality_result)
    )

@pytest.mark.asyncio
async def test_saga_compensates_on_quality_failure(tmp_path):
    bad_quality = _sample_quality_result(score=150)
    recording_saga = RecordingSagaCoordinator()
    result, _, _, saga = await _run_sample_pipeline(
        tmp_path,
        quality_result=bad_quality,
        saga=recording_saga,
    )

    assert result.status == "rolled_back"
    assert STEP_REPORTING not in result.step_results
    assert recording_saga.compensation_results
    assert (
        recording_saga.compensation_results[-1].steps_compensated[:2]
        == [STEP_SCANNING, STEP_PARSING]
    )


@pytest.mark.asyncio
async def test_distributed_tracing_records_child_spans(tmp_path):
    provider = TracingManager.setup("orchestration-test-tracing", endpoint=None)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    result, orchestrator, snapshot_store, _ = await _run_sample_pipeline(
        tmp_path,
        tracer_provider=provider,
    )

    provider.force_flush()
    spans = exporter.get_finished_spans()
    parent_span = next((span for span in spans if span.parent is None), None)
    assert parent_span is not None
    child_spans = [
        span
        for span in spans
        if span.parent is not None and span.parent.span_id == parent_span.context.span_id
    ]
    assert child_spans
    expected_agent_names = {
        "Parser Agent",
        "Security Agent",
        "Quality Agent",
        "Report Agent",
    }
    expected_steps = {
        STEP_PARSING,
        STEP_SCANNING,
        STEP_CHECKING,
        STEP_REPORTING,
    }
    for span in child_spans:
        attrs = span.attributes
        agent_name = attrs.get("agent.name")
        assert agent_name in expected_agent_names
        step_name = attrs.get("step.name")
        assert step_name in expected_steps
        duration_value = attrs.get("duration_ms")
        assert isinstance(duration_value, (int, float))
        assert duration_value >= 0
    observed_agents = {span.attributes.get("agent.name") for span in child_spans}
    assert expected_agent_names <= observed_agents
    step_names = {span.attributes.get("step.name") for span in child_spans}
    assert expected_steps <= step_names


@pytest.mark.asyncio
async def test_pipeline_handles_circuit_breaker_open(tmp_path):
    parser_fail = AsyncMock(side_effect=CircuitOpenError("circuit breaker open"))
    result, orchestrator, snapshot_store, _ = await _run_sample_pipeline(
        tmp_path,
        parser_execute=parser_fail,
    )

    assert result.status == "failed"
    assert isinstance(result.error, str) and result.error.strip()
    assert STEP_CHECKING not in result.step_results


def _invoke_cli_entry(
    tmp_path,
    *,
    monkeypatch,
    pipeline_result: PipelineResult,
    capsys,
) -> tuple[int, str]:
    target = tmp_path / "module.py"
    target.write_text("def dummy():\n    return 1\n")

    class DummyOrchestrator:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, input_path: str) -> PipelineResult:
            return pipeline_result

    monkeypatch.setattr(
        "orchestration.code_analysis.orchestrator.CodeAnalysisOrchestrator",
        DummyOrchestrator,
    )
    monkeypatch.setattr(sys, "argv", ["orchestration.code_analysis", str(target)])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("orchestration.code_analysis", run_name="__main__")

    output = capsys.readouterr().out
    return exit_info.value.code, output


def test_cli_entry_prints_report_and_exits_successfully(monkeypatch, tmp_path, capsys):
    report = _sample_analysis_report()
    report.executive_summary = "CLI structured summary"
    pipeline_result = PipelineResult(
        status="completed",
        report=report,
        step_results={
            STEP_PARSING: _sample_parse_result(),
            STEP_SCANNING: _sample_security_result(),
            STEP_CHECKING: _sample_quality_result(),
        },
        error=None,
        duration_ms=0.0,
        snapshot_ids=[],
    )

    exit_code, output = _invoke_cli_entry(
        tmp_path,
        monkeypatch=monkeypatch,
        pipeline_result=pipeline_result,
        capsys=capsys,
    )

    assert exit_code == 0
    assert "cli structured summary" in output.lower()


def test_cli_entry_exits_nonzero_on_failure(monkeypatch, tmp_path, capsys):
    failure_result = PipelineResult(
        status="rolled_back",
        report=None,
        step_results={
            STEP_PARSING: _sample_parse_result(),
            STEP_SCANNING: _sample_security_result(),
        },
        error="Validation failure at quality gate",
        duration_ms=0.0,
        snapshot_ids=[],
    )

    exit_code, output = _invoke_cli_entry(
        tmp_path,
        monkeypatch=monkeypatch,
        pipeline_result=failure_result,
        capsys=capsys,
    )

    assert exit_code == 1
    assert "validation failure" in output.lower()


def test_agent_results_are_json_serializable():
    models: list[Any] = [
        _sample_parse_result(),
        _sample_security_result(),
        _sample_quality_result(),
        _sample_analysis_report(),
    ]
    for model in models:
        serialized = model.model_dump()
        assert isinstance(serialized, dict)
        dumped = model.model_dump_json()
        assert isinstance(dumped, str)
