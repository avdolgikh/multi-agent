# AGENTS.md — Context for AI Agents

## Project Overview

This repository demonstrates **orchestration vs choreography** in multi-agent AI systems.
It is a working system with real LLM-backed agents, not a framework or mock demo.

## Architecture

```
src/
├── core/                    # Shared infrastructure (agents, messaging, tracing, state, resilience)
│   ├── agents/              # Base agent abstractions
│   ├── messaging/           # Event bus (Redis Streams pub/sub, request/reply)
│   ├── tracing/             # OpenTelemetry distributed tracing
│   ├── state/               # Event store + immutable snapshots
│   └── resilience/          # Circuit breakers, retries, dead letter queue
│
├── orchestration/           # Orchestration pattern use cases
│   └── code_analysis/       # Sequential pipeline: parse → scan → check → report
│
├── choreography/            # Choreography pattern use cases
│   └── research/            # Event-driven multi-source research aggregation
│
├── hybrid/                  # Hybrid pattern use cases
│   └── project_analysis/    # Orchestrated teams + choreographed intra-team work
│
└── comparison/              # Same task, both patterns side-by-side
    └── code_review/         # Code review in orchestrated vs choreographed mode
```

## Tech Stack

- Python 3.11+, uv for package management
- LLM APIs: Ollama (local, primary) + OpenAI (cloud, fallback). Both use OpenAI SDK.
- Messaging: In-memory asyncio-based event bus (no external dependencies)
- Tracing: OpenTelemetry
- State: In-memory event sourcing + immutable snapshots (no external DB)
- No heavy frameworks — patterns built from asyncio + in-memory bus + OpenAI SDK

## Conventions

- All infrastructure lives in `src/core/` and is imported by use cases
- Each module has `__init__.py` re-exporting its public API
- Use `asyncio` throughout — agents are async
- Use `pydantic` for message schemas and configuration
- **UNIT TESTS ONLY — mock ALL external calls.** Tests verify logic/functionality, never real integrations. This includes LLM APIs (OpenAI/Ollama), HTTP (OTLP/Phoenix/Traceloop span export), Redis, databases, and any subprocess to external services. `tests/conftest.py` provides autouse fixtures that stub Traceloop + OTLP HTTP exporters; any new test must preserve these stubs or add its own. No test may make a network call.
- No external services needed (no Redis, no cloud APIs in tests)
- Each use case is runnable via `uv run python -m <module>`

## Environment Variables

- `OPENAI_API_KEY` — OpenAI API key (fallback only — prefer Ollama)
- `OLLAMA_BASE_URL` — Ollama API URL (default: `http://localhost:11434/v1`)
- `OTEL_EXPORTER_OTLP_ENDPOINT` — OpenTelemetry collector endpoint (optional)

## Current Constraints

- **Local-first**: Prefer local Ollama models over cloud APIs. The user has a local GPU.
  OpenAI API key exists as fallback but avoid spending money — use cheap models if needed.
- **No external services in tests**: Tests use `InMemoryBus` and `InMemoryEventStore`.
  No Redis, no real API calls. All test LLM interactions must be mocked.
- **Ollama as primary LLM provider**: Agents should support Ollama via its OpenAI-compatible
  API (http://localhost:11434/v1). This is the preferred runtime provider.
  Available local models (RTX 4070 12GB):
  - `qwen3-coder:latest` — 30.5B MoE, Q4_K_M (best for code tasks)
  - `gemma4:e4b` — 8B, Q4_K_M
  - `glm-4.7-flash:latest` — 29.9B MoE, Q4_K_M
  - `qwen3.5:latest` — 9.7B, Q4_K_M
- **OpenAI as fallback**: Available but costly. Use only when Ollama models can't handle the task.
  When using OpenAI, pick the cheapest suitable model (gpt-4o-mini).

---

## Pipeline-Driven Development

This project is built spec-by-spec using the **spec-driven-dev-pipeline** (TDD pipeline).
Pipeline tool: `D:\dev\avdolgikh_github_repos\spec-driven-dev-pipeline`

### Spec Execution Order (dependency chain)

| # | Task ID | Spec | Status |
|---|---------|------|--------|
| 1 | `core-infrastructure` | Shared infrastructure (agents, messaging, tracing, state, resilience) | DONE |
| 2 | `orchestration-code-analysis` | Orchestrated code analysis pipeline | DONE (Codex, 2026-04-12) |
| 3 | `choreography-research` | Event-driven multi-source research | PENDING |
| 4 | `hybrid-analysis` | Hybrid pattern + comparison harness | PENDING |
| — | `observability-phase1` | Phoenix + OpenLLMetry wiring (independent) | IN PROGRESS (Codex, 2026-04-12; on branch `observability-phase1` in worktree `../multi-agent-obs`) |

### Pipeline Run Commands
```bash
cd D:/dev/avdolgikh_github_repos/spec-driven-dev-pipeline

# Primary: Codex provider
uv run python scripts/run_pipeline.py <task-id> --provider codex --repo-root D:/dev/avdolgikh_github_repos/multi-agent

# Secondary: Gemini provider
uv run python scripts/run_pipeline.py <task-id> --provider gemini --repo-root D:/dev/avdolgikh_github_repos/multi-agent
```

### Development Roles
- **Claude**: Orchestrate only — write specs, run pipelines, monitor, fix issues, document.
  Do NOT implement code directly. Save tokens for vital orchestration work.
- **Codex / Gemini**: Generate tests and implementation code via the pipeline.
- **Ollama local models**: Runtime LLM provider for the multi-agent system's agents.

### Known Issues During Pipeline Runs

**Issue 1 (2026-04-11): Stage 1 EXIT_STAGE_NO_EFFECT (exit 10)**
- Pipeline couldn't match test files to task ID `core-infrastructure`
- Test-writer created `test_agents.py`, `test_messaging.py`, etc. — none contain "core-infrastructure"
- **Fix**: Added `## Source Files` section to spec listing `.py` files in backticks.
  Pipeline extracts `test_<stem>` terms from these, so `agents.py` → `test_agents` matches.
- Applied to all 4 specs proactively.

**Issue 2 (2026-04-11): test_agents.py SyntaxError** (run 1 only, resolved)
- Codex generated `'what's up'` (unescaped single quote in single-quoted string) on line 28
- This is a codegen quality issue, not a pipeline bug
- Resolved on run 2: Codex produced a single `test_core_infrastructure.py` instead

---

## Pipeline Execution Log (2026-04-11)

### Run 1: core-infrastructure (FAILED — exit 10)
- **Provider**: codex
- **Failure**: EXIT_STAGE_NO_EFFECT — test files `test_agents.py` etc. didn't match task ID
- **Root cause**: Pipeline's `_is_task_test_file` couldn't match. Spec lacked `## Source Files` section.
- **Fix**: Added `## Source Files` with `.py` filenames in backticks to all 4 specs.
- **Cleanup**: Deleted `.pipeline-state/`, test files, `.venv`, started fresh.

### Run 2: core-infrastructure (COMPLETE)
- **Provider**: codex
- **Stage 1 (Test Generation)**: PASSED — `tests/test_core_infrastructure.py`
- **Stage 2 (Test Review)**: 5 review/revise iterations total
  - iter 0-3: Codex revised (mock shape, tool coverage, traced decorator, circuit breaker, trace propagation)
  - iter 4: Reviewer flagged over-constrained trace propagation test. Hit revision cap (exit 2).
  - **Manual fix**: Replaced message-payload trace assertion with span-based check. Reset iteration.
  - iter 0 (post-fix): APPROVED. Tests frozen.
- **Stage 3 (Implementation)**: PASSED — 1,034 lines across 6 modules
  - **Manual fix**: SyntaxError in test file (escaped quotes `f\"...\"` from codegen). Fixed + updated frozen hash.
- **Stage 4 (Validation)**: PASSED — 19/19 tests green
- **Stage 5 (Code Review)**: 3 review/revise iterations
  - iter 0: Revise (traced decorator, DLQ API)
  - iter 1: Revise (InMemoryBus must be queue-backed per spec)
  - iter 2: APPROVED
- **Verification**: PASSED — 19/19 tests, exit 0

### Pending Specs
- `choreography-research` — waiting for user to proceed
- `hybrid-analysis` — waiting for choreography to complete
- `observability-phase1` — in progress on branch `observability-phase1` (Codex)

### Run 3: orchestration-code-analysis (FAILED — exit 7, false positive from concurrent host edits)
- **Provider**: codex (bg `b4819x9eq`)
- Stage 2 reviewer finished iter 2 review with valid revise JSON; pipeline exited `EXIT_REVIEWER_MODIFIED_FILES`.
- **Root cause**: concurrent edits to `AGENTS.md` / `specs/` from the host workspace. Pipeline-side fix landed on 2026-04-12 (per-file diff + clear error) — see spec-driven-dev-pipeline AGENTS.md.

### Run 4: orchestration-code-analysis (FAILED — exit 7, same false positive)
- **Provider**: codex (bg `b6usn101j`, resume)
- User edited `specs/observability-phase1-spec.md` (APPROVED marker) mid-run → guard tripped again.
- Motivated the pipeline fix described above.

### Run 5: orchestration-code-analysis (FAILED — exit 2, revision cap at iter 4)
- **Provider**: codex (bg `bqr2iwz39`, post-pipeline-fix)
- Stage 2 went 4 iterations of revise/revise-revision; reviewer kept surfacing new items.
- Iter 4 reviewer raised a legitimate test bug: `test_validation_failure_triggers_rollback` expected `[SCANNING, PARSING]` compensation on security-step failure; spec says only prior completed steps → `[PARSING]`.

### Run 6: orchestration-code-analysis (COMPLETE ✅)
- **Provider**: codex (bg `bt8yqmxd2`)
- **Manual fix** before resume: edited `tests/test_orchestration_code_analysis.py` line ~345 to drop SCANNING from expected compensation on security-failure test. Reset `.pipeline-state` iteration to 0.
- **Stage 2 (Test Review)**: 3 iterations (revise → revise → approve). Tests frozen, hash `90669cf8…`.
- **Stage 3 (Implementation)**: PASSED first try.
- **Stage 4 (Validation)**: PASSED.
- **Stage 5 (Code Review)**: APPROVED iter 0.
- **Verification**: 34/34 tests passed, exit 0.
- Final state: `VERIFIED`.

### How to Resume
```bash
# Pipeline state is saved to .pipeline-state/. Just re-run:
cd D:/dev/avdolgikh_github_repos/spec-driven-dev-pipeline
uv run python scripts/run_pipeline.py <task-id> --provider codex --repo-root D:/dev/avdolgikh_github_repos/multi-agent

# To start spec fresh, delete state first:
rm -rf D:/dev/avdolgikh_github_repos/multi-agent/.pipeline-state
```

