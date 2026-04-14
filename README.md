# Multi-Agent Distributed System

Orchestration vs choreography in multi-agent AI systems — working demonstrations with real LLM-backed agents.

## What This Is

A side-by-side comparison of how multi-agent systems can coordinate work:

- **Orchestration** — a central coordinator assigns tasks and collects results
- **Choreography** — agents react to events independently, no central control
- **Hybrid** — orchestrated teams with choreographed intra-team work

Each pattern solves a real task (code analysis, research aggregation, code review) using local LLMs via Ollama.

## Architecture

```
src/
├── core/              # Shared infrastructure
│   ├── agents/        # Base agent + LLM routing (Ollama / OpenAI)
│   ├── messaging/     # Async message bus (pub/sub, request/reply)
│   ├── tracing/       # OpenTelemetry distributed tracing
│   ├── state/         # Event sourcing + snapshots
│   └── resilience/    # Circuit breaker, retries, dead letter queue
│
├── orchestration/     # Orchestrated code analysis pipeline
├── choreography/      # Event-driven research aggregation
├── hybrid/            # Hybrid project analysis
└── comparison/        # Same task, both patterns side-by-side
```

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- [Ollama](https://ollama.com/) running locally with at least one pulled model
  (the shipped verticals default to `qwen3-coder:latest` for orchestration and
  a mix of `qwen3.5:latest` / `qwen3-coder:latest` / `glm-4.7-flash:latest`
  for choreography; edit the `model=` kwargs in
  `src/orchestration/code_analysis/__init__.py` and
  `src/choreography/research/{runner,agents}.py` to pick your own)
- Optional: `OPENAI_API_KEY` as a fallback (avoid unless necessary)

## Quick Start

```bash
# Install dependencies
uv sync --dev

# Run checks
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest --tb=short -q
```

## See traces live

Two-terminal flow. The ports are `6006` (Phoenix UI + OTLP HTTP) and `4317`
(OTLP gRPC) — both must be free. If a prior run left something bound, see
the troubleshooting note at the bottom of
[docs/vertical-validation-runbook.md](docs/vertical-validation-runbook.md).

**Terminal A — Phoenix:**

```bash
uv run python scripts/run_phoenix.py
```

Wait for the banner ending with `Phoenix UI: http://127.0.0.1:6006`, then
open that URL.

**Terminal B — run a vertical.** Either path works:

```bash
# Via the validator CLI (recommended — adds wall-clock + exit-status)
uv run python scripts/validate_vertical.py orchestration \
    --input fixtures/validation/sample_module.py
uv run python scripts/validate_vertical.py choreography \
    --topic "event sourcing vs CQRS tradeoffs"

# Or direct module invocation
uv run python -m orchestration.code_analysis fixtures/validation/sample_module.py
uv run python -m choreography.research "event sourcing vs CQRS tradeoffs"
```

In Phoenix's **Projects** panel you'll see two projects after your first
runs — `orchestration-code-analysis` (9 spans per run: 1 pipeline root +
4 step spans + 4 agent spans) and `choreography-research` (one
`SearchAgent.execute` + nested `.llm` span per source agent, plus an
aggregator trace). Click a project → Traces tab → a trace to inspect the
span tree, latencies, and LLM prompts/completions.

For the full guided walkthrough — dry-runs, inspection checklist, findings
template — see [docs/vertical-validation-runbook.md](docs/vertical-validation-runbook.md).

## Build Roadmap

| # | Spec | What It Builds | Status |
|---|------|----------------|--------|
| 1 | `core-infrastructure` | Agents, messaging, tracing, state, resilience | Done |
| 2 | `orchestration-code-analysis` | Sequential pipeline: parse, scan, check, report | Done |
| 3 | `choreography-research` | Event-driven multi-source research | Done |
| — | `observability-phase1` | Phoenix + OpenLLMetry wiring (independent) | Done |
| 4 | `vertical-validation` | Scaffolding (fixtures + CLI runner) for live runbook | Done |
| 5 | `hybrid-analysis` | Hybrid pattern + comparison harness | Deferred (Milestone 2) |

## Tech Stack

- **LLM**: Ollama (local, primary) + OpenAI (cloud, fallback) — both via OpenAI SDK
- **Async**: asyncio throughout, no threads
- **Messaging**: In-memory queue-backed bus, no external dependencies
- **Tracing**: OpenTelemetry with OTLP export
- **Observability**: Arize Phoenix (local dev), Langfuse + Jaeger (production) — see [docs/observability.md](docs/observability.md)
- **CI**: GitHub Actions — ruff, pyright, pytest
