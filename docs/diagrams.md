# Architecture Diagrams

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
