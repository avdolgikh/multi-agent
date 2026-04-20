# Spec: Hybrid Foundation — Team

## Status
Approved

## Goal
Build the smallest reusable collaboration unit for the hybrid pattern: a
`Team` that runs member agents concurrently, aggregates their output via a
caller-supplied function, appends per-agent events to the event store on a
team-scoped stream, and publishes a single team-completion message when
done. No orchestrator, no phases — just one team.

## Scope
New use-case code under `src/hybrid/project_analysis/`. Depends on
existing `src/core/` primitives. No changes to `src/core/`.

## Source Files
- `src/hybrid/__init__.py`
- `src/hybrid/project_analysis/__init__.py`
- `src/hybrid/project_analysis/models.py`
- `src/hybrid/project_analysis/events.py`
- `src/hybrid/project_analysis/team.py`
- `src/hybrid/project_analysis/stubs.py`
- `tests/test_hybrid_team.py`

## Requirements

### REQ-1: Domain models (minimal)
Pydantic models for the data this spec actually exercises: a per-agent
output and a team result. Field shapes follow naturally from their names.
Models carry data only.

### REQ-2: Event vocabulary (minimal)
A topic constant for team completion, a Pydantic event payload for it,
and a helper that builds a per-team topic string from a team name and
event name.

### REQ-3: Team
A `Team` runs its member agents concurrently, aggregates their outputs
into a single dict via a caller-supplied aggregator, appends one event
per agent to the event store on a team-scoped stream, and publishes a
team-completion message to the team-complete topic. Trace context from
the caller propagates to each child agent task. Failures of individual
agents are surfaced on the published team-completion message (not
swallowed, not raised).

### REQ-4: Stub agents
A `StubAgent` returns a constructor-supplied canned output (optionally
forced to failure), does not call an LLM, and opens its own `.execute`
span under the caller's span context. A `make_stub_team` helper builds a
working `Team` from a list of `(agent_id, canned_output)` pairs with a
default merging aggregator.

## Acceptance Criteria

### AC-1: Concurrent execution
A team of N stub agents executes them concurrently (their work overlaps).

### AC-2: Aggregation
The caller-supplied aggregator receives all agent outputs and produces
the team's result. Stubs that fail are surfaced; they do not abort the
aggregation.

### AC-3: Event-store append
The event store receives one event per agent on a team-scoped stream.

### AC-4: Team-complete publication
Exactly one team-completion message is published to the team-complete
topic. It carries the team's result and any failure indicators.

### AC-5: Trace context propagation
Each agent's `.execute` span is parented under the team's span context.

### AC-6: Helper produces a working team
`make_stub_team(...)` returns a `Team` that can be run and produces an
aggregated result; the helper is exercised end-to-end, not just imported.

### AC-7: Public API re-exports
`Team`, `StubAgent`, `make_stub_team`, the event topic, and the event
payload are importable from the package root.

### AC-8: Unit-test isolation
Tests do not make network calls; `tests/conftest.py` autouse fixtures
are preserved.

## Package Layout
(as per Source Files)

## Out of Scope
- ProjectOrchestrator, phases, validators (next spec).
- Failure compensation (third spec).
- Real LLM-backed agents (later iterations).
