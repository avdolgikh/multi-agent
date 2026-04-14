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
| 2 | `orchestration-code-analysis` + `-llm` | Orchestrated code analysis pipeline with LLM-backed agents | DONE |
| 3 | `choreography-research` + `-llm-surface` | Event-driven research with surfaced LLM summaries | DONE |
| 4 | `observability-phase1` | Phoenix + OpenLLMetry wiring | DONE |
| 5 | `vertical-validation` | Scaffolding (fixtures + CLI runner) for live Ollama + Phoenix runbook | DONE |
| 6 | `hybrid-analysis` | Hybrid pattern + comparison harness | **Deferred to Milestone 2** |

> **`vertical-validation` is split**: the *spec* is pipeline-implementable scaffolding — fixtures + CLI runner, all externals mocked. The *runbook* (`docs/vertical-validation-runbook.md`) is manual — human runs Ollama + Phoenix, inspects traces.

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

### Lessons Learned (load-bearing rules for future specs)

- **Branch-before-merge spec work risks divergence.** Pipelines cut before a prior spec merges produce rebase conflicts on the same files. Sequence specs, or rebase onto master before Stage 4.
- **Pipeline tests don't catch post-merge integration bugs.** The pipeline validates tests-in-worktree. Dual-import + OTel set-once issues only surface after merge. Plan: trial-merged validation (future pipeline work).
- **Unit-test isolation is load-bearing.** Autouse fixtures stub external I/O (Traceloop, OTLP HTTP) and reset OTel globals — codified in `tests/conftest.py` and the Conventions section. Any new test must preserve or re-establish these stubs. No test makes a network call.
- **Stage 1 NO_EFFECT recovery.** When a task's tests already exist in the repo (e.g. added manually or from a prior session), Stage 1 exits code 10. Recovery: compute current tests hash, seed `.pipeline-state/<task>.json` with `stage=TESTS_FROZEN` + that hash, then re-run — the pipeline resumes at Stage 3 (Implementation).
- **Specs need a `## Source Files` section** listing the `.py` filenames in backticks. The pipeline extracts `test_<stem>` terms from those to match test files to tasks.

---

### How to Resume a Pipeline

```bash
# Pipeline state is saved to .pipeline-state/. Just re-run:
cd D:/dev/avdolgikh_github_repos/spec-driven-dev-pipeline
uv run python scripts/run_pipeline.py <task-id> --provider codex --repo-root D:/dev/avdolgikh_github_repos/multi-agent --config D:/dev/avdolgikh_github_repos/multi-agent/pipeline-config.toml

# To start a spec fresh, delete state first:
rm -rf D:/dev/avdolgikh_github_repos/multi-agent/.pipeline-state/<task-id>.json
```

