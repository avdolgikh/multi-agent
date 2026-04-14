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
- `PHOENIX_HOST` / `PHOENIX_PORT` — bind address for `scripts/run_phoenix.py`
  (defaults `127.0.0.1` / `6006`; gRPC ingestion on `4317` is fixed upstream).
  Phoenix's own CLI does **not** take `--host` / `--port` — use env vars.
- `PHOENIX_URL` — base URL used by `scripts/phoenix_trace.py` (default
  `http://localhost:6006`). The script pulls a trace via Phoenix's GraphQL
  endpoint (`/graphql`) so we can inspect span trees from the terminal
  without opening the browser.

### Inspecting a Phoenix trace from the terminal

```bash
# list projects
uv run python scripts/phoenix_trace.py --list-projects <anyid>

# fetch a specific trace (auto-discovers the project)
PYTHONIOENCODING=utf-8 uv run python scripts/phoenix_trace.py <TRACE_ID>

# dump raw span JSON
uv run python scripts/phoenix_trace.py <TRACE_ID> --raw
```

Output: span tree with latencies, plus one line per LLM span
(prompt/completion tokens, status, output preview). Trace IDs are the 32-hex
ID in the Phoenix URL. A healthy `code_analysis.pipeline` run is **13 spans**:
1 root + 4 `STAGE.execute` + 4 `Agent.execute` + 4 `Agent.llm`.

## Dependency Pins (non-obvious)

- `arize-phoenix-evals<3.0.0` — `arize-phoenix-evals==3.0.0` drops
  `phoenix.evals.models`, which `arize-phoenix==13.21` still imports. Pinning
  <3.0.0 is required until Phoenix upstream catches up. Remove the pin only
  after verifying `uv run python scripts/run_phoenix.py` still starts.

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
| 3 | `choreography-research` | Event-driven multi-source research | DONE (Codex, 2026-04-12) |
| 4 | `vertical-validation` | **Next**: scaffolding only (fixtures + CLI runner) so we can manually exercise shipped verticals vs real Ollama + Phoenix | PENDING |
| 5 | `hybrid-analysis` | Hybrid pattern + comparison harness | PENDING (blocked on vertical-validation runbook findings) |
| — | `observability-phase1` | Phoenix + OpenLLMetry wiring (independent) | DONE (Codex, 2026-04-12; merged to master) |

> **`vertical-validation` is split**: the *spec* (`specs/vertical-validation-spec.md`) is pipeline-implementable scaffolding only — fixtures + CLI runner, all externals mocked, pytest-verifiable. The *runbook* (`docs/vertical-validation-runbook.md`) is manual — human runs Ollama + Phoenix, inspects traces, writes findings doc. Pipeline builds the rails; human drives the car. Findings feed into `hybrid-analysis`.

### Pipeline Run Commands
```bash
cd D:/dev/avdolgikh_github_repos/spec-driven-dev-pipeline

# Primary: Codex provider (with full validation suite — ruff + format + pyright + pytest)
uv run python scripts/run_pipeline.py <task-id> --provider codex --repo-root D:/dev/avdolgikh_github_repos/multi-agent --config D:/dev/avdolgikh_github_repos/multi-agent/pipeline-config.toml

# Secondary: Gemini provider
uv run python scripts/run_pipeline.py <task-id> --provider gemini --repo-root D:/dev/avdolgikh_github_repos/multi-agent --config D:/dev/avdolgikh_github_repos/multi-agent/pipeline-config.toml
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

### Run 7: observability-phase1 (COMPLETE ✅, merged 2026-04-12)
- **Provider**: codex, isolated worktree `../multi-agent-obs` on branch `observability-phase1`. Early Gemini attempt (bg `bqnsn54ho`) failed on quota → switched to Codex.
- Codex first run (bg `bg5oejfzj`) hit revision cap at iter 4 with two legitimate reviewer blockers (README demo command assertion + smoke test service-name check).
- **Manual fix** + resume (bg `bxqptkmy3`): added the two assertions, reset state iter→0. All stages approved first-try. 26/26 frozen tests passed in-worktree.
- **Merge to master**: rebased onto master, resolved 2 conflicts (`__init__.py`, `__main__.py`) by keeping master's full orchestrator and adding `init_observability("orchestration-code-analysis")` in `main()`.
- **Post-rebase test failures** (fixed in commit `fbcb717`):
  - Smoke test exited 1: demo file had no functions/classes → `StepValidator` rejected. Fixed test to use `def demo(): ...`.
  - Pydantic `ValidationError` in orchestrator: `runpy.run_module("src.orchestration.code_analysis", ...)` caused a dual-import (`src.*` vs non-`src.*`) — two different `QualityResult` classes failing isinstance. Fixed by switching the smoke test to `runpy.run_module("orchestration.code_analysis", ...)` (matches master's CLI test convention).
  - `test_distributed_tracing_records_child_spans` flaky: OTel's global TracerProvider is set-once; smoke test poisoned it. Fixed via autouse `_reset_tracer_provider_state` fixture in `conftest.py` (resets `TracingManager._provider` + `opentelemetry.trace._TRACER_PROVIDER(_SET_ONCE)`).
  - CLI entry tests printed Traceloop "missing API key" + tried real OTLP HTTP to localhost:6006. Fixed via autouse `_stub_external_telemetry` fixture in `conftest.py` (stubs `traceloop.sdk.Traceloop.init` + OTLP HTTP exporter globally). Enforces the unit-test rule codified in Conventions.
- CI: green on master after merge.

### Lessons learned (2026-04-12)
- **Branch-before-merge spec work risks divergence.** `observability-phase1` was cut before `orchestration-code-analysis` merged → rebase conflicts on files the pipeline added to both sides. Prefer: sequence specs, or rebase the feature branch onto master before the pipeline's Stage 4 runs.
- **Pipeline tests can't catch post-merge integration bugs.** The pipeline validated tests-in-worktree (26/26 green). Dual-import + OTel set-once only surfaced after merge integrated master's richer orchestrator. Plan: extend pipeline to also validate on a trial-merged tree (future task).
- **Unit-test isolation is load-bearing.** Autouse fixtures to stub external I/O and reset OTel globals prevent both leaked external calls and cross-test state pollution. Codified in Conventions; apply to all future specs.

### Run 8: choreography-research (IN PROGRESS — paused on Codex quota 2026-04-12)
- **Provider**: codex.
- **First attempt** (bg `bozb5znpo`): hit Stage 2 revision cap at iter 4 with two legitimate reviewer blockers — (a) no test for "no agent calls another agent directly" choreography guarantee (spec §Constraints 1); (b) `ResearchBrief` must be a Pydantic model per spec §3.7. Same Run 5→6 pattern.
- **Manual fix**: appended three tests at tail of `tests/test_choreography_research.py` — `test_initiator_agent_has_no_direct_references_to_other_agents` (structural source check), `test_initiator_publishes_research_requested_as_first_event` (runtime first-event check), `test_research_brief_is_pydantic_model_with_required_fields` (model + typed-field check). Reset `.pipeline-state` iter→0.
- **Resume attempt** (bg `besylnv1q`): progressed all the way through Stage 2 approval (4 more iters, hash `33252be4…`), Stage 3 impl first-try, Stage 4 validation **60/60 green** (full suite: ruff + format + pyright + pytest — `pipeline-config.toml` working as designed), Stage 5 Code Review iter 1 revise (legit spec gaps: hardcoded findings, deterministic brief, empty `research_id` default), Stage 5 Code Review iter 2 revise (timeline ordering bug — see below).
- **Paused**: Stage 5b iter 2 implementer revision failed with Codex OpenAI quota (`try again at 6:58 PM`). Pipeline exit 9. State intact.
- **Pending reviewer blocker**: `reconstruct_timeline` sorts by `timestamp`; `SearchAgent` uses `context.started_at` for findings, which can be ≤ `ResearchRequested`'s timestamp → `FindingDiscovered` sorts before `ResearchRequested`, breaking the initiator-first-event test. Fix direction: sort timeline by event store sequence. Caught by the choreography test added in the manual fix above.
- **Next session resume**: re-run the same command (state resumes from CODE_VALIDATED iter 2). See `memory/project_choreography_research_resume.md` for the one-line command and full protocol.

### Run 8 (resumed): choreography-research (COMPLETE ✅, 2026-04-12 19:04 local)
- **Provider**: codex (bg `byd1x9505`, resume after quota reset).
- Pipeline resumed at `CODE_VALIDATED iter 2`. Codex revised implementation for the timeline ordering bug (FindingDiscovered sorting before ResearchRequested) plus remaining iter-1 items.
- **Stage 4 Validation (iter 4 post-revision)**: 60/60 tests green; ruff + format + pyright + pytest all clean.
- **Stage 5 Code Review iter 4**: APPROVED — "All choreography-research requirements appear satisfied, and the full validation suite passes."
- **Verification gate**: 60/60 tests passed, ruff + format + pyright clean.
- Final state: `VERIFIED`. Frozen tests hash `33252be4…`.
- **Lessons confirmed**: the Run 5→6 manual-test-addition pattern continues to be the right escape hatch for legit blockers hitting the revision cap; the `pipeline-config.toml` full validation suite caught the timeline-ordering regression before merge.

### How to Resume
```bash
# Pipeline state is saved to .pipeline-state/. Just re-run:
cd D:/dev/avdolgikh_github_repos/spec-driven-dev-pipeline
uv run python scripts/run_pipeline.py <task-id> --provider codex --repo-root D:/dev/avdolgikh_github_repos/multi-agent --config D:/dev/avdolgikh_github_repos/multi-agent/pipeline-config.toml

# To start spec fresh, delete state first:
rm -rf D:/dev/avdolgikh_github_repos/multi-agent/.pipeline-state
```

