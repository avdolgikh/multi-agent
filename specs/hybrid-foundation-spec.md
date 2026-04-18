# Spec: Hybrid Foundation — Models, Team, Project Orchestrator

> **Iteration 1 of 7** in Milestone 2 (see `specs/hybrid-analysis-spec.md`).
> Parent: `hybrid-analysis-spec.md`. This spec is self-contained and
> pipeline-runnable.

## Goal

Build the skeleton for the hybrid project-analysis pattern: Pydantic models,
event types, a `Team` abstraction, and a `ProjectOrchestrator` state machine
that drives three phases (DISCOVERY → DEEP_DIVE → SYNTHESIS) with validation
gates, snapshots, and saga compensation.

Agents are **stubs** in this iteration — they return canned `AgentResult`s so
the two-level coordination (orchestrator across phases, teams within phases)
can be validated without LLM calls. Real agents land in Iterations 2–4.

Lives under `src/hybrid/project_analysis/`. Depends on `src/core/`.

## Source Files

The implementation creates these modules:

- `src/hybrid/__init__.py` — package marker
- `src/hybrid/project_analysis/__init__.py` — re-exports public API
- `src/hybrid/project_analysis/models.py` — `ProjectStructure`,
  `DependencyReport`, `SecurityReport`, `QualityReport`, `ProjectReport`,
  `PhaseResult`, `ProjectAnalysisState`
- `src/hybrid/project_analysis/events.py` — `TeamComplete`, `PhaseValidated`,
  `PhaseFailed`, and event topic constants
- `src/hybrid/project_analysis/team.py` — `Team` abstraction
- `src/hybrid/project_analysis/orchestrator.py` — `ProjectOrchestrator`,
  `Phase`, `InvalidPhaseTransitionError`, `PhaseValidator`
- `src/hybrid/project_analysis/stubs.py` — stub agent implementations
  (`StubAgent`) returning canned results, used by this iteration's tests and
  replaced by real LLM-backed agents in Iterations 2–4

> Note: `stubs.py` is added here to the master spec's 6-file list so stub
> agents are an importable, testable module rather than inline fixtures.

## Requirements

### 1. Models (`models.py`)

All models are Pydantic `BaseModel`s. Fields are defined in this iteration
without business logic — later iterations populate them from LLM output.

#### 1.1 `ProjectStructure`
- `files: list[str]` — discovered source files
- `modules: list[str]` — module names / boundaries
- `functions: list[dict[str, Any]]` — at minimum `{name, module, line}`
- `classes: list[dict[str, Any]]` — at minimum `{name, module, line}`
- `imports: list[dict[str, str]]` — `{source, target}` edges
- `dependency_graph: dict[str, list[str]]` — adjacency list of module-to-module
  imports

#### 1.2 `DependencyReport`
- `packages: list[dict[str, Any]]` — `{name, version, license}`
- `vulnerabilities: list[dict[str, Any]]` — `{package, cve, severity,
  description}`

#### 1.3 `SecurityReport`
- `findings: list[dict[str, Any]]` — `{severity, category, location,
  description, recommendation}`
- `severity_counts: dict[str, int]` — counts keyed by
  `critical|high|medium|low`

#### 1.4 `QualityReport`
- `score: int` — 0–100 overall score
- `issues: list[dict[str, Any]]` — `{category, location, description}`
- `metrics: dict[str, Any]` — e.g. complexity, duplication, coverage

#### 1.5 `ProjectReport`
- `structure: ProjectStructure`
- `dependencies: DependencyReport`
- `security: SecurityReport`
- `quality: QualityReport`
- `summary: str`
- `recommendations: list[str]`

#### 1.6 `PhaseResult`
- `phase: Literal["DISCOVERY", "DEEP_DIVE", "SYNTHESIS"]`
- `team_outputs: dict[str, dict[str, Any]]` — `{team_name: team_output}`
- `started_at: datetime`
- `completed_at: datetime | None`
- `status: Literal["running", "completed", "failed"]`
- `error: str | None = None`

#### 1.7 `ProjectAnalysisState`
- `workflow_id: str`
- `project_path: str`
- `current_phase: str` — one of the `Phase` values (see §4.1)
- `phase_results: dict[str, PhaseResult]` — keyed by phase name
- `snapshots: list[str]` — snapshot IDs in order
- `started_at: datetime`
- `status: Literal["running", "completed", "failed", "rolling_back"]`

### 2. Events (`events.py`)

All events are Pydantic models suitable for use as `Message.payload`. Topic
constants are module-level strings.

#### 2.1 Topic constants
- `TOPIC_TEAM_COMPLETE = "project:team:complete"` — team → orchestrator
- `TOPIC_PHASE_VALIDATED = "project:phase:validated"` — orchestrator broadcast
- `TOPIC_PHASE_FAILED = "project:phase:failed"` — orchestrator broadcast
- `def team_topic(team_name: str) -> str` — returns `f"team:{team_name}:*"`
  base namespace helper; concrete topics use
  `f"team:{team_name}:{event_name}"`

#### 2.2 `TeamComplete` event
- `team_name: str`
- `phase: str`
- `output: dict[str, Any]` — team's aggregated result
- `agents_run: list[str]` — agent IDs that contributed
- `duration_ms: float`

#### 2.3 `PhaseValidated` event
- `phase: str`
- `next_phase: str`
- `snapshot_id: str`

#### 2.4 `PhaseFailed` event
- `phase: str`
- `error: str`
- `will_rollback: bool`

### 3. Team Abstraction (`team.py`)

#### 3.1 `Team`
```python
Team(
    name: str,
    agents: Sequence[BaseAgent],
    bus: MessageBus,
    event_store: EventStore,
    aggregator: Callable[[list[AgentResult]], dict[str, Any]],
)
```

Responsibilities:
- Owns a topic namespace: all intra-team events live under
  `team:{name}:...`. Team subscriptions MUST use this prefix.
- `async run(task: AgentTask) -> TeamComplete` — executes all agents
  concurrently (`asyncio.gather`), aggregates their results via the
  `aggregator` callable, publishes a `TeamComplete` message on
  `TOPIC_TEAM_COMPLETE`, records one `Event` per agent result to the event
  store on stream `f"team:{name}"`, and returns the `TeamComplete`.
- Propagates the `task.trace_context` down to every agent via each agent's
  own `AgentTask.trace_context`.
- Creates a `Team:{name}` span that parents every agent `execute()` span.
- If any agent returns `status="failure"`, `run()` still publishes
  `TeamComplete` but marks the team output with an `error` field and
  includes the failing agent IDs.

#### 3.2 Aggregator contract
An aggregator is a pure function receiving all `AgentResult`s for a single
team run and returning the team's output dict. Concrete aggregators are
supplied by iteration 2+ (one per real team). For iteration 1's tests, a
simple pass-through aggregator that merges `output_data` dicts is sufficient.

### 4. Project Orchestrator (`orchestrator.py`)

#### 4.1 `Phase` constants (string enum or class attributes)
- `DISCOVERY = "DISCOVERY"`
- `DEEP_DIVE = "DEEP_DIVE"`
- `SYNTHESIS = "SYNTHESIS"`
- `COMPLETED = "COMPLETED"`
- `FAILED = "FAILED"`
- `ROLLING_BACK = "ROLLING_BACK"`

#### 4.2 Valid transitions
```
PENDING       → DISCOVERY, FAILED
DISCOVERY     → DEEP_DIVE, ROLLING_BACK, FAILED
DEEP_DIVE     → SYNTHESIS, ROLLING_BACK, FAILED
SYNTHESIS     → COMPLETED, ROLLING_BACK, FAILED
ROLLING_BACK  → FAILED
```
Any other transition raises `InvalidPhaseTransitionError`.

#### 4.3 `ProjectOrchestrator`
```python
ProjectOrchestrator(
    discovery_teams: Sequence[Team],
    deepdive_teams: Sequence[Team],
    synthesis_agent: BaseAgent,
    bus: MessageBus,
    event_store: EventStore,
    snapshot_store: SnapshotStore,
    validator: PhaseValidator,
)
```

Public methods:
- `async run(project_path: str) -> ProjectReport` — drives the full pipeline.
  Generates a `workflow_id` (uuid4), creates a root span
  `ProjectOrchestrator.run`, enters DISCOVERY.
- `async rollback(from_phase: str) -> None` — saga compensation. Walks
  completed phases in reverse order. Preserves phase outputs (they are
  read-only — compensations log only, as in `orchestration/code_analysis`).

Phase execution rules:
- **DISCOVERY / DEEP_DIVE** — `await asyncio.gather(*(team.run(task) for
  team in teams))` runs teams concurrently under a `Phase.{NAME}` span.
  Orchestrator awaits all `TeamComplete`s before validating.
- **SYNTHESIS** — single-agent phase. Orchestrator calls
  `synthesis_agent.execute(task)` with all prior phase outputs in
  `task.input_data`.
- Between phases: call `validator.validate(phase, phase_result)`. On
  success, save a `Snapshot` of the full `ProjectAnalysisState`, publish
  `PhaseValidated`, transition. On failure, publish `PhaseFailed`,
  transition to `ROLLING_BACK`, call `rollback()`, terminate as `FAILED`.
- Snapshot at every phase boundary — one per completed phase plus one at
  `COMPLETED`.

#### 4.4 `PhaseValidator`
- `async validate(phase: str, result: PhaseResult) -> ValidationResult`
- `ValidationResult`: `valid: bool`, `errors: list[str]`
- Validation rules for this iteration:
  - **DISCOVERY**: both expected team names appear in `result.team_outputs`
    AND each team output is non-empty (at least one non-empty field).
    Empty outputs block the transition — this exercises the gate.
  - **DEEP_DIVE**: same — both expected team names present, non-empty.
  - **SYNTHESIS**: the agent returned a `ProjectReport` with all five
    sub-sections populated.

### 5. Stub Agents (`stubs.py`)

#### 5.1 `StubAgent`
A `BaseAgent` subclass whose `execute()`:
- Does not call `call_llm()`.
- Creates an `{AgentName}.execute` span under the team span (so tracing
  assertions work).
- Returns an `AgentResult` with a configurable `output_data` payload (set
  via constructor kwarg `canned_output: dict[str, Any]`).
- Supports a `fail: bool = False` flag that makes `execute()` return
  `status="failure"`, used to test validation-gate rejection.

#### 5.2 `make_stub_team(name, agents_spec, bus, event_store) -> Team`
Helper that constructs a `Team` from a list of `(agent_id, canned_output)`
tuples, wiring in a simple merging aggregator. Used in tests.

### 6. Observability

All spans use `opentelemetry.trace` via the existing `core.tracing` helpers.

- `ProjectOrchestrator.run` — root span. Attributes: `workflow.id`,
  `project.path`.
- `Phase.{DISCOVERY|DEEP_DIVE|SYNTHESIS}` — phase span, child of root.
  Attributes: `phase`, `workflow.id`.
- `Team:{name}` — team span, child of the current phase span. Attributes:
  `team.name`, `phase`.
- `{AgentName}.execute` — agent span, child of team span. Attributes:
  `agent.id`, `agent.name` (already set by `BaseAgent.call_llm`; stubs
  must set the same attributes manually since they don't call the LLM).
- Trace context propagation uses `inject_context()` when building
  `AgentTask.trace_context`, and `extract_context()` on receipt.

## Acceptance Criteria

1. **State machine enforces transitions.** Attempting `PENDING → DEEP_DIVE`
   (or any other invalid hop) raises `InvalidPhaseTransitionError`. Each
   valid transition in §4.2 succeeds.

2. **Full happy-path runs with stub teams.** Given two stub Discovery teams
   (`structure`, `dependencies`), two stub Deep-Dive teams (`security`,
   `quality`), and a stub synthesis agent whose canned output is a
   populated `ProjectReport`, `ProjectOrchestrator.run(path)` returns a
   `ProjectReport` and the final state is `COMPLETED`.

3. **Concurrent team execution.** In a DISCOVERY phase with two teams that
   each sleep 50 ms, the phase's wall-clock duration is closer to 50 ms
   than 100 ms (tolerance ≤ 80 ms). Verified by timing in tests, not by
   trace spans.

4. **`TeamComplete` published and consumed.** When a team finishes,
   `TOPIC_TEAM_COMPLETE` receives a `TeamComplete` message. A test
   subscribes to that topic and asserts both expected teams' messages are
   delivered within the DISCOVERY phase.

5. **Validation gate blocks empty DISCOVERY.** If both Discovery stub teams
   return empty `output_data`, the `PhaseValidator` marks DISCOVERY
   invalid, orchestrator transitions to `ROLLING_BACK` then `FAILED`, and
   no DEEP_DIVE spans are created.

6. **Snapshots at every boundary.** On a successful run,
   `SnapshotStore.history(workflow_id)` returns ≥ 4 snapshots (post-
   DISCOVERY, post-DEEP_DIVE, post-SYNTHESIS, and `COMPLETED`), ordered by
   timestamp. Each snapshot's `state` deserialises back into
   `ProjectAnalysisState` (Pydantic round-trip).

7. **Saga compensation preserves prior results.** Configure DEEP_DIVE's
   quality team stub with `fail=True`. Run the orchestrator: DISCOVERY
   completes, DEEP_DIVE validation fails, rollback runs. After run,
   `phase_results["DISCOVERY"]` is still present and populated; final
   status is `FAILED`.

8. **Span tree matches the observability contract.** Using a mock tracer
   (or an in-memory span exporter) a successful run produces, in order:
   `ProjectOrchestrator.run` (root) → `Phase.DISCOVERY` → `Team:structure`,
   `Team:dependencies` (siblings) → stub agent spans under each team →
   `Phase.DEEP_DIVE` → `Team:security`, `Team:quality` → agent spans →
   `Phase.SYNTHESIS` → synthesis-agent span. No orphan spans.

9. **All externals mocked; no network calls.** Tests use `InMemoryBus`,
   `InMemoryEventStore`, `SnapshotStore`, and `StubAgent`. `tests/conftest.py`
   autouse fixtures must remain effective (OTLP / Traceloop stubs
   unchanged). No LLM, HTTP, or subprocess calls occur during the test
   suite.

10. **Public API re-exported.** `from hybrid.project_analysis import
    ProjectOrchestrator, Team, PhaseValidator, ProjectReport,
    ProjectStructure, DependencyReport, SecurityReport, QualityReport,
    TeamComplete, Phase` succeeds.

## Out of Scope (deferred to later iterations)

- Real LLM-backed agents — Iterations 2–4.
- `__main__.py` / CLI entry point — Iteration 4 (`hybrid-deepdive-synthesis`).
- `init_observability("hybrid.project_analysis")` wiring — Iteration 4
  (when CLI lands).
- Intra-team choreography (agents reacting to each other's events) —
  Iteration 2 onwards. This iteration's `Team` only needs to run agents
  concurrently and aggregate; subscription-based agent coordination is
  not required yet.
- Circuit breakers around team execution — deferred; `BaseAgent` already
  wraps LLM calls in a breaker for real agents.
