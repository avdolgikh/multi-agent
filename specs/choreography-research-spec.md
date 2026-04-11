# Spec: Choreography — Multi-Source Research Aggregation

## Goal

Build a choreography-based multi-agent research system that demonstrates **why choreography is the right pattern** for parallel, independent, event-driven work where no single agent knows the full picture upfront.

Given a research topic, multiple specialist agents independently explore different source types (web, academic, code repositories, news), publish findings as events, react to each other's discoveries, and an aggregator synthesizes the final research brief — all without a central controller.

Lives under `src/choreography/research/`. Depends on `src/core/`.

## Source Files

The implementation creates these key modules:

- `src/choreography/research/events.py` — ResearchRequested, FindingDiscovered, CrossReferenceFound, SourceExhausted, ResearchComplete, AgentError
- `src/choreography/research/agents.py` — InitiatorAgent, WebSearchAgent, AcademicSearchAgent, CodeAnalysisAgent, NewsSearchAgent, CrossReferenceAgent, AggregatorAgent
- `src/choreography/research/event_log.py` — reconstruct_timeline, ResearchTimeline
- `src/choreography/research/runner.py` — ResearchRunner (entry point logic)

## Requirements

### 1. Event-Driven Architecture

No central orchestrator. Agents coordinate exclusively through events on the message bus:

```
                           ┌─────────────────────┐
                           │   Redis Streams      │
                           │   (Event Bus)        │
                           └──┬──┬──┬──┬──┬──┬───┘
                              │  │  │  │  │  │
   ┌─────────┐  ┌─────────┐  │  │  │  │  │  │  ┌──────────┐  ┌────────────┐
   │Initiator│  │   Web   │  │  │  │  │  │  │  │Aggregator│  │  DLQ       │
   │         │──│ Searcher│──┘  │  │  │  │  └──│          │  │  Monitor   │
   └─────────┘  └─────────┘     │  │  │  │     └──────────┘  └────────────┘
                ┌─────────┐     │  │  │  │
                │Academic │─────┘  │  │  │
                │ Scholar │        │  │  │
                └─────────┘        │  │  │
                ┌─────────┐        │  │  │
                │  Code   │────────┘  │  │
                │ Analyst │           │  │
                └─────────┘           │  │
                ┌─────────┐           │  │
                │  News   │───────────┘  │
                │ Scanner │              │
                └─────────┘              │
                ┌─────────┐              │
                │ Cross-  │──────────────┘
                │ Ref     │
                └─────────┘
```

### 2. Event Types (`src/choreography/research/events.py`)

All events extend `core.messaging.Message`:

| Event | Published By | Consumed By | Purpose |
|-------|-------------|-------------|---------|
| `ResearchRequested` | Initiator | All searchers | Starts the research process |
| `FindingDiscovered` | Any searcher | Cross-referencer, Aggregator | A searcher found something relevant |
| `CrossReferenceFound` | Cross-referencer | Aggregator, originating searcher | Two findings from different sources corroborate or conflict |
| `SourceExhausted` | Any searcher | Aggregator | A searcher has finished exploring its domain |
| `ResearchComplete` | Aggregator | (terminal) | All sources exhausted and brief compiled |
| `AgentError` | Any agent | DLQ Monitor | An agent encountered an error |

Each event Pydantic model must include: `research_id: str`, `event_type: str`, `timestamp: datetime`, `trace_context: dict`, plus event-specific fields.

### 3. Research Agents (`src/choreography/research/agents/`)

Each agent extends `BaseAgent` and subscribes to relevant event topics:

#### 3.1 `InitiatorAgent`
- Receives a research topic from the user
- Publishes `ResearchRequested` with: `topic: str`, `scope: str`, `deadline: datetime | None`
- Does not coordinate anything further — purely fires and forgets

#### 3.2 `WebSearchAgent`
- Subscribes to `ResearchRequested`
- Uses LLM + web search tool to explore the topic
- Publishes one `FindingDiscovered` per significant finding: `source_type: "web"`, `title: str`, `summary: str`, `url: str`, `relevance_score: float` (0-1), `raw_content: str`
- Publishes `SourceExhausted` when done
- Reacts to `CrossReferenceFound` events that mention its findings — can do follow-up searches

#### 3.3 `AcademicSearchAgent`
- Same pattern as WebSearchAgent but for academic/research sources
- `source_type: "academic"`, includes `authors: list[str]`, `year: int | None`

#### 3.4 `CodeAnalysisAgent`
- Searches code repositories for implementations related to the topic
- `source_type: "code"`, includes `repository: str`, `language: str`

#### 3.5 `NewsSearchAgent`
- Searches recent news for the topic
- `source_type: "news"`, includes `published_date: datetime | None`

#### 3.6 `CrossReferenceAgent`
- Subscribes to all `FindingDiscovered` events
- Compares new findings against previously seen findings (maintains local state)
- When it detects overlap or contradiction between two findings from different sources, publishes `CrossReferenceFound`: `finding_a_id: str`, `finding_b_id: str`, `relationship: Literal["corroborates", "contradicts", "extends"]`, `explanation: str`

#### 3.7 `AggregatorAgent`
- Subscribes to `FindingDiscovered`, `CrossReferenceFound`, `SourceExhausted`
- Maintains running tally of findings per source
- When all sources have published `SourceExhausted`, uses LLM to synthesize a final `ResearchBrief`
- Publishes `ResearchComplete` with the brief
- `ResearchBrief` model: `topic: str`, `summary: str`, `key_findings: list[Finding]`, `cross_references: list[CrossReference]`, `sources_consulted: dict[str, int]`, `confidence_score: float`

### 4. Event Sourcing (`src/choreography/research/event_log.py`)

- All events are persisted to `EventStore` (from core) under stream `research:{research_id}`
- `async reconstruct_timeline(research_id: str) -> ResearchTimeline` — replays all events to build an ordered timeline of what happened, who discovered what, and how findings connected
- `ResearchTimeline`: `events: list[Event]`, `findings_by_source: dict[str, list]`, `cross_references: list`, `duration_ms: float`
- This is the **lineage reconstruction** demonstration — in choreography "the chain" is implicit, but event sourcing makes it explicit

### 5. Dead Letter Queue Integration

- When any agent fails to process an event (LLM error, timeout, malformed input), the event is sent to the DLQ via `core.resilience.DeadLetterQueue`
- `DLQMonitorAgent` subscribes to `AgentError` events and logs failures with full context
- Failed events can be retried via the DLQ retry mechanism

### 6. Distributed Tracing

- Each `ResearchRequested` event starts a new trace
- Every subsequent event carries the trace context
- The aggregator's final span links back to the initiator's span, showing the full distributed trace across all agents
- This demonstrates **lineage reconstruction via tracing** — complementary to event sourcing

### 7. Entry Point

- `python -m choreography.research "topic goes here"` starts the system
- Agents run concurrently via asyncio
- Prints findings as they arrive (streaming output)
- Prints the final `ResearchBrief` when complete
- Graceful shutdown: if interrupted, persists all events so far (resumable via event replay)

## Acceptance Criteria

1. **Event-driven flow with no orchestrator**: The `InitiatorAgent` publishes one event and is done. All other agents react to events autonomously. No agent calls another agent directly.

2. **Parallel agent execution**: At least 3 search agents run concurrently (verified by overlapping timestamps in the event log).

3. **Cross-referencing works**: When two agents publish findings on the same sub-topic, the `CrossReferenceAgent` detects the overlap and publishes a `CrossReferenceFound` event linking them.

4. **Event sourcing reconstruction**: After a completed research run, `reconstruct_timeline(research_id)` produces a complete timeline that includes every finding and cross-reference, in causal order.

5. **DLQ captures failures**: When an agent raises an exception during event processing, the failed event appears in the DLQ with the error message. It does not stop other agents from continuing.

6. **Aggregator waits for all sources**: The `ResearchComplete` event is only published after all source agents have published `SourceExhausted`. Early completion is not possible.

7. **Trace propagation across agents**: A single trace ID connects the initiator's `ResearchRequested` through searcher `FindingDiscovered` events to the aggregator's `ResearchComplete`. The trace can be reconstructed from the event log's `trace_context` fields.

8. **Graceful degradation**: If one search agent fails entirely (circuit breaker open), the remaining agents complete and the aggregator produces a partial brief noting which sources were unavailable.

9. **All events are Pydantic models**: Every event type validates its schema on creation. Invalid events (missing required fields) raise `ValidationError`.

10. **Findings have provenance**: Each `FindingDiscovered` event includes `source_type` and enough metadata to trace it back to its origin.
