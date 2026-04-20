# Spec: Hybrid Foundation — Pipeline (happy path)

## Status
Superseded 2026-04-20 — split into
`hybrid-foundation-phase-validator-spec.md` (validator surface) and
`hybrid-foundation-orchestrator-spec.md` (state machine + events +
observability). Split reason: test-writer consistently over-constrained
public names across REQ-1/2/3/4 + AC-8 over 6 revision rounds (see
AGENTS.md lesson "Test-writer tends to over-constrain public shape").
Shrinking per-slice surface reduces the name-pinning pressure.

Original content retained below for history.

## Goal
Add the `ProjectOrchestrator` state machine on top of the `Team`
abstraction. Drive three phases (DISCOVERY → DEEP_DIVE → SYNTHESIS) with
validation gates and snapshots at every phase boundary. Happy path only:
no failure handling, no rollback. Stub agents from the prior spec are
reused.

## Scope
Extends `src/hybrid/project_analysis/`. Depends on `core` and on the
shipped `hybrid-foundation-team` spec. No changes to `src/core/`.

## Source Files
- `src/hybrid/project_analysis/models.py` (extended)
- `src/hybrid/project_analysis/events.py` (extended)
- `src/hybrid/project_analysis/orchestrator.py`
- `tests/test_hybrid_pipeline.py`

## Requirements

### REQ-1: Domain models (extended)
Add models for per-phase results, the orchestrator's analysis state, the
final project report, and a validation result.

### REQ-2: Event vocabulary (extended)
Add a topic and Pydantic event payload for phase-validated, including the
next phase.

### REQ-3: ProjectOrchestrator state machine
Phases PENDING → DISCOVERY → DEEP_DIVE → SYNTHESIS → COMPLETED. Only the
natural progression is allowed; any other transition raises a clear
error. The orchestrator:
1. Creates a workflow id and an initial analysis state.
2. Opens a root span for the run; each phase opens its own span parented
   by the root.
3. DISCOVERY and DEEP_DIVE each run their teams concurrently, then call
   the phase validator. On pass: snapshot, publish phase-validated,
   transition.
4. SYNTHESIS runs a single agent that receives the prior phases' team
   outputs, validates the resulting report, and on success transitions
   to COMPLETED and returns it.
5. Snapshots the analysis state at every phase boundary.

The orchestrator exposes its analysis state as a public attribute so
tests can observe progression without reaching into privates.

### REQ-4: PhaseValidator
A validator with an async `validate(phase, result)` method returning a
validation result. DISCOVERY and DEEP_DIVE require all expected team
outputs to be present and non-empty. SYNTHESIS requires a fully
populated project report. Expected team names per phase are injected at
construction.

## Acceptance Criteria

### AC-1: Valid progression succeeds
Happy-path run reaches COMPLETED and returns a complete project report.

### AC-2: Invalid transitions raise
Driving the orchestrator into an out-of-sequence phase raises.

### AC-3: Concurrent teams within a phase
Within DISCOVERY (and DEEP_DIVE), the phase's teams run concurrently.

### AC-4: Validator gates progression
A phase whose teams produced empty/missing outputs does not advance and
the validator's failure surfaces.

### AC-5: Snapshots at every phase boundary
On a happy-path run, one snapshot exists per phase transition.

### AC-6: Phase-validated emissions
Each validated phase produces exactly one phase-validated message
naming the next phase.

### AC-7: Synthesis input shape
The synthesis agent receives the prior teams' outputs as its input.

### AC-8: Observability
The trace has a root run span with one child span per phase; each
DISCOVERY/DEEP_DIVE team runs under its phase's span; SYNTHESIS's agent
span parents directly under the SYNTHESIS phase span.

### AC-9: Unit-test isolation
Tests do not make network calls; `tests/conftest.py` autouse fixtures
are preserved.

## Out of Scope
- Failure compensation, rollback, phase-failed events (next spec).
- Real LLM-backed agents.
