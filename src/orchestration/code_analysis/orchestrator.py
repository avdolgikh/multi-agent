from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from opentelemetry import trace
from pydantic import BaseModel, Field, ConfigDict

from core.agents import AgentResult, AgentTask, BaseAgent
from core.resilience import CircuitOpenError
from core.state import SnapshotStore
from core.tracing import inject_context

from .agents import ParserAgent, QualityAgent, ReportAgent, SecurityAgent
from .models import AnalysisReport, ParseResult, QualityResult, SecurityResult, StepResult
from .saga import SagaCoordinator
from .validation import StepValidator

__all__ = [
    "CodeAnalysisOrchestrator",
    "InvalidTransitionError",
    "PipelineState",
    "PipelineResult",
]


class InvalidTransitionError(RuntimeError):
    """Raised when an illegal state transition is attempted."""


class StepValidationError(RuntimeError):
    def __init__(self, step: str, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.step = step
        self.errors = errors


class PipelineState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    current_step: str
    input_path: str
    results: dict[str, StepResult] = Field(default_factory=dict)
    status: str
    started_at: datetime
    snapshots: list[str] = Field(default_factory=list)


class PipelineResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: Literal["completed", "failed", "rolled_back"]
    report: AnalysisReport | None = None
    step_results: dict[str, StepResult] = Field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0
    snapshot_ids: list[str] = Field(default_factory=list)


class CodeAnalysisOrchestrator:
    STEP_PARSING = "PARSING"
    STEP_SCANNING = "SCANNING"
    STEP_CHECKING = "CHECKING"
    STEP_REPORTING = "REPORTING"

    STATE_PENDING = "PENDING"
    STATE_COMPLETED = "COMPLETED"
    STATE_FAILED = "FAILED"
    STATE_ROLLING_BACK = "ROLLING_BACK"

    _STEP_MODELS = {
        STEP_PARSING: ParseResult,
        STEP_SCANNING: SecurityResult,
        STEP_CHECKING: QualityResult,
        STEP_REPORTING: AnalysisReport,
    }

    _VALID_TRANSITIONS = {
        STATE_PENDING: {STEP_PARSING, STATE_FAILED},
        STEP_PARSING: {STEP_SCANNING, STATE_ROLLING_BACK, STATE_FAILED},
        STEP_SCANNING: {STEP_CHECKING, STATE_ROLLING_BACK, STATE_FAILED},
        STEP_CHECKING: {STEP_REPORTING, STATE_ROLLING_BACK, STATE_FAILED},
        STEP_REPORTING: {STATE_COMPLETED, STATE_ROLLING_BACK, STATE_FAILED},
        STATE_ROLLING_BACK: {STATE_FAILED},
    }

    def __init__(
        self,
        *,
        parser: ParserAgent,
        security: SecurityAgent,
        quality: QualityAgent,
        report: ReportAgent,
        validator: StepValidator,
        saga: SagaCoordinator,
        snapshot_store: SnapshotStore,
        tracer_provider=None,
    ) -> None:
        self.parser = parser
        self.security = security
        self.quality = quality
        self.report = report
        self.validator = validator
        self.saga = saga
        self.snapshot_store = snapshot_store
        if tracer_provider is not None:
            trace.set_tracer_provider(tracer_provider)
        self._tracer = trace.get_tracer("orchestration.code_analysis")
        self._state: PipelineState | None = None
        self._workflow_id: str | None = None

    async def run(self, input_path: str) -> PipelineResult:
        workflow_id = str(uuid4())
        self._workflow_id = workflow_id
        state = PipelineState(
            current_step=self.STATE_PENDING,
            input_path=input_path,
            results={},
            status=self.STATE_PENDING,
            started_at=datetime.now(timezone.utc),
            snapshots=[],
        )
        self._state = state
        start_time = time.perf_counter()
        with self._tracer.start_as_current_span("code_analysis.pipeline") as span:
            span.set_attribute("workflow.id", workflow_id)
            span.set_attribute("input.path", input_path)
            try:
                report_result = await self._execute_pipeline(state)
            except StepValidationError as exc:
                await self.rollback(state.current_step)
                duration_ms = (time.perf_counter() - start_time) * 1000
                return PipelineResult(
                    status="rolled_back",
                    report=None,
                    step_results=dict(state.results),
                    error=str(exc),
                    duration_ms=duration_ms,
                    snapshot_ids=list(state.snapshots),
                )
            except CircuitOpenError as exc:
                await self._transition_to(self.STATE_FAILED)
                state.status = self.STATE_FAILED
                duration_ms = (time.perf_counter() - start_time) * 1000
                return PipelineResult(
                    status="failed",
                    report=None,
                    step_results=dict(state.results),
                    error=str(exc),
                    duration_ms=duration_ms,
                    snapshot_ids=list(state.snapshots),
                )
            except Exception as exc:  # noqa: BLE001
                await self._handle_unexpected_failure(exc)
                duration_ms = (time.perf_counter() - start_time) * 1000
                status = "rolled_back" if state.results else "failed"
                return PipelineResult(
                    status=status,
                    report=None,
                    step_results=dict(state.results),
                    error=str(exc),
                    duration_ms=duration_ms,
                    snapshot_ids=list(state.snapshots),
                )

        await self._transition_to(self.STATE_COMPLETED)
        state.status = self.STATE_COMPLETED
        final_snapshot = await self._save_state_snapshot(self.STATE_COMPLETED, state)
        state.snapshots.append(final_snapshot)
        duration_ms = (time.perf_counter() - start_time) * 1000
        return PipelineResult(
            status="completed",
            report=report_result,
            step_results=dict(state.results),
            error=None,
            duration_ms=duration_ms,
            snapshot_ids=list(state.snapshots),
        )

    async def rollback(self, from_step: str) -> None:
        if self._state is None:
            raise InvalidTransitionError("Pipeline state is not initialized")
        await self._transition_to(self.STATE_ROLLING_BACK)
        if self._state is not None:
            self._state.status = self.STATE_ROLLING_BACK
        await self.saga.compensate_all()

    async def _execute_pipeline(self, state: PipelineState) -> AnalysisReport:
        await self._run_step(self.STEP_PARSING, self.parser, state)
        await self._run_step(self.STEP_SCANNING, self.security, state)
        await self._run_step(self.STEP_CHECKING, self.quality, state)
        report_result = await self._run_step(self.STEP_REPORTING, self.report, state)
        if not isinstance(report_result, AnalysisReport):
            raise TypeError("Report agent did not return an AnalysisReport")
        return report_result

    async def _run_step(
        self,
        step: str,
        agent: BaseAgent,
        state: PipelineState,
    ) -> StepResult:
        await self._transition_to(step)
        task = self._build_task(step, state)
        with self._tracer.start_as_current_span(f"{step}.execute") as span:
            span.set_attribute("agent.name", agent.name)
            span.set_attribute("step.name", step)
            step_start = time.perf_counter()
            agent_result = await agent.execute(task)
            span.set_attribute("duration_ms", (time.perf_counter() - step_start) * 1000)
        step_result = self._extract_step_result(step, agent_result)
        validation = await self.validator.validate(
            step,
            step_result,
            previous_results=state.results,
            input_path=state.input_path,
        )
        if not validation.valid:
            raise StepValidationError(step, validation.errors)
        await self._register_compensation(step)
        state.results[step] = step_result
        state.current_step = step
        snapshot_id = await self._save_state_snapshot(step, state)
        state.snapshots.append(snapshot_id)
        return step_result

    async def _register_compensation(self, step: str) -> None:
        async def _compensate() -> None:
            with self._tracer.start_as_current_span("saga.compensation") as span:
                span.set_attribute("step", step)
                span.add_event("compensate", {"message": f"Would undo effects of {step}"})

        await self.saga.register_step(step, _compensate)

    def _build_task(self, step: str, state: PipelineState) -> AgentTask:
        return AgentTask(
            task_id=f"{self._workflow_id}-{step.lower()}",
            input_data={
                "input_path": state.input_path,
                "results": dict(state.results),
                "step": step,
            },
            metadata={"step": step, "workflow_id": self._workflow_id},
            trace_context=inject_context(),
        )

    def _extract_step_result(self, step: str, agent_result: AgentResult) -> StepResult:
        payload = agent_result.output_data.get("result")
        if payload is None:
            raise ValueError(f"Agent {agent_result.agent_id} did not return a result payload")
        model_type = self._STEP_MODELS.get(step)
        if model_type and isinstance(payload, model_type):
            return payload
        if hasattr(payload, "model_dump"):
            return payload  # type: ignore[return-value]
        if isinstance(payload, dict) and model_type is not None:
            return model_type.model_validate(payload)
        raise TypeError(f"Unsupported result payload type for step {step}")

    async def _save_state_snapshot(self, step: str, state: PipelineState) -> str:
        if self._workflow_id is None:
            raise RuntimeError("Workflow is not initialized")
        serialized_state = {
            "current_step": state.current_step,
            "input_path": state.input_path,
            "results": {
                key: value.model_dump() if hasattr(value, "model_dump") else value
                for key, value in state.results.items()
            },
            "status": state.status,
            "started_at": state.started_at.isoformat(),
        }
        return await self.snapshot_store.save(self._workflow_id, step, serialized_state)

    async def _transition_to(self, next_state: str) -> None:
        if self._state is None:
            raise RuntimeError("Pipeline state is not initialized")
        current = self._state.current_step
        allowed = self._VALID_TRANSITIONS.get(current, set())
        if next_state not in allowed:
            raise InvalidTransitionError(f"Cannot transition from {current} to {next_state}")
        self._state.current_step = next_state

    async def _handle_unexpected_failure(self, exc: Exception) -> None:
        if self._state is None:
            return
        if self._state.results:
            await self.rollback(self._state.current_step)
        else:
            await self._transition_to(self.STATE_FAILED)
            self._state.status = self.STATE_FAILED
