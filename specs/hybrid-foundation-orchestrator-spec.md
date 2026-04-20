# Spec: Hybrid Foundation — Orchestrator

## Status
Approved

## Goal
Drive the three-phase project-analysis state machine on top of the
shipped `Team` and phase-validator abstractions. Run the phases in
order, enforce transition rules, publish a phase-validated message at
each boundary, snapshot the state at each boundary, and expose the
workflow's live state. Happy path only — no failure handling, no
rollback, no compensation.

## Scope
Extends `src/hybrid/project_analysis/`. Depends on `core`, on the
shipped `hybrid-foundation-team` spec (Team + events helpers), and on
the shipped `hybrid-foundation-phase-validator` spec (validator +
project-report model). No changes to `src/core/`.

## Source Files
- `src/hybrid/project_analysis/models.py` (extended)
- `src/hybrid/project_analysis/events.py` (extended)
- `src/hybrid/project_analysis/orchestrator.py`
- `tests/test_hybrid_orchestrator.py`

## Requirements

### REQ-1: Domain models (extended)
Add a model for the orchestrator's live analysis state — it carries a
workflow identifier and the current phase. Model carries data only.

### REQ-2: Event vocabulary (extended)
Add a topic for the phase-validated notification and a Pydantic payload
for it. The payload identifies the phase that just validated and the
phase the pipeline will enter next.

### REQ-3: ProjectOrchestrator state machine
An orchestrator that drives the flow from a pre-run resting state
through the discovery, deep-dive, and synthesis phases and into a
terminal completed state. The natural forward progression is the only
allowed progression; any attempt at an out-of-sequence transition
raises a clear error. The orchestrator:
1. Creates a workflow identifier and an initial analysis state before
   the run begins.
2. Opens a root span for the run and one child span per phase, each
   parented on the root span.
3. For discovery and for deep-dive: runs the phase's teams
   concurrently, invokes the injected validator, and on success
   snapshots the state, publishes phase-validated, then transitions.
4. For synthesis: runs a single agent that consumes the prior phases'
   team outputs, validates the resulting project report, snapshots,
   and transitions to the terminal state, returning the report.
5. Exposes the live analysis state publicly enough that callers and
   tests observe progression without reaching into privates.

### REQ-4: Dependency injection
The orchestrator receives its validator, its per-phase teams, and its
synthesis agent from the caller. It does not construct them itself. A
failing validator outcome surfaces from the orchestrator without being
swallowed.

## Acceptance Criteria

### AC-1: Valid progression succeeds
A happy-path run reaches the terminal state and returns a complete
project report.

### AC-2: Invalid transitions raise
Driving the orchestrator into an out-of-sequence phase raises an error
carrying a reason.

### AC-3: Concurrent teams within a phase
Within the discovery phase and within the deep-dive phase, the phase's
teams run concurrently (their work overlaps).

### AC-4: Validator gates progression
When the injected validator returns a failure outcome for a phase, the
orchestrator does not advance past that phase and the validator's
failure surfaces to the caller.

### AC-5: Snapshots at every phase boundary
A happy-path run produces exactly one snapshot per phase transition.

### AC-6: Phase-validated emissions
Each validated phase produces exactly one phase-validated publication.
The publication identifies the next phase.

### AC-7: Synthesis input shape
The synthesis agent receives the prior phases' team outputs as its
input.

### AC-8: Observability topology
The trace has a single root span for the run with exactly three
children — one per phase. Each discovery team and each deep-dive team
runs under its respective phase's span. The synthesis agent's span is
directly parented by the synthesis phase span.

### AC-9: Public state visibility
The live analysis state (workflow identifier and current phase) is
observable from outside the orchestrator across the full run — before
the run begins, during each phase, and after completion.

### AC-10: Unit-test isolation
Tests do not make network calls; `tests/conftest.py` autouse fixtures
are preserved. The validator, teams, and synthesis agent are provided
by the test as stubs (no real LLM calls).

## Out of Scope
- Failure compensation, rollback, phase-failed events (next spec).
- Real LLM-backed agents.
