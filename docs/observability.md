# Observability for AI Multi-Agent Systems

## What We Need to See

- Agent-to-agent message flows through InMemoryBus
- CircuitBreaker state changes, retry attempts, DeadLetterQueue activity
- LLM call latencies, token usage, model routing (Ollama vs OpenAI)
- Distributed trace propagation across agent boundaries

## Decision: Two-Phase Stack

No single tool covers both infrastructure tracing and AI-specific observability well.
We use the same OTel instrumentation in both phases -- only the backend changes.

```
INSTRUMENTATION (same code, both phases):
  our @traced decorator + CircuitBreaker OTel events   (already built)
  + OpenLLMetry / traceloop-sdk                        (to add: pip install + 2 lines)
  |
  | OTLP export via OTEL_EXPORTER_OTLP_ENDPOINT
  v
PHASE 1 - Local Prototype:   Arize Phoenix    (pip install, localhost:6006)
PHASE 2 - Production:        Langfuse (AI)  + Jaeger or SigNoz (infra)
```

### Phase 1: Arize Phoenix (now)

```bash
uv add arize-phoenix opentelemetry-exporter-otlp
python -m phoenix.server.main serve
# UI at http://localhost:6006
```

Why Phoenix:
- Zero infrastructure -- pip install, no Docker, no DB
- Auto-renders agent workflows as interactive graphs (Agent Graph)
- Shows prompts, completions, token usage, latencies
- Built on OTel (OpenInference) -- our existing tracing feeds directly into it
- Works with Ollama via OpenAI SDK instrumentation

### Phase 2: Langfuse + Jaeger/SigNoz (production)

- **Langfuse** (AI layer): prompt management, evaluations, cost tracking, session analytics. MIT license, self-hostable, native OTLP receiver. 24K+ GitHub stars.
- **Jaeger** (infra, lightweight) or **SigNoz** (infra, full APM): traces, metrics, logs, alerts. Both OTel-native, self-hostable.

## What We Already Have

`core/tracing` already emits OTel spans:
- `TracingManager.setup(service_name, endpoint)` -- configures TracerProvider with OTLP exporter
- `@traced` decorator -- auto-creates spans with error recording and sensitive value scrubbing
- `inject_context()` / `extract_context()` -- trace propagation across message bus
- `CircuitBreaker` emits `circuitbreaker.state_change` OTel events on state transitions

Connect to any backend with one env var:
```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:6006/v1/traces   # Phoenix
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317             # Jaeger
```

## What to Add (small, not a spec)

OpenLLMetry auto-instruments all OpenAI SDK calls (including Ollama via OpenAI-compatible API):
```python
from traceloop.sdk import Traceloop
Traceloop.init(app_name="multi-agent")
# Captures: prompts, completions, token counts, model, latency -- as OTel spans
```

## Rejected Alternatives

| Tool | Why Not |
|------|---------|
| **MLflow** | ML experiment tracking (training, hyperparams, model registry). Wrong problem space -- doesn't do agent message flows, circuit breaker states, or event choreography. |
| **Zipkin** | OTel actively deprecating Zipkin support (removal Dec 2026). Dead end. |
| **LangSmith** | Proprietary, cloud-only, expensive. No self-hosting without enterprise license. |
| **Helicone** | API proxy/gateway architecture. Doesn't work with local Ollama. |
| **Braintrust** | Proprietary, cloud-only. Good for evals but not observability. |
| **AgentOps** | Good agent-specific features but proprietary protocol, no OTel native. |
| **Weave (W&B)** | Tied to W&B cloud ecosystem. |
| **Grafana Tempo** | Needs multi-container setup (Tempo + Grafana + Collector). Overkill for prototype. |
| **Lunary** | Niche chatbot analytics. Partial OSS. |
| **Phospho** | Discontinued. |

## Full Research Table

| Tool | OSS | Local Startup | OTel Native | Agent Viz | LLM Features | Verdict |
|------|-----|---------------|-------------|-----------|--------------|---------|
| **Arize Phoenix** | Yes | `pip install` | Yes | Agent Graph | Full | **Phase 1 pick** |
| **Langfuse** | Yes (MIT) | Docker Compose | Yes (OTLP) | Nested spans | Full | **Phase 2 AI layer** |
| **Jaeger** | Yes (CNCF) | `docker run` | Yes (v2 = OTel) | No | No | **Phase 2 infra (light)** |
| **SigNoz** | Yes | Docker Compose | Yes (born OTel) | No | Via OpenLLMetry | **Phase 2 infra (full APM)** |
| **OpenLLMetry** | Yes | `pip install` | Pure OTel | Via backend | Instrumentation | **Glue layer** (both phases) |
