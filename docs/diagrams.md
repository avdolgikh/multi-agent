# Architecture Diagrams

## Orchestration — Sequential Code Analysis

A central `CodeAnalysisOrchestrator` drives four agents through a fixed pipeline. Each step's output is snapshotted; on failure, a `SagaCoordinator` rolls back completed steps in reverse order. The pattern is *centralised control, explicit state machine, compensating transactions*.

```
                  Input file path
                        │
                        ▼
        ┌──────────────────────────────────┐
        │  CodeAnalysisOrchestrator         │
        │  state: PENDING → ... → COMPLETED │
        │  saga: snapshot after each step   │
        └────────┬─────────────────────────┘
                 │  run_step(step, agent)
     ┌───────────┼───────────┬───────────┐
     ▼           ▼           ▼           ▼
  PARSING    SCANNING    CHECKING    REPORTING
  Parser     Security    Quality     Report
  Agent      Agent       Agent       Agent
     │           │           │           │
     └──── each: Agent.execute → Agent.llm span ────┘
                        │
                        ▼
        On failure at step N:
        SagaCoordinator.compensate(steps[:N])
        → rollback in reverse order → ROLLED_BACK
```

```mermaid
flowchart TD
    INPUT["input_path"] --> ORCH["CodeAnalysisOrchestrator"]
    ORCH -->|step 1| P["ParserAgent<br/>PARSING"]
    P -->|ParseResult| S["SecurityAgent<br/>SCANNING"]
    S -->|SecurityResult| Q["QualityAgent<br/>CHECKING"]
    Q -->|QualityResult| R["ReportAgent<br/>REPORTING"]
    R --> DONE["AnalysisReport<br/>status=completed"]
    S -.failure.-> SAGA["SagaCoordinator<br/>compensate prior steps"]
    Q -.failure.-> SAGA
    R -.failure.-> SAGA
    SAGA --> RB["status=rolled_back"]
```

Key property: a single trace per run. Phoenix shows the pipeline root, one `Agent.execute` span per step, and one `Agent.llm` child span per agent (13 spans total in a healthy run).

## Choreography — Event-Driven Research Aggregation

No orchestrator. `InitiatorAgent` publishes `ResearchRequested`; four search agents subscribe *independently* and each publish `FindingDiscovered` when they have something. `CrossReferenceAgent` reacts to findings. `AggregatorAgent` accumulates and emits `ResearchComplete` + `ResearchBrief`. The pattern is *decentralised reaction, shared event stream, no direct calls between agents*.

```
  InitiatorAgent                MessageBus                   EventStore
       │                            │                            │
       │  ResearchRequested ───────>│ ──────────────────────────>│ append
       │                            │
       │        ┌───────────────────┼───────────────────┐
       │        │ fan-out subscribe │                   │
       │        ▼                   ▼                   ▼
       │   WebSearch           AcademicSearch       CodeAnalysis  + NewsSearch
       │   Agent               Agent                Agent          Agent
       │        │                   │                   │              │
       │        │   each: .llm span + _summarize_entries               │
       │        │   _build_finding_payload(summary=...)                │
       │        │                   │                   │              │
       │   FindingDiscovered   FindingDiscovered   FindingDiscovered  ...
       │        │                   │                   │              │
       │        └───────────────────┼───────────────────┘
       │                            ▼
       │                      CrossReferenceAgent
       │                            │
       │                       CrossReferenceFound
       │                            │
       │                            ▼
       │                      AggregatorAgent
       │                            │
       │                     ResearchComplete + ResearchBrief
       │
       └──── DLQMonitorAgent watches for AgentError / dead-letter events ─────
```

```mermaid
flowchart LR
    INIT["InitiatorAgent"] -->|ResearchRequested| BUS((MessageBus))
    BUS -.subscribe.-> W["WebSearchAgent"]
    BUS -.subscribe.-> A["AcademicSearchAgent"]
    BUS -.subscribe.-> C["CodeAnalysisAgent"]
    BUS -.subscribe.-> N["NewsSearchAgent"]
    W -->|FindingDiscovered| BUS
    A -->|FindingDiscovered| BUS
    C -->|FindingDiscovered| BUS
    N -->|FindingDiscovered| BUS
    BUS -.subscribe.-> X["CrossReferenceAgent"]
    X -->|CrossReferenceFound| BUS
    BUS -.subscribe.-> AGG["AggregatorAgent"]
    AGG -->|ResearchComplete<br/>ResearchBrief| OUT["stdout / downstream"]
    BUS -.error stream.-> DLQ["DLQMonitorAgent"]
```

Key property: one trace per agent (not per run), linked by the `trace_context` field on events. No agent holds a reference to another — the structural invariant is enforced by `test_initiator_agent_has_no_direct_references_to_other_agents`.

---

## Core Infrastructure Diagrams

The diagrams below describe the shared plumbing that both patterns use — agents, bus, resilience, tracing.

## 1. How an Agent Talks to an LLM

The core flow: any agent calls an LLM through a resilient, traced pipeline.

```
  AgentTask ──> BaseAgent.execute()
                     |
                call_llm(messages)
                     |
            ┌────────┴────────┐
            │  OpenTelemetry  │  create span, set agent.id/model/provider
            │     Span        │
            └────────┬────────┘
                     |
            ┌────────┴────────┐
            │ CircuitBreaker  │  closed ──> pass through
            │                 │  open   ──> reject (CircuitOpenError)
            └────────┬────────┘
                     |
            AsyncOpenAI(base_url, api_key)
            chat.completions.create(model, messages)
                   /          \
       ┌──────────┘            └──────────┐
       │  provider="ollama"               │  provider="openai"
       │  localhost:11434/v1              │  api.openai.com/v1
       │  qwen3-coder, gemma4, ...       │  gpt-4o-mini
       └──────────┐            ┌──────────┘
                   \          /
              LLMResponse(content, usage, model, provider)
```

```mermaid
flowchart TD
    TASK["AgentTask"] --> EXEC["BaseAgent.execute()"]
    EXEC --> CALL["call_llm(messages)"]
    CALL --> SPAN["OTel Span<br/>agent.id, model, provider"]
    SPAN --> CB["CircuitBreaker"]
    CB -->|closed| CHAT["AsyncOpenAI.chat.completions.create"]
    CB -->|open| ERR["CircuitOpenError"]
    CHAT -->|"provider=ollama"| OLLAMA["Ollama<br/>localhost:11434"]
    CHAT -->|"provider=openai"| OPENAI["OpenAI API<br/>api.openai.com"]
    OLLAMA --> RESP["LLMResponse"]
    OPENAI --> RESP
```

## 2. Agent-to-Agent Communication via Message Bus

Agents don't call each other directly. They communicate through a queue-backed async message bus with pub/sub and request/reply.

```
  Agent A                    InMemoryBus                    Agent B
    |                            |                            |
    |  publish("topic", msg)     |                            |
    |──────────────────────────>│|                            |
    |                       ┌───┴───┐                         |
    |                       │ Queue │──> handler(msg) ───────>│
    |                       └───────┘                         |
    |                       ┌───────┐                         |
    |                       │ Queue │──> handler(msg) ───> Agent C
    |                       └───────┘                         |
    |                                                         |
    |  request("topic", msg, timeout=5)                       |
    |──────────────────────────>│                              |
    |                       ┌───┴───┐                         |
    |                       │ Queue │──> handler(msg)          |
    |                       │Future │<── return reply_msg ────│
    |<──────────────────────│result │                          |
    |  reply Message        └───────┘                         |
```

```mermaid
sequenceDiagram
    participant A as Agent A
    participant Bus as InMemoryBus
    participant B as Agent B
    participant C as Agent C

    Note over A,C: Pub/Sub (fan-out)
    A->>Bus: publish("topic", msg)
    Bus->>B: queue -> handler(msg)
    Bus->>C: queue -> handler(msg)

    Note over A,C: Request/Reply
    A->>Bus: request("topic", msg, timeout)
    Bus->>B: queue -> handler(msg)
    B-->>Bus: return reply Message
    Bus-->>A: Future resolves with reply
```

## 3. Resilience: What Happens When Things Fail

Circuit breaker, retries, and dead letter queue work together to handle failures.

```
                         Agent calls LLM
                              |
                     ┌────────┴────────┐
                     │  RetryPolicy    │  max_retries=3
                     │  exp. backoff   │  base_delay * 2^attempt + jitter
                     └────────┬────────┘
                              |
                     ┌────────┴────────┐
                     │ CircuitBreaker  │
                     └────────┬────────┘
                           /  |  \
                      ┌───┘   |   └───┐
                 CLOSED    HALF_OPEN   OPEN
                 pass      probe (1)   reject all
                 through   success?    ──> CircuitOpenError
                    |       /    \
                    |    yes      no
                    |     |       |
                    |  CLOSED    OPEN
                    |             |
                    v             v
               success     failure ──> DeadLetterQueue
                                        |
                                   ┌────┴────┐
                                   │ capture  │  store original msg + error
                                   │ retry()  │  re-publish to bus
                                   │ purge()  │  discard
                                   └─────────┘
```

```mermaid
stateDiagram-v2
    [*] --> CLOSED
    CLOSED --> OPEN : failures >= threshold
    OPEN --> HALF_OPEN : recovery_timeout elapsed
    HALF_OPEN --> CLOSED : probe succeeds
    HALF_OPEN --> OPEN : probe fails

    state CLOSED {
        [*] --> PassThrough
        PassThrough --> RetryPolicy: on failure
        RetryPolicy --> PassThrough: retry with backoff
    }

    state OPEN {
        [*] --> Reject
        Reject --> DeadLetterQueue: capture failed msg
    }
```

## 4. Distributed Tracing Across Agent Boundaries

Trace context propagates through messages so you can see the full call chain in Jaeger/Phoenix.

```
  Agent A (Span: "parser.llm")
    |
    | inject_context()
    |  -> {trace_id: abc, span_id: 123}
    |
    | publish(msg with trace_context={trace_id: abc, span_id: 123})
    |──────────────────> InMemoryBus ──────────────────> Agent B
                                                          |
                                         extract_context(msg.trace_context)
                                           -> restore parent span
                                                          |
                                         start_as_current_span("scanner.llm",
                                             context=parent)
                                                          |
                                         trace_id == abc  (same trace!)
                                         span_id == 456   (new child span)
                                                          |
    ┌─────────────────────────────────────────────────────┘
    |
    v  What you see in the tracing UI:
    ┌──────────────────────────────────────────────────┐
    │ Trace abc                                        │
    │ ├── parser.llm (Agent A)         42ms            │
    │ │   └── openai.chat             38ms             │
    │ └── scanner.llm (Agent B)        67ms            │
    │     └── openai.chat             61ms             │
    └──────────────────────────────────────────────────┘
```

```mermaid
sequenceDiagram
    participant A as Agent A
    participant T as Tracing
    participant Bus as InMemoryBus
    participant B as Agent B

    A->>T: inject_context()
    T-->>A: {trace_id, span_id}
    A->>Bus: publish(msg + trace_context)
    Bus->>B: deliver msg
    B->>T: extract_context(msg.trace_context)
    T-->>B: parent Context restored
    B->>B: start_as_current_span("child", context=parent)
    Note over A,B: Same trace_id links both agents
```
