# Spec: Observability Phase 1 — Phoenix + OpenLLMetry Wiring

APPROVED

## Goal

Wire the existing `core/tracing` OTel instrumentation to Arize Phoenix so every
multi-agent run (orchestration, choreography, hybrid) produces a live,
inspectable trace: agent graph, message flows, LLM prompts/completions, token
usage, latencies, circuit breaker state changes.

This is **backend wiring only** — no changes to agent logic. The `@traced`
decorator and `TracingManager.setup()` already emit OTel spans; this spec adds
LLM-call auto-instrumentation (OpenLLMetry) and a one-command Phoenix launch.

See `docs/observability.md` for the full rationale and Phase 2 plan.

## Source Files

The implementation creates/edits these files:

- `src/core/observability.py` — `init_observability(service_name, phoenix_endpoint)` wrapper
- `pyproject.toml` — add `arize-phoenix`, `opentelemetry-exporter-otlp`, `traceloop-sdk`
- `scripts/run_phoenix.py` — launches local Phoenix server on :6006
- `README.md` — short "How to see traces" section
- `src/orchestration/code_analysis/__main__.py` — calls `init_observability()` at startup
  (also applies to choreography/hybrid entry points when they exist)

## Requirements

### 1. Dependency additions

Add to `pyproject.toml`:
- `arize-phoenix` — local trace UI
- `opentelemetry-exporter-otlp` — OTLP HTTP exporter for Phoenix
- `traceloop-sdk` — OpenLLMetry; auto-instruments OpenAI SDK (covers Ollama via OpenAI-compat)

### 2. `init_observability()` helper (`src/core/observability.py`)

Single entry point that use-case demos call on startup:

```python
def init_observability(
    service_name: str,
    phoenix_endpoint: str = "http://localhost:6006/v1/traces",
) -> None:
    """Configure OTel export to Phoenix and enable OpenLLMetry auto-instrumentation."""
```

Behavior:
- Calls existing `TracingManager.setup(service_name, endpoint=phoenix_endpoint)`.
- Calls `Traceloop.init(app_name=service_name, disable_batch=True)` so LLM
  spans are captured and exported through the same TracerProvider.
- Idempotent — safe to call multiple times (no duplicate providers).
- Respects `OTEL_EXPORTER_OTLP_ENDPOINT` env var if set (overrides argument).
- No-op / warn-log if Phoenix/Traceloop imports fail (observability is optional).

### 3. Phoenix launcher script (`scripts/run_phoenix.py`)

Thin wrapper so users don't memorize the module path:

```python
# uv run python scripts/run_phoenix.py
# → starts Phoenix at http://localhost:6006
```

Implementation may simply `exec` `python -m phoenix.server.main serve` or import
`phoenix` and call its `launch_app()` — whichever is stable across versions.

### 4. Demo entry-point integration

Every use-case entry point (starting with `src/orchestration/code_analysis/__main__.py`)
must call `init_observability("<service-name>")` before running any agent code.

Naming: `service_name` = use-case module name (`"orchestration-code-analysis"`,
`"choreography-research"`, `"hybrid-analysis"`). This gives each demo its own
Phoenix project view.

### 5. README section

Add a short block to the repo `README.md`:

```markdown
## See traces live

In one terminal:
    uv run python scripts/run_phoenix.py

In another:
    uv run python -m src.orchestration.code_analysis

Open http://localhost:6006 — Agent Graph tab shows the live trace.
```

## Non-Requirements (out of scope)

- Phase 2 stack (Langfuse, Jaeger, SigNoz) — separate future spec.
- Metrics pipeline (Prometheus, etc.) — tracing only for now.
- Production OTel collector config — local Phoenix endpoint is fine.
- Custom span processors or samplers beyond defaults.
- Evaluation harnesses, prompt registries (Langfuse territory).

## Acceptance Criteria

1. `uv sync` installs Phoenix + Traceloop cleanly.
2. `uv run python scripts/run_phoenix.py` starts the UI at :6006.
3. Running any existing use-case demo produces visible spans in Phoenix with:
   - A parent span per pipeline run
   - Child spans for each `@traced` agent method
   - LLM call spans with prompts, completions, model name, token counts
   - `circuitbreaker.state_change` events where applicable
4. Trace context propagates across the message bus (existing behavior preserved).
5. `init_observability()` is idempotent and does not break tests when Phoenix is
   not running.
6. No regressions — all pre-existing tests still pass.

## Testing

- Unit test `init_observability()` — idempotency, env-var override, graceful
  degradation when imports fail (monkeypatch `traceloop` import).
- Smoke test: run `src/orchestration/code_analysis` demo against a locally
  started Phoenix, assert at least one span reaches the exporter (can use a
  capturing in-memory exporter in tests instead of a real Phoenix instance).
- Do NOT hit real Phoenix in the test suite. Tests must pass without any
  external service running.
