# Spec: Hybrid Foundation — Models, Team, Project Orchestrator

## Status

Approved

## Goal

Build the skeleton for the hybrid project-analysis pattern: Pydantic models
for the domain, an event bus vocabulary for phase/team lifecycle events,
a `Team` abstraction that runs member agents concurrently and aggregates
their output, and a `ProjectOrchestrator` state machine that drives three
phases (DISCOVERY → DEEP_DIVE → SYNTHESIS) with validation gates,
snapshots at phase boundaries, and saga-style rollback on failure.

Agents in this iteration are **stubs** — they return canned results so the
two-level coordination (orchestrator across phases, teams within phases)
can be validated without any real LLM calls. Real agents arrive in
Iterations 2–4 of Milestone 2 (see `specs/hybrid-analysis-spec.md`).

## Scope

New use-case code lives under `src/hybrid/project_analysis/`. Depends on
existing `src/core/` primitives (agents, messaging, tracing, state,
resilience). No changes to `src/core/`.

## Source Files

- `src/hybrid/__init__.py`
- `src/hybrid/project_analysis/__init__.py`
- `src/hybrid/project_analysis/models.py`
- `src/hybrid/project_analysis/events.py`
- `src/hybrid/project_analysis/team.py`
- `src/hybrid/project_analysis/orchestrator.py`
- `src/hybrid/project_analysis/stubs.py`
- `tests/test_hybrid_foundation.py`

## Requirements

### REQ-1: Domain models

Pydantic models representing the domain objects the three phases produce:
a structure report, a dependency report, a security report, a quality
report, a final project report, per-phase results, the orchestrator's
analysis state, and a validation result. Field shapes follow naturally
from their names. Models carry data only — no business logic.

### REQ-2: Event bus vocabulary

Topic constants and Pydantic event payload models for the three
lifecycle events the orchestrator and teams emit: a team has completed,
a phase has validated (including the next phase), a phase has failed
(including whether rollback will run). A helper that builds a
per-team topic string from a team name and event name.

### REQ-3: Team

A `Team` runs its member agents concurrently, aggregates their outputs
into a single dict via a caller-supplied aggregator, appends one event
per agent to the event store on a team-scoped stream, and publishes a
team-completion message to the team-complete topic. Trace context from
the caller propagates to each child agent task. Failures of individual
agents are surfaced on the published team-completion message (not
swallowed, not raised).

### REQ-4: ProjectOrchestrator

A state machine with phases PENDING → DISCOVERY → DEEP_DIVE → SYNTHESIS
→ COMPLETED, plus ROLLING_BACK and FAILED as recovery/terminal states.
Only the natural progression transitions are allowed; any other
transition must raise a clear error. The orchestrator:

1. Creates a workflow id and an initial analysis state.
2. Opens a root span for the whole run; each phase opens its own span
   parented by the root.
3. DISCOVERY and DEEP_DIVE each run their teams concurrently, then
   invoke the phase validator. On validation pass: snapshot the state,
   publish phase-validated, transition to next phase. On fail: publish
   phase-failed, rollback, transition to FAILED, raise.
4. SYNTHESIS runs a single synthesis agent that receives the prior
   phases' team outputs as its input, validates the resulting report,
   and on success transitions to COMPLETED and returns the project
   report.
5. Snapshots the state at every phase boundary.
6. On rollback, walks completed phases LIFO and runs per-phase
   compensations (stub-era compensations are logging no-ops). Rollback
   never raises for individual compensation failures.

The orchestrator exposes its analysis state as a public attribute so
tests can observe progression without reaching into privates.

### REQ-5: PhaseValidator

A validator with an async `validate(phase, result)` method returning a
validation result. DISCOVERY and DEEP_DIVE require all expected team
outputs to be present and non-empty. SYNTHESIS requires a fully
populated project report. Expected team names per phase are injected at
construction time.

### REQ-6: Stub agents

A `StubAgent` subclass of the existing base agent that returns a
constructor-supplied canned output (optionally forced to failure for
testing rollback paths), does not call an LLM, and opens its own
`.execute` span under the caller's span context. A `make_stub_team`
helper builds a `Team` from a list of `(agent_id, canned_output)` pairs
with a merging aggregator.

## Acceptance Criteria

### AC-1: Phase transitions

Valid phase progressions succeed; disallowed transitions raise.

### AC-2: Happy path

`ProjectOrchestrator.run(path)` returns a complete project report; the
orchestrator's final phase is COMPLETED.

### AC-3: Concurrent team execution

Within DISCOVERY, the two teams run concurrently (not sequentially).

### AC-4: Event bus emissions

Each team produces one team-completion message; each validated phase
produces one phase-validated message; a failed phase produces one
phase-failed message.

### AC-5: Validator gates progression

An empty or invalid phase result blocks the next phase and triggers
rollback.

### AC-6: Snapshots

The state is snapshotted at every phase boundary on the happy path.

### AC-7: Rollback preserves prior results

Failure in a later phase leaves earlier phases' results reachable (via
the pre-failure snapshot or the analysis state at failure time).

### AC-8: Observability

The trace has a root span for the run with one child span per phase;
each team runs under its phase's span; stub-agent `.execute` spans
parent appropriately (under their team inside DISCOVERY/DEEP_DIVE;
directly under SYNTHESIS's phase span when there is no enclosing team).

### AC-9: Synthesis validator

Incomplete synthesis output causes `SYNTHESIS` to fail via the same
validator path as the other phases.

### AC-10: Synthesis input

The synthesis agent receives the four prior team outputs as its input.

### AC-11: Trace context & failure surfacing in teams

Child agent tasks inherit the team's trace context. Individual agent
failures are reported on the team-completion message, not swallowed
and not raised.

### AC-12: Async validator

The validator's `validate` is an async method.

### AC-13: Unit-test isolation

Tests do not make network calls; `tests/conftest.py` autouse fixtures
are preserved.

### AC-14: Public API re-exports

Public symbols are importable from the package root.

## Package Layout

```
src/hybrid/
  __init__.py
  project_analysis/
    __init__.py          # public re-exports
    models.py            # REQ-1
    events.py            # REQ-2
    team.py              # REQ-3
    orchestrator.py      # REQ-4, REQ-5
    stubs.py             # REQ-6
tests/
  test_hybrid_foundation.py
```

Existing files modified: none.

## Out of Scope

- Real LLM-backed agents (Iterations 2–4).
- Persistence beyond in-memory event store + snapshot store.
- Real compensating actions beyond logging.
