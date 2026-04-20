# Spec: Hybrid Foundation — Phase Validator

## Status
Approved

## Goal
Build the phase validation gate that the hybrid pipeline will use to
decide whether a phase may advance. This spec covers the validator in
isolation: given a phase identifier and the phase's produced result,
return an outcome that reports pass/fail and (on failure) a
human-readable reason. No orchestrator, no events, no spans, no state
machine.

## Scope
New use-case code under `src/hybrid/project_analysis/`. Depends on
existing `src/core/` primitives and on the shipped
`hybrid-foundation-team` spec (team output shapes). No changes to
`src/core/`.

## Source Files
- `src/hybrid/project_analysis/models.py` (extended)
- `src/hybrid/project_analysis/validator.py`
- `tests/test_hybrid_phase_validator.py`

## Requirements

### REQ-1: Domain models (extended)
Add a validation-outcome model that carries pass/fail plus a
human-readable reason. Add a project-report model that aggregates the
prior phases' team outputs into a final synthesis deliverable; required
sections follow naturally from the three phase names.

### REQ-2: Phase validator
A component with an async method that, given a phase identifier and the
phase's produced result, returns a validation outcome. Expected
per-phase team names are supplied at construction. Validation rules:
- For the discovery and deep-dive phases: every expected team must be
  represented in the result with a non-empty output.
- For the synthesis phase: the produced project report must be fully
  populated.
- The component is standalone — no dependency on an orchestrator,
  event bus, tracer, or any other runtime service.

## Acceptance Criteria

### AC-1: Discovery / deep-dive happy path
A result carrying non-empty outputs for every expected team validates
successfully for the given phase.

### AC-2: Missing team fails validation
A result that omits an expected team's output fails validation. The
outcome's reason identifies what is missing.

### AC-3: Empty team output fails validation
A result where an expected team produced an empty output fails
validation. The outcome's reason identifies what is empty.

### AC-4: Synthesis happy path
A fully populated project report validates successfully for the
synthesis phase.

### AC-5: Synthesis failure
A project report missing required content fails validation for the
synthesis phase. The outcome's reason identifies what is missing.

### AC-6: Reusable across calls
The same validator instance returns correct outcomes for multiple
independent phase results without state leaking between calls.

### AC-7: Public API re-exports
The validator and the new models are importable from the package root.

### AC-8: Unit-test isolation
Tests do not make network calls; `tests/conftest.py` autouse fixtures
are preserved.

## Out of Scope
- ProjectOrchestrator, state machine, snapshots (next spec).
- Phase-validated events, event-bus integration (next spec).
- Observability / span topology (next spec).
- Failure compensation / rollback (later spec).
