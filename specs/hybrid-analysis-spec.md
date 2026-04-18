# Milestone 2: Hybrid Project Analysis + Pattern Comparison

> **Status: ACTIVE.** Broken into 7 pipeline-runnable iterations (sub-specs).

## Goal

Demonstrate that multi-agent AI systems are **distributed systems** — inheriting
their nuances, failure modes, and architectural patterns. This milestone adds
two capabilities:

1. **Hybrid pattern** — orchestration *between* teams + choreography *within*
   teams (`src/hybrid/project_analysis/`)
2. **Pattern comparison** — the same code-review task executed via pure
   orchestration and pure choreography side-by-side, with measurable
   differences (`src/comparison/code_review/`)

### Why this matters

Multi-agent systems face the same challenges as any distributed system:
coordination, partial failure, observability, state management. This milestone
makes those challenges explicit and observable:

- **Coordination**: orchestrator gates phases; teams self-coordinate via events
- **Partial failure**: saga compensation across phases; DLQ for agent errors
- **Observability**: every agent, team, phase, and LLM call produces traced
  spans visible in Phoenix — structured logs with distributed traces
- **State**: event-sourced history + snapshots at phase boundaries

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                   PROJECT ORCHESTRATOR                    │
│   (phase transitions, validation gates, saga rollback)   │
│                                                          │
│   Phase 1: DISCOVERY     Phase 2: DEEP_DIVE    Phase 3  │
│   ┌───────────────┐     ┌───────────────┐     ┌───────┐│
│   │ Structure     │     │ Security      │     │Report ││
│   │ Team (choreo) │────▶│ Team (choreo) │────▶│Agent  ││
│   ├───────────────┤     ├───────────────┤     └───────┘│
│   │ Dependencies  │     │ Quality       │              │
│   │ Team (choreo) │     │ Team (choreo) │              │
│   └───────────────┘     └───────────────┘              │
└──────────────────────────────────────────────────────────┘
```

- **Between phases** — Orchestration. The `ProjectOrchestrator` controls
  DISCOVERY → DEEP_DIVE → SYNTHESIS transitions. Validation gates between
  phases. Snapshots at every boundary. Saga compensation on failure.
- **Within teams** — Choreography. Agents communicate via events on the
  `MessageBus`. The orchestrator does not direct individual agents inside a
  team. Teams publish `TeamComplete` when done.

---

## Existing Infrastructure (what we build on)

All new code imports from `src/core/`. No new infrastructure is created — only
new use-case modules under `src/hybrid/` and `src/comparison/`.

| Layer | Module | Key exports |
|-------|--------|-------------|
| Agents | `core.agents` | `BaseAgent`, `AgentTask`, `AgentResult`, `LLMResponse`, `Tool` |
| Messaging | `core.messaging` | `MessageBus` (protocol), `InMemoryBus`, `Message` |
| State | `core.state` | `EventStore`, `InMemoryEventStore`, `Event`, `SnapshotStore`, `Snapshot` |
| Tracing | `core.tracing` | `TracingManager`, `@traced`, `inject_context()`, `extract_context()` |
| Resilience | `core.resilience` | `CircuitBreaker`, `RetryPolicy`, `DeadLetterQueue` |
| Observability | `core.observability` | `init_observability(service_name)` — OTLP export + atexit flush |

**Patterns to reuse:**
- Orchestration pipeline pattern → `src/orchestration/code_analysis/orchestrator.py`
  (`CodeAnalysisOrchestrator`, `PipelineState`, `StepValidator`, `SagaCoordinator`)
- Choreography event pattern → `src/choreography/research/`
  (`BaseChoreographyAgent`, `ResearchEventPublisher`, event hierarchy,
  `SourceExhausted` completion signaling, `AggregatorAgent` wait-and-synthesize)

---

## Observability Contract

Every iteration must satisfy these observability requirements:

1. **`@traced` on every agent `execute()`** — creates a span per agent invocation
2. **Trace context propagation** — parent→child spans across orchestrator→team→agent
   via `inject_context()` / `extract_context()` on `AgentTask.trace_context`
3. **Span attributes** — each span sets `agent.id`, `agent.name`, `agent.model`,
   `agent.provider`; orchestrator spans add `workflow.id`, `phase`; team spans
   add `team.name`
4. **`init_observability(service_name)`** in every `__main__.py` entry point,
   with `openinference.project.name` for Phoenix project grouping
5. **Atexit flush** — already handled by `init_observability`, but short-lived
   runs must not skip it (lesson from observability-phase1)
6. **Testable** — tests mock OTLP export (autouse fixtures in `tests/conftest.py`);
   no network calls

**Expected Phoenix trace structure for a full hybrid run:**

```
ProjectOrchestrator.run                          ← root span
├── Phase.DISCOVERY                              ← phase span
│   ├── Team:structure                           ← team span
│   │   ├── FileTreeAgent.execute                ← agent span
│   │   │   └── FileTreeAgent.llm                ← LLM call span
│   │   ├── ModuleAgent.execute
│   │   │   └── ModuleAgent.llm
│   │   └── DependencyGraphAgent.execute
│   │       └── DependencyGraphAgent.llm
│   └── Team:dependencies                        ← concurrent with structure
│       ├── PackageAgent.execute
│       │   └── PackageAgent.llm
│       └── VulnerabilityAgent.execute
│           └── VulnerabilityAgent.llm
├── Phase.DEEP_DIVE
│   ├── Team:security
│   │   └── ...agent spans...
│   └── Team:quality
│       └── ...agent spans...
└── Phase.SYNTHESIS
    └── ReportAgent.execute
        └── ReportAgent.llm
```

---

## Iteration Plan

7 iterations, strict dependency chain. Each iteration = one pipeline-runnable
sub-spec in `specs/`. Each is self-contained with its own source files,
acceptance criteria, and tests.

| # | Task ID | Scope | Source location |
|---|---------|-------|-----------------|
| 1 | `hybrid-foundation` | Models + Team abstraction + ProjectOrchestrator state machine (stub agents) | `src/hybrid/project_analysis/` |
| 2 | `hybrid-structure-team` | Discovery: Structure Team — 3 agents with intra-team choreography | `src/hybrid/project_analysis/` |
| 3 | `hybrid-dependencies-team` | Discovery: Dependencies Team — 2 agents, concurrent with Structure Team | `src/hybrid/project_analysis/` |
| 4 | `hybrid-deepdive-synthesis` | Deep Dive teams + Synthesis phase — full hybrid pipeline end-to-end | `src/hybrid/project_analysis/` |
| 5 | `comparison-orchestrated-review` | Common models + orchestrated code review (4-agent pipeline) | `src/comparison/code_review/` |
| 6 | `comparison-choreographed-review` | Choreographed code review (3 reviewers + aggregator) | `src/comparison/code_review/` |
| 7 | `comparison-harness` | Comparison harness, metrics, CLI entry points for both packages | `src/comparison/code_review/` |

---

## Iteration Details

### Iteration 1: `hybrid-foundation`

**Goal:** Build the skeleton — models, Team abstraction, ProjectOrchestrator
state machine. Prove two-level coordination works with stub agents.

**Source files:**
- `src/hybrid/__init__.py`
- `src/hybrid/project_analysis/__init__.py`
- `src/hybrid/project_analysis/models.py` — all Pydantic models
- `src/hybrid/project_analysis/events.py` — TeamComplete, PhaseValidated, etc.
- `src/hybrid/project_analysis/team.py` — Team abstraction
- `src/hybrid/project_analysis/orchestrator.py` — ProjectOrchestrator

**Delivers:**
- `ProjectStructure`, `DependencyReport`, `SecurityReport`, `QualityReport`,
  `ProjectReport` — Pydantic models (fields defined, no agent logic yet)
- `Team(name, agents, bus)` — manages agent lifecycle, topic namespace
  `team:{name}:*`, publishes `TeamComplete` when all agents signal done
- `ProjectOrchestrator` — state machine (DISCOVERY → DEEP_DIVE → SYNTHESIS →
  COMPLETED / FAILED), phase transitions, inter-phase validation gates,
  snapshots at boundaries, saga compensation
- Stub agents that return canned results (no LLM calls)

**Acceptance criteria:**
1. Orchestrator transitions through all phases with stub agents
2. `TeamComplete` events published and received by orchestrator
3. Inter-phase validation blocks DEEP_DIVE if DISCOVERY produced empty results
4. `SnapshotStore.history(workflow_id)` has one snapshot per phase transition
5. Saga compensation preserves Phase 1 results when Phase 2 fails
6. All spans traced: root → phase → team (verified via mock tracer)

---

### Iteration 2: `hybrid-structure-team`

**Goal:** Replace stub agents in the Structure Team with real LLM-backed agents
that communicate via events within the team.

**Source files:**
- `src/hybrid/project_analysis/agents/structure.py` — FileTreeAgent,
  ModuleAgent, DependencyGraphAgent

**Delivers:**
- `FileTreeAgent` — maps project file structure, publishes findings as events
- `ModuleAgent` — identifies module boundaries, reacts to FileTree findings
- `DependencyGraphAgent` — maps import relationships, cross-references both
- Intra-team choreography: agents subscribe to each other's events on the bus
  (reuses `BaseChoreographyAgent` pattern from research spec)
- Output: populated `ProjectStructure` model

**Acceptance criteria:**
1. Structure Team agents communicate via events (at least 2 events per agent
   visible in event store)
2. `ModuleAgent` reacts to `FileTreeAgent` findings (event-driven, not called
   by orchestrator)
3. Team produces a valid `ProjectStructure` with functions, classes, imports
4. All agent spans are children of the `Team:structure` span
5. LLM calls mocked in tests

---

### Iteration 3: `hybrid-dependencies-team`

**Goal:** Add the Dependencies Team. Both Discovery teams run concurrently.

**Source files:**
- `src/hybrid/project_analysis/agents/dependencies.py` — PackageAgent,
  VulnerabilityAgent

**Delivers:**
- `PackageAgent` — analyzes external dependencies (versions, licenses)
- `VulnerabilityAgent` — checks known vulnerabilities, reacts to PackageAgent
  findings
- Output: populated `DependencyReport` model
- Concurrent execution: Structure Team and Dependencies Team run in parallel
  within the DISCOVERY phase

**Acceptance criteria:**
1. Both teams run concurrently (their spans overlap in time)
2. Orchestrator waits for both `TeamComplete` events before advancing
3. Dependencies Team produces a valid `DependencyReport`
4. `VulnerabilityAgent` reacts to `PackageAgent` findings via events
5. LLM calls mocked in tests

---

### Iteration 4: `hybrid-deepdive-synthesis`

**Goal:** Complete the hybrid pipeline — Deep Dive phase (Security + Quality
teams) and Synthesis phase (ReportAgent). Full end-to-end test.

**Source files:**
- `src/hybrid/project_analysis/agents/security.py` — Security Team agents
- `src/hybrid/project_analysis/agents/quality.py` — Quality Team agents
- `src/hybrid/project_analysis/agents/report.py` — ReportAgent
- `src/hybrid/project_analysis/agents/__init__.py` — re-exports
- `src/hybrid/project_analysis/__main__.py` — CLI entry point

**Delivers:**
- Security Team: multiple agents examining different security dimensions via
  events → `SecurityReport`
- Quality Team: multiple agents assessing different quality dimensions via
  events → `QualityReport`
- `ReportAgent`: synthesizes all team outputs into `ProjectReport` (orchestrated,
  not choreographed — single agent, direct LLM call)
- CLI: `python -m hybrid.project_analysis <path>`
- Full pipeline: DISCOVERY → DEEP_DIVE → SYNTHESIS → COMPLETED with all real
  agents

**Acceptance criteria:**
1. Full pipeline runs end-to-end with all agents (LLM mocked in tests)
2. Security and Quality teams run concurrently in DEEP_DIVE phase
3. `ReportAgent` receives all four team reports and produces `ProjectReport`
4. `ProjectReport` contains all sections: structure, dependencies, security,
   quality, summary, recommendations
5. Phoenix trace matches the expected span tree structure (see Observability
   Contract above)
6. CLI entry point calls `init_observability("hybrid.project_analysis")`

---

### Iteration 5: `comparison-orchestrated-review`

**Goal:** Implement the orchestrated code review pipeline and the common
comparison models.

**Source files:**
- `src/comparison/__init__.py`
- `src/comparison/code_review/__init__.py`
- `src/comparison/code_review/models.py` — CodeReview, ReviewIssue
- `src/comparison/code_review/orchestrated.py` — 4-agent pipeline

**Delivers:**
- `ReviewIssue(severity, category, line, description, suggestion)` — Pydantic
- `CodeReview(file_path, issues, overall_score, summary, reviewer_count)` —
  Pydantic, shared by both patterns
- Orchestrated pipeline: `ReadAgent → AnalysisAgent → ReviewAgent →
  SummaryAgent` — sequential, validated between steps, snapshot at each step
- Reuses orchestration infrastructure: `SagaCoordinator`, `StepValidator`
  pattern, `SnapshotStore`

**Acceptance criteria:**
1. Pipeline produces a valid `CodeReview` from a Python file
2. Exactly 4 sequential steps (verified by snapshot count)
3. Each step validates output before passing to next
4. All spans traced: pipeline root → 4 step spans → 4 agent spans → 4 LLM spans
5. LLM calls mocked in tests

---

### Iteration 6: `comparison-choreographed-review`

**Goal:** Implement the same code review task via choreography — event-driven,
concurrent reviewers.

**Source files:**
- `src/comparison/code_review/choreographed.py` — event-driven review system
- `src/comparison/code_review/events.py` — ReviewRequested, ReviewFinding,
  ReviewComplete

**Delivers:**
- `ReviewRequested` event → triggers all reviewer agents
- `StyleReviewerAgent`, `LogicReviewerAgent`, `SecurityReviewerAgent` — each
  subscribes to `ReviewRequested`, publishes `ReviewFinding` events concurrently
- `ReviewAggregatorAgent` — collects findings, waits for all reviewers,
  produces `CodeReview` (same model as orchestrated)
- Reuses choreography infrastructure: `BaseChoreographyAgent`,
  `ResearchEventPublisher` pattern, `SourceExhausted` completion signaling

**Acceptance criteria:**
1. Produces a valid `CodeReview` (same Pydantic schema as orchestrated)
2. Reviewer agents run concurrently (their spans overlap in time)
3. More events than orchestrated steps (at least: 1 request + N findings + 1
   complete)
4. `ReviewAggregatorAgent` waits for all reviewers before synthesizing
5. LLM calls mocked in tests

---

### Iteration 7: `comparison-harness`

**Goal:** Build the comparison harness that runs both patterns on the same
input and measures differences. Add CLI entry points for everything.

**Source files:**
- `src/comparison/code_review/compare.py` — ComparisonResult, run_comparison
- `src/comparison/code_review/__main__.py` — CLI entry point

**Delivers:**
- `ComparisonResult`: orchestrated + choreographed results, durations, trace
  IDs, step/event counts, finding overlap score
- `run_comparison(file_path) -> ComparisonResult` — runs both patterns, collects
  metrics
- `finding_overlap` calculation: float 0–1, matching on category + line number
- CLI: `python -m comparison.code_review <file>` (both),
  `--mode orchestrated` or `--mode choreographed` (single)
- CLI calls `init_observability("comparison.code_review")`

**Acceptance criteria:**
1. `ComparisonResult` contains valid results from both patterns
2. Orchestrated and choreographed runs have different trace IDs
3. `finding_overlap` correctly computed (0–1 float)
4. Duration captured for both patterns
5. `orchestrated_steps == 4`, `choreographed_events >= 5`
6. CLI entry point works in all three modes

---

## Key Principles

1. **Observability is not an afterthought.** Every iteration includes span
   structure, attributes, and trace context propagation as acceptance criteria.
   If it's not in Phoenix, it didn't happen.

2. **Build on existing core.** No new infrastructure. `BaseAgent`, `MessageBus`,
   `EventStore`, `SnapshotStore`, `@traced`, `CircuitBreaker`, `DeadLetterQueue`
   — all exist and are tested. Use them.

3. **Unit tests mock everything external.** LLM calls, OTLP export, HTTP — all
   mocked. `tests/conftest.py` autouse fixtures must be preserved. No test
   makes a network call.

4. **Small iterations, strict dependency chain.** Each iteration builds on the
   previous. No iteration requires more than 3–5 source files. Pipeline can
   complete each in a single run.

5. **Distributed systems, not just AI.** The hybrid pattern exists to show that
   multi-agent coordination has the same challenges as microservice
   orchestration: phase gates, saga rollback, event-driven communication,
   partial failure, dead letter queues, circuit breakers.

6. **Same output, different patterns.** The comparison harness proves that
   orchestration and choreography are architectural choices with measurable
   trade-offs — not just theoretical distinctions.
