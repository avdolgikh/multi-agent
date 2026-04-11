# Spec: Core Infrastructure

## Goal

Build the shared infrastructure layer that all use cases (orchestration, choreography, hybrid) depend on. This layer provides: base agent abstractions, a messaging bus, distributed tracing, state management, and resilience primitives.

Everything lives under `src/core/` and is imported by the use-case packages.

## Source Files

The implementation creates these key modules:

- `src/core/agents.py` — Base agent abstraction, AgentTask, AgentResult, Tool, LLMResponse
- `src/core/messaging.py` — MessageBus protocol, InMemoryBus, Message
- `src/core/tracing.py` — TracingManager, traced decorator, inject/extract context
- `src/core/state.py` — EventStore, InMemoryEventStore, SnapshotStore, Event, Snapshot
- `src/core/resilience.py` — CircuitBreaker, RetryPolicy, DeadLetterQueue

## Requirements

### 1. Base Agent Abstraction (`src/core/agents/`)

#### 1.1 `BaseAgent`
- Abstract async class with:
  - `agent_id: str` — unique identifier
  - `name: str` — human-readable name
  - `model: str` — LLM model identifier (e.g. `"qwen3:8b"` for Ollama, `"gpt-4o-mini"` for OpenAI)
  - `provider: str` — `"ollama"` or `"openai"` (Ollama is the primary/default provider)
  - `tools: list[Tool]` — list of tool definitions the agent can call
  - `system_prompt: str` — the agent's system instructions
  - `base_url: str | None` — API base URL override (default: `http://localhost:11434/v1` for Ollama, `https://api.openai.com/v1` for OpenAI)
- Abstract method `async execute(task: AgentTask) -> AgentResult` — runs the agent on a task
- Concrete method `async call_llm(messages: list[Message]) -> LLMResponse` — calls the configured LLM provider, propagates tracing context, applies circuit breaker
- Should use the OpenAI SDK for both providers since Ollama exposes an OpenAI-compatible API. The only difference is `base_url` and `api_key` (Ollama doesn't require one, use `"ollama"` as placeholder).

#### 1.2 `AgentTask` and `AgentResult`
- Pydantic models
- `AgentTask`: `task_id: str`, `input_data: dict`, `metadata: dict`, `trace_context: dict | None`
- `AgentResult`: `task_id: str`, `agent_id: str`, `output_data: dict`, `status: Literal["success", "failure", "partial"]`, `error: str | None`, `duration_ms: float`, `trace_context: dict`

#### 1.3 `Tool` protocol
- `name: str`, `description: str`, `parameters: dict` (JSON Schema)
- `async execute(params: dict) -> dict` — runs the tool
- Built-in tools: `WebSearchTool` (uses httpx to search), `FileReadTool`, `FileWriteTool`

#### 1.4 `LLMResponse`
- Pydantic model: `content: str`, `tool_calls: list[ToolCall] | None`, `usage: TokenUsage`, `model: str`, `provider: str`
- `ToolCall`: `tool_name: str`, `arguments: dict`
- `TokenUsage`: `prompt_tokens: int`, `completion_tokens: int`, `total_tokens: int`

### 2. Messaging (`src/core/messaging/`)

#### 2.1 `MessageBus` protocol
- `async publish(topic: str, message: Message) -> None`
- `async subscribe(topic: str, handler: Callable) -> Subscription`
- `async request(topic: str, message: Message, timeout: float) -> Message` — request/reply pattern
- `async unsubscribe(subscription: Subscription) -> None`

#### 2.2 `Message` model
- Pydantic: `message_id: str`, `topic: str`, `payload: dict`, `timestamp: datetime`, `trace_context: dict`, `source_agent: str | None`

#### 2.3 `InMemoryBus` implementation
- Implements `MessageBus` using asyncio queues
- This is the primary (and currently only) implementation — no external dependencies required
- Supports fan-out to multiple subscribers on the same topic
- Thread-safe for use within a single asyncio event loop

### 3. Distributed Tracing (`src/core/tracing/`)

#### 3.1 `TracingManager`
- Wraps OpenTelemetry SDK setup
- `setup(service_name: str, endpoint: str | None) -> TracerProvider`
- Creates a tracer with the given service name
- If no endpoint, uses a no-op exporter (for tests)

#### 3.2 `traced` decorator
- Decorator for async functions that creates a span
- Automatically records: function name, arguments (sanitized — no API keys), duration, errors
- Propagates trace context through `AgentTask.trace_context`

#### 3.3 Context propagation helpers
- `inject_context() -> dict` — extracts current span context into a dict (for embedding in messages)
- `extract_context(ctx: dict) -> Context` — restores span context from a dict (when receiving messages)

### 4. State Management (`src/core/state/`)

#### 4.1 `EventStore`
- Append-only event log
- `async append(stream: str, event: Event) -> int` — returns sequence number
- `async read(stream: str, from_seq: int = 0) -> list[Event]` — reads events from a position
- `async replay(stream: str) -> dict` — replays all events to reconstruct current state
- Events are Pydantic models: `event_id: str`, `stream: str`, `event_type: str`, `data: dict`, `timestamp: datetime`, `sequence: int`, `trace_context: dict`

#### 4.2 `InMemoryEventStore` implementation
- Dict-backed, primary implementation (no external dependencies)
- One list per stream, append-only

#### 4.4 `SnapshotStore`
- Immutable state snapshots for orchestration patterns
- `async save(workflow_id: str, step: str, state: dict) -> str` — returns snapshot ID
- `async load(snapshot_id: str) -> dict`
- `async history(workflow_id: str) -> list[Snapshot]` — all snapshots for a workflow, ordered

#### 4.5 `Snapshot` model
- Pydantic: `snapshot_id: str`, `workflow_id: str`, `step: str`, `state: dict`, `timestamp: datetime`

### 5. Resilience (`src/core/resilience/`)

#### 5.1 `CircuitBreaker`
- States: CLOSED, OPEN, HALF_OPEN
- Config: `failure_threshold: int` (default 5), `recovery_timeout: float` (default 30.0), `half_open_max_calls: int` (default 1)
- `async call(func: Callable, *args, **kwargs) -> Any` — executes func with circuit breaker protection
- Raises `CircuitOpenError` when circuit is open
- Emits OpenTelemetry events on state transitions

#### 5.2 `RetryPolicy`
- Config: `max_retries: int` (default 3), `base_delay: float` (default 1.0), `max_delay: float` (default 30.0), `exponential_base: float` (default 2.0)
- `async execute(func: Callable, *args, **kwargs) -> Any` — retries with exponential backoff + jitter
- Integrates with circuit breaker (stops retrying if circuit opens)

#### 5.3 `DeadLetterQueue`
- `async send(message: Message, error: str, source: str) -> None` — captures failed message
- `async list_failed(limit: int = 100) -> list[DeadLetter]`
- `async retry(dead_letter_id: str) -> bool` — re-publishes the original message
- `async purge(dead_letter_id: str) -> None`
- `DeadLetter` model: `id: str`, `original_message: Message`, `error: str`, `source: str`, `timestamp: datetime`, `retry_count: int`

### 6. Package Structure

Every directory under `src/core/` must have an `__init__.py` that re-exports its public API. The top-level `src/core/__init__.py` should make common imports convenient:

```python
from core.agents import BaseAgent, AgentTask, AgentResult, Tool
from core.messaging import MessageBus, Message, InMemoryBus
from core.tracing import TracingManager, traced, inject_context, extract_context
from core.state import EventStore, SnapshotStore, Event, Snapshot, InMemoryEventStore
from core.resilience import CircuitBreaker, RetryPolicy, DeadLetterQueue
```

## Acceptance Criteria

1. **Agent abstraction works**: Can instantiate a concrete agent subclass, call `execute()` with an `AgentTask`, and get back an `AgentResult`. The `call_llm()` method uses the OpenAI SDK, routing to Ollama or OpenAI based on `base_url`.

2. **Messaging pub/sub works**: Publishing a message to a topic delivers it to all subscribers. Using `InMemoryBus`, publish 3 messages and verify a subscriber receives all 3 in order.

3. **Messaging request/reply works**: A subscriber can respond to a request, and the requester receives the reply within the timeout. Timeout raises an appropriate error.

4. **Trace context propagates through messages**: A message published with trace context can be received and the context restored to create a child span linked to the original.

5. **Event store append and replay**: Append 5 events to a stream, then `replay()` reconstructs the aggregated state by applying all events in order.

6. **Snapshot store round-trip**: Save a snapshot, load it by ID, verify the state matches. `history()` returns snapshots in chronological order.

7. **Circuit breaker trips on failures**: After `failure_threshold` consecutive failures, subsequent calls raise `CircuitOpenError`. After `recovery_timeout`, the circuit enters HALF_OPEN and allows one probe call.

8. **Retry with exponential backoff**: A function that fails twice then succeeds is retried correctly. Delays increase exponentially between retries.

9. **Dead letter queue captures and retries**: A failed message lands in the DLQ. Listing shows it. Retrying re-publishes the original message. Purging removes it.

10. **All components use async/await**: No blocking I/O calls on the event loop.

11. **In-memory implementations pass all tests without external dependencies**: Tests using `InMemoryBus` and `InMemoryEventStore` run without Redis.
