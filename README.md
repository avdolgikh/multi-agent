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
- [Ollama](https://ollama.com/) with a pulled model, or an `OPENAI_API_KEY`

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

In one terminal:
    uv run python scripts/run_phoenix.py

In another:
    uv run python -m src.orchestration.code_analysis

Open http://localhost:6006 - Agent Graph tab shows the live trace.

## Build Roadmap

| # | Spec | What It Builds | Status |
|---|------|----------------|--------|
| 1 | `core-infrastructure` | Agents, messaging, tracing, state, resilience | Done |
| 2 | `orchestration-code-analysis` | Sequential pipeline: parse, scan, check, report | Pending |
| 3 | `choreography-research` | Event-driven multi-source research | Pending |
| 4 | `hybrid-analysis` | Hybrid pattern + comparison harness | Pending |

## Tech Stack

- **LLM**: Ollama (local, primary) + OpenAI (cloud, fallback) — both via OpenAI SDK
- **Async**: asyncio throughout, no threads
- **Messaging**: In-memory queue-backed bus, no external dependencies
- **Tracing**: OpenTelemetry with OTLP export
- **Observability**: Arize Phoenix (local dev), Langfuse + Jaeger (production) — see [docs/observability.md](docs/observability.md)
- **CI**: GitHub Actions — ruff, pyright, pytest
