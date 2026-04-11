# Spec: Orchestration — Code Analysis Pipeline

## Goal

Build an orchestrated multi-agent code analysis pipeline that demonstrates **why orchestration is the right pattern** for sequential, dependent workflows with validation gates and rollback requirements.

The pipeline takes a code file/directory as input and produces a comprehensive analysis report by routing through a sequence of specialized agents. An orchestrator controls the flow, validates outputs between steps, and can roll back (saga compensation) when a step fails.

Lives under `src/orchestration/code_analysis/`. Depends on `src/core/`.

## Source Files

The implementation creates these key modules:

- `src/orchestration/code_analysis/orchestrator.py` — CodeAnalysisOrchestrator, PipelineState, PipelineResult
- `src/orchestration/code_analysis/agents.py` — ParserAgent, SecurityAgent, QualityAgent, ReportAgent
- `src/orchestration/code_analysis/validation.py` — StepValidator, ValidationAgent, ValidationResult
- `src/orchestration/code_analysis/saga.py` — SagaCoordinator, CompensationResult
- `src/orchestration/code_analysis/models.py` — ParseResult, SecurityResult, QualityResult, AnalysisReport

## Requirements

### 1. Pipeline Stages

The orchestrator runs agents in this sequence:

```
Input (code path)
  → [1] Parser Agent        — extracts structure (functions, classes, imports, dependencies)
  → [2] Security Agent      — scans for vulnerabilities (OWASP top 10 patterns, hardcoded secrets, injection risks)
  → [3] Quality Agent       — checks code quality (complexity, duplication, naming, type coverage)
  → [4] Report Agent        — synthesizes all findings into a structured report
  → Output (analysis report)
```

Each step receives the output of the previous step plus the original input. If any step fails, the saga compensates all completed steps in reverse.

### 2. Orchestrator (`src/orchestration/code_analysis/orchestrator.py`)

#### 2.1 `CodeAnalysisOrchestrator`
- Implements a deterministic state machine with states: `PENDING`, `PARSING`, `SCANNING`, `CHECKING`, `REPORTING`, `COMPLETED`, `FAILED`, `ROLLING_BACK`
- Valid transitions are explicit and enforced (e.g., `PARSING` can go to `SCANNING` or `ROLLING_BACK`, never directly to `REPORTING`)
- On each transition:
  - Saves an immutable snapshot via `SnapshotStore` (the full pipeline state at that point)
  - Creates an OpenTelemetry span for the step
- Method `async run(input_path: str) -> PipelineResult` — drives the full pipeline
- Method `async rollback(from_step: str) -> None` — runs saga compensation

#### 2.2 `PipelineState`
- Pydantic model tracking: `current_step: str`, `input_path: str`, `results: dict[str, StepResult]`, `status: str`, `started_at: datetime`, `snapshots: list[str]`

#### 2.3 `PipelineResult`
- Pydantic model: `status: Literal["completed", "failed", "rolled_back"]`, `report: AnalysisReport | None`, `step_results: dict[str, StepResult]`, `error: str | None`, `duration_ms: float`, `snapshot_ids: list[str]`

### 3. Agents (`src/orchestration/code_analysis/agents/`)

Each agent extends `BaseAgent` from core:

#### 3.1 `ParserAgent`
- Reads source files, uses LLM to extract: list of functions (name, params, return type, line range), classes, imports, dependency graph
- Output: `ParseResult` pydantic model
- Compensation: no-op (read-only)

#### 3.2 `SecurityAgent`
- Receives `ParseResult` + source code
- Uses LLM to identify: hardcoded secrets, SQL injection risks, XSS vectors, insecure deserialization, command injection
- Each finding has: `severity: Literal["critical", "high", "medium", "low"]`, `location: str`, `description: str`, `recommendation: str`
- Output: `SecurityResult` with `findings: list[SecurityFinding]`
- Compensation: no-op (read-only)

#### 3.3 `QualityAgent`
- Receives `ParseResult` + source code
- Uses LLM to assess: cyclomatic complexity per function, code duplication, naming convention adherence, overall quality score (0-100)
- Output: `QualityResult` with `score: int`, `issues: list[QualityIssue]`, `metrics: dict`
- Compensation: no-op (read-only)

#### 3.4 `ReportAgent`
- Receives all previous results
- Synthesizes into a structured `AnalysisReport`: executive summary, security section, quality section, recommendations (prioritized)
- Output: `AnalysisReport` pydantic model
- Compensation: no-op (read-only)

### 4. Validation (`src/orchestration/code_analysis/validation.py`)

#### 4.1 `StepValidator`
- After each agent completes, the orchestrator runs validation before proceeding
- `async validate(step: str, result: StepResult) -> ValidationResult`
- `ValidationResult`: `valid: bool`, `errors: list[str]`, `warnings: list[str]`
- Validation rules:
  - `ParserAgent` output must contain at least one function or class (non-empty parse)
  - `SecurityAgent` findings must each have valid severity and non-empty description
  - `QualityAgent` score must be 0-100; issues must reference valid locations from the parse result
  - `ReportAgent` report must include all sections

#### 4.2 `ValidationAgent` (optional LLM-backed)
- An LLM agent that cross-checks one agent's output against the original input for consistency
- Demonstrates the "independent validation at each boundary" pattern that reduces error amplification from 17.2x to 4.4x

### 5. Saga / Compensation (`src/orchestration/code_analysis/saga.py`)

#### 5.1 `SagaCoordinator`
- Tracks completed steps and their compensating actions
- `register_step(step: str, compensate: Callable) -> None`
- `async compensate_all() -> CompensationResult` — runs compensations in reverse order
- `CompensationResult`: `steps_compensated: list[str]`, `failures: list[str]`

While this pipeline's agents are read-only (compensations are no-ops), the saga infrastructure must be fully functional because:
1. It demonstrates the pattern correctly
2. Compensation actions log what they would undo (for observability)
3. The same saga coordinator is reusable for use cases with side effects

### 6. Entry Point

- `python -m orchestration.code_analysis` accepts a path argument and runs the full pipeline
- Prints a structured report to stdout
- Exits 0 on success, 1 on failure (after rollback)

## Acceptance Criteria

1. **State machine enforces transitions**: Attempting an invalid transition (e.g., PENDING → REPORTING) raises `InvalidTransitionError`. Only valid transitions succeed.

2. **Pipeline runs end-to-end**: Given a Python file as input, the orchestrator runs all 4 agents in sequence and produces an `AnalysisReport` with all sections populated.

3. **Snapshots are saved at each step**: After a completed run, `SnapshotStore.history(workflow_id)` returns one snapshot per completed step (at least 4), and each snapshot's state is an immutable copy of the pipeline state at that point.

4. **Validation catches bad output**: If the `SecurityAgent` returns a finding with empty description, `StepValidator` rejects it and the pipeline enters ROLLING_BACK state.

5. **Saga compensation runs in reverse**: When step 3 (quality) fails, compensation runs for steps 2 and 1 in that order. `CompensationResult` lists both steps.

6. **Distributed tracing spans**: A completed pipeline run produces a parent span with child spans for each agent execution step. Spans include agent name, step name, and duration.

7. **Circuit breaker protects LLM calls**: If the LLM API is unavailable, the circuit breaker opens after the configured threshold and the pipeline fails gracefully with a clear error (not a raw exception).

8. **Each agent produces structured output**: Agent outputs conform to their respective Pydantic models and can be serialized to JSON.

9. **Pipeline state is recoverable from snapshots**: Given a snapshot from step 2, the pipeline state can be reconstructed showing completed steps 1-2 and remaining steps 3-4.
