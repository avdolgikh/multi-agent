# Spec: Hybrid Foundation — Resilience (rollback + failure path)

## Status
Approved

## Goal
Add failure handling to the orchestrator: when a validator rejects a
phase result, publish a phase-failed event, run saga-style compensations
in LIFO order over completed phases, transition to FAILED, and raise.
Compensation failures during rollback are tolerated (logged, not
re-raised).

## Scope
Extends `src/hybrid/project_analysis/orchestrator.py`. Depends on the
shipped `hybrid-foundation-pipeline` spec.

## Source Files
- `src/hybrid/project_analysis/orchestrator.py` (extended)
- `src/hybrid/project_analysis/events.py` (extended)
- `tests/test_hybrid_resilience.py`

## Requirements

### REQ-1: Failure states & event
Add ROLLING_BACK and FAILED states to the orchestrator. Add a topic and
Pydantic event payload for phase-failed (including whether rollback will
run).

### REQ-2: Compensation registration
The orchestrator records a per-phase compensation callable as each phase
completes. Stub-era compensations are logging no-ops, but the
registration mechanism is real and exercised.

### REQ-3: Rollback semantics
On any phase failure (validator rejection or unhandled error during the
phase): publish phase-failed, transition to ROLLING_BACK, run registered
compensations in **LIFO order** (most recent completed phase first),
transition to FAILED, raise. If an individual compensation raises, log
it and continue with the next compensation; do not re-raise from
rollback.

## Acceptance Criteria

### AC-1: Phase failure triggers rollback
A phase whose validator fails causes the orchestrator to transition
through ROLLING_BACK to FAILED and re-raise.

### AC-2: Phase-failed emission
Exactly one phase-failed message is published per failed phase.

### AC-3: LIFO compensation order
When phase N fails after phases 1..N-1 completed, compensations run in
order N-1, N-2, ..., 1 (observable by capturing which compensations
ran and in which sequence).

### AC-4: Compensation failures tolerated
If one compensation raises during rollback, subsequent compensations
still run, and rollback overall completes (the original phase failure
is what propagates, not the compensation error).

### AC-5: Earlier results preserved
After rollback, the analysis state at failure time still contains
results from phases that completed before the failure.

### AC-6: Happy path unchanged
A successful run still reaches COMPLETED with no phase-failed events
emitted (regression check).

### AC-7: Unit-test isolation
Tests do not make network calls; `tests/conftest.py` autouse fixtures
are preserved.

## Out of Scope
- Real LLM-backed agents.
- Persistence of rolled-back state beyond in-memory event store.
