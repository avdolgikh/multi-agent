# Milestone 1 Wrap-Up Spec

## Context

Goal: wrap Milestone 1 completely so the repo stands as a clean portfolio piece demonstrating orchestration + choreography as distributed-systems patterns, with real agents, Ollama, and Phoenix observability. Hybrid-analysis is explicitly deferred to Milestone 2.

Current state (2026-04-14):
- ORCH-1 shipped + live-verified.
- CHOREO-1 spec drafted (`specs/choreography-research-llm-surface-spec.md`); pipeline ready to launch.
- CI red on `master`. Failures cluster in two unrelated groups (see below).
- `AGENTS.md` carries ~100 lines of obsolete Run 1–8 execution logs — token waste.
- `docs/diagrams.md` covers only core infra (LLM call, bus, resilience, tracing). It is missing pattern-level diagrams for orchestration and choreography, which are the headline of the repo.
- `README.md` is already good; only needs a roadmap-row tweak.

Intended outcome: CI green, docs accurate and compact, hybrid marked deferred, CHOREO-1 shipped with live trace verification. Milestone closable.

## Root causes — two CI failure clusters

**Cluster A (~4 tests, `tests/test_choreography_research_llm_surface.py`):** `_build_finding_payload` in Academic/Code/News agents does not surface `_summarize_entries` output. CHOREO-1 pipeline fixes this (scope already spec'd in `specs/choreography-research-llm-surface-spec.md`).

**Cluster B (~12 tests, `tests/test_vertical_validation.py`):** tests call `importlib.import_module("scripts.validate_vertical")`. `scripts/` has no `__init__.py`, and `pyproject.toml` has `pythonpath = ["src"]` — repo root is not on `sys.path`. Fix: add `scripts/__init__.py` + extend `pythonpath` to `["src", "."]`.

## Steps (in order)

### Step 1 — Bake this plan into the repo
This document. Durable across disconnects.

### Step 2 — Launch CHOREO-1 pipeline (Cluster A fix)
Run from `spec-driven-dev-pipeline` with the Codex env-var workaround (see `memory/project_choreo1_resume.md`):

```bash
cd /d/dev/avdolgikh_github_repos/spec-driven-dev-pipeline
CODEX_MODEL_TEST_WRITER=gpt-5.4-mini \
CODEX_MODEL_IMPLEMENTER=gpt-5.3-codex \
CODEX_MODEL_REVIEWER=gpt-5.4 \
uv run python scripts/run_pipeline.py choreography-research-llm-surface \
  --provider codex \
  --repo-root D:/dev/avdolgikh_github_repos/multi-agent \
  --config D:/dev/avdolgikh_github_repos/multi-agent/pipeline-config.toml
```

If `_enforce_test_freeze` trips, apply the manual-fix recipe from the resume memory. **No host edits to `multi-agent/` files while pipeline is active** (user rule).

### Step 3 — Fix Cluster B (scripts import on CI)
- Create empty `scripts/__init__.py`.
- In `pyproject.toml` change `pythonpath = ["src"]` → `pythonpath = ["src", "."]`.
- Verify locally: `uv run pytest tests/test_vertical_validation.py -q`.

### Step 4 — Mark hybrid-analysis as Milestone 2
- Insert a one-line banner at the top of `specs/hybrid-analysis-spec.md`: `> **Status: deferred to Milestone 2.** Spec retained for future execution; not in Milestone 1 scope.`
- Do not delete or move the file.
- Update `README.md` Build Roadmap row 5 to read `Deferred (Milestone 2)` instead of `Pending`.

### Step 5 — Compact AGENTS.md
Remove the entire `## Pipeline Execution Log (2026-04-11)` section and everything below through `### How to Resume` (the obsolete per-run logs). Keep:
- Project Overview, Architecture, Tech Stack, Conventions, Environment Variables, Dependency Pins, Current Constraints.
- `## Pipeline-Driven Development` header + Spec Execution Order table (update row 5 to `Deferred (Milestone 2)`; reflect that vertical-validation is shipped).
- `### Pipeline Run Commands` block.
- `### Development Roles` block.
- `### Lessons learned (2026-04-12)` block — load-bearing rules, not status.
- `### How to Resume` block.

Target: AGENTS.md ≤ ~130 lines.

### Step 6 — Augment docs/diagrams.md
Add two pattern-level diagrams at the top (before the core-infra ones):
- **Orchestration — sequential code analysis pipeline.** `Orchestrator → Parser → Scanner → Checker → Reporter`, saga/rollback path, one `Agent.execute → LLM` span per step. Ref: `src/orchestration/code_analysis/orchestrator.py`, `saga.py`.
- **Choreography — event-driven research aggregation.** Initiator publishes `ResearchRequested`; Academic/Code/News subscribe independently; each publishes `FindingDiscovered`; aggregator consumes; no direct agent-to-agent calls. Ref: `src/choreography/research/agents.py`, `event_log.py`.

Use both ASCII art + mermaid, matching existing style. Verify details against the actual source before writing.

### Step 7 — Final smoke run (local, manual)
After Steps 2–6 land and CI green:

```bash
uv run python scripts/run_phoenix.py &
uv run python -m orchestration.code_analysis fixtures/validation/sample_module.py
uv run python -m choreography.research "event sourcing vs CQRS tradeoffs"
```

Confirm in Phoenix: `orchestration-code-analysis` trace = 9 spans; `choreography-research` shows per-agent `.llm` child spans; findings contain real LLM prose. Fetch one trace with `scripts/phoenix_trace.py` for personal notes.

## Critical files

- **Create**: `specs/milestone-1-wrap-up-spec.md` (this), `scripts/__init__.py`.
- **Edit**: `pyproject.toml` (pythonpath), `specs/hybrid-analysis-spec.md` (banner), `README.md` (roadmap row), `AGENTS.md` (trim logs), `docs/diagrams.md` (add ORCH+CHOREO diagrams).
- **Untouched during Step 2**: everything under `multi-agent/`. Claude only edits after pipeline exits.

## Verification

- `uv run pytest --tb=short -q` → 0 failures (both clusters).
- `uv run ruff check src/ tests/` + `uv run ruff format --check src/ tests/` → clean.
- `uv run pyright src/` → clean.
- GitHub Actions CI on `master` → green after user commits.
- Smoke run (Step 7) produces traces in Phoenix UI.
- `AGENTS.md` line count ≤ ~130.
- `specs/hybrid-analysis-spec.md` has deferral banner at top; content otherwise unchanged.

## Non-goals

- No implementation of hybrid-analysis.
- No new features beyond what CHOREO-1 demands.
- No rewrites of README beyond the one roadmap row.
- No commits — user always commits.
