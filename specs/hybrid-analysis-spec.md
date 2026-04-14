# Spec: Hybrid — Project Analysis with Pattern Comparison

> **Status: deferred to Milestone 2.** Spec retained for future execution; not in Milestone 1 scope.

## Goal

Build a hybrid multi-agent system that combines orchestration between teams with choreography within teams, plus implement the **same code-review task in both pure orchestration and pure choreography** for direct side-by-side comparison.

This spec delivers two things:
1. A **hybrid use case** (project analysis) demonstrating the combined pattern
2. A **comparison harness** that runs an identical task through both patterns and presents the differences

Lives under `src/hybrid/project_analysis/` and `src/comparison/code_review/`. Depends on `src/core/`, `src/orchestration/`, and `src/choreography/`.

## Source Files

The implementation creates these key modules:

- `src/hybrid/project_analysis/orchestrator.py` — ProjectOrchestrator, Team
- `src/hybrid/project_analysis/agents.py` — FileTreeAgent, ModuleAgent, DependencyGraphAgent, PackageAgent, VulnerabilityAgent
- `src/hybrid/project_analysis/models.py` — ProjectStructure, DependencyReport, SecurityReport, QualityReport, ProjectReport
- `src/comparison/code_review/orchestrated.py` — orchestrated code review pipeline
- `src/comparison/code_review/choreographed.py` — choreographed code review system
- `src/comparison/code_review/compare.py` — ComparisonResult, run_comparison
- `src/comparison/code_review/models.py` — CodeReview, ReviewIssue

## Requirements

### Part A: Hybrid Project Analysis

#### 1. Architecture

Two-level coordination:

```
┌──────────────────────────────────────────────────────┐
│                  PROJECT ORCHESTRATOR                  │
│  (assigns domains to teams, gates between phases)     │
│                                                       │
│   Phase 1: Discovery    Phase 2: Deep Dive   Phase 3 │
│   ┌──────────────┐     ┌──────────────┐     ┌──────┐│
│   │ Team:        │     │ Team:        │     │Report││
│   │ Structure    │────▶│ Security     │────▶│Agent ││
│   │ (choreo)     │     │ (choreo)     │     │      ││
│   ├──────────────┤     ├──────────────┤     └──────┘│
│   │ Team:        │     │ Team:        │              │
│   │ Dependencies │     │ Quality      │              │
│   │ (choreo)     │     │ (choreo)     │              │
│   └──────────────┘     └──────────────┘              │
└──────────────────────────────────────────────────────┘
```

- **Between phases**: Orchestration. The orchestrator decides when Phase 1 is complete and Phase 2 can begin. Validation gates between phases.
- **Within teams**: Choreography. Agents within a team coordinate via events, explore independently, cross-reference findings.

#### 2. Project Orchestrator (`src/hybrid/project_analysis/orchestrator.py`)

##### 2.1 `ProjectOrchestrator`
- State machine with phases: `DISCOVERY`, `DEEP_DIVE`, `SYNTHESIS`, `COMPLETED`, `FAILED`
- `async run(project_path: str) -> ProjectReport`
- For each phase:
  - Spawns teams as choreography groups (each team uses the event bus internally)
  - Waits for all teams in the phase to complete (listens for team completion events)
  - Runs inter-phase validation
  - Saves snapshot before transitioning to next phase
- Saga compensation across phases (if Phase 2 fails, Phase 1 results are preserved but marked incomplete)

##### 2.2 `Team` abstraction
- `Team(name: str, agents: list[BaseAgent], bus: MessageBus)`
- Each team gets its own topic namespace on the bus: `team:{team_name}:*`
- Teams publish a `TeamComplete` event when all their agents have reported `SourceExhausted`
- The orchestrator subscribes to `TeamComplete` events to know when to advance

#### 3. Discovery Phase Teams

##### 3.1 Structure Team
- `FileTreeAgent`: maps the project file structure
- `ModuleAgent`: identifies modules and their boundaries
- `DependencyGraphAgent`: maps import relationships
- These agents communicate via events within the team, cross-referencing findings (reusing the choreography pattern from the research use case)
- Output: `ProjectStructure` model

##### 3.2 Dependencies Team
- `PackageAgent`: analyzes external dependencies (versions, licenses)
- `VulnerabilityAgent`: checks known vulnerabilities in dependencies
- Output: `DependencyReport` model

#### 4. Deep Dive Phase Teams

##### 4.1 Security Team
- Reuses `SecurityAgent` pattern from orchestration spec
- Multiple agents examine different security dimensions in parallel via events
- Output: `SecurityReport` model

##### 4.2 Quality Team
- Reuses `QualityAgent` pattern from orchestration spec
- Multiple agents assess different quality dimensions in parallel via events
- Output: `QualityReport` model

#### 5. Synthesis Phase
- Single `ReportAgent` (orchestrated, not choreographed) synthesizes all team outputs into a final `ProjectReport`
- `ProjectReport`: `project_path: str`, `structure: ProjectStructure`, `dependencies: DependencyReport`, `security: SecurityReport`, `quality: QualityReport`, `summary: str`, `recommendations: list[str]`

### Part B: Pattern Comparison — Code Review

#### 6. Same Task, Two Patterns (`src/comparison/code_review/`)

Implement a **code review** task in both patterns, with a harness to run them side-by-side and compare results.

The task: Given a Python file, produce a code review with: issues found, severity, suggestions, overall assessment.

##### 6.1 Orchestrated Code Review (`src/comparison/code_review/orchestrated.py`)
- Pipeline: `ReadAgent → AnalysisAgent → ReviewAgent → SummaryAgent`
- Sequential, validated between steps, snapshot at each step
- Reuses orchestration infrastructure from the code analysis spec

##### 6.2 Choreographed Code Review (`src/comparison/code_review/choreographed.py`)
- Event-driven: `ReviewRequested` → multiple reviewer agents react independently → `AggregatorAgent` synthesizes
- Agents: `StyleReviewerAgent`, `LogicReviewerAgent`, `SecurityReviewerAgent` — each subscribes to `ReviewRequested`, publishes `ReviewFinding` events
- `AggregatorAgent` collects findings, waits for all reviewers, produces final review
- Reuses choreography infrastructure from the research spec

##### 6.3 `CodeReview` common model
- Both patterns produce the same output type: `CodeReview(file_path: str, issues: list[ReviewIssue], overall_score: int, summary: str, reviewer_count: int)`
- `ReviewIssue`: `severity: str`, `category: str`, `line: int | None`, `description: str`, `suggestion: str`

##### 6.4 Comparison Harness (`src/comparison/code_review/compare.py`)
- `async run_comparison(file_path: str) -> ComparisonResult`
- Runs both patterns on the same file
- `ComparisonResult` captures:
  - `orchestrated_result: CodeReview`
  - `choreographed_result: CodeReview`
  - `orchestrated_duration_ms: float`
  - `choreographed_duration_ms: float`
  - `orchestrated_trace_id: str`
  - `choreographed_trace_id: str`
  - `orchestrated_steps: int` (number of sequential steps)
  - `choreographed_events: int` (number of events published)
  - `finding_overlap: float` (0-1, how many issues both found)

#### 7. Entry Points

- `python -m hybrid.project_analysis <path>` — runs the hybrid project analysis
- `python -m comparison.code_review <file>` — runs the side-by-side comparison
- `python -m comparison.code_review --mode orchestrated <file>` — runs only orchestrated
- `python -m comparison.code_review --mode choreographed <file>` — runs only choreographed

## Acceptance Criteria

### Hybrid

1. **Two-level coordination**: The orchestrator controls phase transitions (DISCOVERY → DEEP_DIVE → SYNTHESIS). Within each phase, agents communicate only via events on the message bus — the orchestrator does not direct individual agents.

2. **Teams complete independently**: In the DISCOVERY phase, Structure Team and Dependencies Team run concurrently. The orchestrator advances to DEEP_DIVE only after both teams publish `TeamComplete`.

3. **Inter-phase validation**: The orchestrator validates Phase 1 output before starting Phase 2. If Structure Team produced an empty result, Phase 2 does not start.

4. **Snapshots at phase boundaries**: `SnapshotStore.history(workflow_id)` has one snapshot per phase transition.

5. **Intra-team event flow**: Within a team, agents publish and react to events. The `FileTreeAgent` and `ModuleAgent` in the Structure Team produce at least 2 events each that can be seen in the event store.

### Comparison

6. **Same input, same output type**: Both patterns receive the same Python file and produce a `CodeReview` instance. Both instances validate against the same Pydantic schema.

7. **Timing captured**: `ComparisonResult` includes duration for both patterns. The choreographed version processes reviewer agents concurrently (its individual agent spans overlap in time).

8. **Event count vs step count**: The orchestrated version has exactly 4 sequential steps. The choreographed version has more events than steps (at least: 1 request + N findings + 1 complete).

9. **Finding overlap computed**: `finding_overlap` is a float between 0 and 1 representing how many issues were found by both patterns (by matching on category and line number).

10. **Traces are independent**: The orchestrated and choreographed runs have different trace IDs but can be correlated through the `ComparisonResult`.
