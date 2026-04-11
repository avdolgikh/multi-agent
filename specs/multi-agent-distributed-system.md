# Spec: Multi-Agent Distributed System

## Goal

A practical repository that demonstrates **when and why** to use orchestration vs choreography in multi-agent AI systems. 
A **working system** with real agents doing real tasks, organized so that the architectural choice (orchestration, choreography, hybrid) is driven by task properties — and the reader/user can see the difference.

The repo answers one question:

> Given a task — should I orchestrate it or choreograph it? Here's both, running on real work, so you can see for yourself.

## Non-Goals

- Not an SDK or reusable framework for others to import
- Not a toy/demo with mock LLM calls and print statements
- Not a theoretical document
- Not a benchmark suite

## Core Concept

**Shared infrastructure layer** (agents, messaging, tracing, state) + **use cases** that naturally fit different coordination patterns. Each use case:

1. States WHY this pattern fits (linking to the decision framework)
2. Runs with real LLM agents and real tools
3. Shows the hard problems it solves (sagas, tracing, error propagation)

```
┌──────────────────────────────────────────────────────────┐
│              SHARED INFRASTRUCTURE                       │
│  agents, messaging, tracing, state, fault tolerance      │
└────────┬──────────────────┬────────────────┬─────────────┘
         │                  │                │
   ┌─────▼──────┐    ┌─────▼──────┐    ┌─────▼──────┐
   │ORCHESTRATED│    │CHOREOGRAPH.│    │   HYBRID   │
   │ use case   │    │ use case   │    │ use case   │
   │            │    │            │    │            │
   │ Sequential │    │ Parallel,  │    │ Orch btwn  │
   │ dependent  │    │ independ.  │    │ teams,     │
   │ validated  │    │ event-     │    │ chor within│
   │ rollback   │    │ driven     │    │ teams      │
   └────────────┘    └────────────┘    └────────────┘
```

## Use Cases (what the agents actually do)

Pick real tasks where the pattern choice is **architecturally motivated**, not arbitrary. Examples of the right kind of tasks (the implementer should finalize):

**Orchestration territory** — sequential dependencies, need validation between steps, rollback matters:
- Multi-step code analysis pipeline (parse -> security scan -> quality check -> report)
- Research-to-document workflow (plan -> gather -> analyze -> synthesize -> validate)
- Data transformation with quality gates

**Choreography territory** — independent exploration, parallel, no single agent knows the full picture:
- Competitive/market research (agents explore different sources independently, react to each other's findings)
- Multi-source information gathering with event-driven aggregation
- Monitoring/alerting where agents watch different signals

**Hybrid territory** — strategic coordination at the top, local autonomy at the bottom:
- Complex project analysis: orchestrator assigns teams, teams self-organize
- Multi-domain research: orchestrator plans domains, domain-specialist agents explore freely within their scope

**Important**: at least one task should be implemented in BOTH patterns (orchestrated AND choreographed) so the reader can directly compare behavior, debuggability, failure handling, and performance.

## Tech Stack

- **Python 3.11**, **uv** for package management
- **Real LLM APIs**: OpenAI + Anthropic Claude (configurable)
- **Real tools**: web search, file operations, code analysis — not mocks
- **Messaging**: Redis Streams or NATS (lightweight, easy local setup) for choreography event bus
- **Tracing**: OpenTelemetry for distributed tracing across all patterns
- **State**: Event sourcing for choreography; immutable snapshots for orchestration (like LangGraph approach)
- **No heavy frameworks as orchestrators**: build the patterns from primitives (asyncio, message queues) so the architecture is visible, not hidden behind a framework. Use LLM SDKs directly.

## Repo Structure (high-level)

```
multi-agent-distributed-system/
├── README.md                    # What this is, how to run, architectural overview
├── pyproject.toml
├── .env.example
│
├── core/                        # Shared infrastructure
│   ├── agents/                  # Base agent abstractions (LLM-backed agents with tools)
│   ├── messaging/               # Event bus abstraction (pub/sub, request/reply)
│   ├── tracing/                 # Distributed tracing (OpenTelemetry integration)
│   ├── state/                   # State management (event store, snapshots)
│   └── resilience/              # Circuit breakers, retries, dead letter queue
│
├── orchestration/               # Orchestration pattern implementations
│   └── <use_case>/
│
├── choreography/                # Choreography pattern implementations
│   └── <use_case>/
│
├── hybrid/                      # Hybrid pattern implementations
│   └── <use_case>/
│
├── comparison/                  # Same task, both patterns — side-by-side
│   └── <task>/
│       ├── orchestrated/
│       └── choreographed/
│
└── docs/                        # Architecture decisions, pattern guides
```

## What "Working" Means

Each use case must:
- Run end-to-end with a single command (`uv run <use_case>`)
- Use real LLM calls (with cost awareness — use cheaper models for high-volume agent calls)
- Produce visible output: logs, traces, results
- Handle failures gracefully (not crash on LLM timeout or tool error)
- Have its own README explaining: what it does, why this pattern, what to observe

## Infrastructure Requirements That Must Be Demonstrated

These are not optional nice-to-haves. They are the point of the project:

| Requirement | Where | Why |
|-------------|-------|-----|
| **Saga / compensation** | Orchestration use case | Show rollback when step 3 of 5 fails |
| **Validation agents** | Orchestration use case | Show error amplification reduction (17.2x -> 4.4x) |
| **Event sourcing** | Choreography use case | Show lineage reconstruction from event log |
| **Distributed tracing** | All use cases | Show trace propagation across agent boundaries |
| **Circuit breakers** | All use cases | Show graceful degradation on tool/API failure |
| **Dead letter queue** | Choreography use case | Show failed task capture and retry |

## Quality Bar

- Clean, readable code
- Each pattern's strengths and weaknesses should be VISIBLE in the running system, not just documented
- Tracing output should let you reconstruct exactly what happened, in what order, and why

---

## Theoretical Foundation

This section contains the essential distributed systems knowledge that motivates every architectural decision in this repo. An implementer must understand this before writing code.

### The Core Tension

Multi-agent AI = distributed system. The moment you have >1 LLM-equipped agent, you inherit every hard problem from distributed computing — plus new ones unique to non-deterministic, language-driven actors.

```
Classical Problem          │  Multi-Agent Manifestation
───────────────────────────┼──────────────────────────────────────
Consensus                  │  Which agent's output is "truth"?
Partial failure            │  Agent 3 of 5 hallucinated — now what?
Network partition          │  Agent can't reach tool/API/other agent
Ordering guarantees        │  Event A must happen before Event B
State consistency          │  Multiple agents modify shared context
Exactly-once delivery      │  Agent retries → duplicate tool calls
Rollback / compensation    │  Undo a chain of 7 agent decisions
```

New problems unique to AI agents:

| Problem | Description |
|---------|-------------|
| **Non-determinism** | Same input -> different output. Classical distributed systems assume deterministic nodes. |
| **Attention narrowing** | LLMs lose constraints mid-sequence. Agent forgets earlier decisions as context grows. |
| **Cascading hallucination** | Agent A's hallucinated output becomes Agent B's input -> error amplifies through the chain. |
| **Error amplification** | Independent agents: 17.2x. Centralized: 4.4x. Decentralized: 7.8x. |
| **Semantic gap** | Can't distinguish a benign agent writing a script from a compromised agent writing a payload at syscall level. |

### The Autonomy-Control Spectrum

```
 CENTRALIZED                                         DECENTRALIZED
 ORCHESTRATION            HYBRID                     CHOREOGRAPHY
 <-------------------------------------------------------------->

 Predictable <-------------------------------------------> Adaptive
 Debuggable  <-------------------------------------------> Scalable
 Easy rollback <-----------------------------------------> Hard rollback
 Bottleneck / SPOF <------------------------------------> Resilient
```

### Orchestration Patterns

**One component (the orchestrator) owns the execution flow.** It decides which agent runs, when, with what input, and what happens on failure. Agents see only their own task.

| Pattern | How | Best For |
|---------|-----|----------|
| **Deterministic state machine** | Pre-defined nodes and edges. Each transition explicit. | Well-understood workflows. Compliance. |
| **LLM-orchestrated graph** | LLM decides routing at each step based on current state. | Branching logic hard to enumerate upfront. |
| **Conditional DAG** | Directed acyclic graph with conditional branching + fan-out/merge. | Parallelizable sub-tasks with reduce step. |

Scaling reality: centralized MAS shows +80.9% on structured analysis tasks, but error amplification is lowest (4.4x) because orchestrator validates between steps. Fails on open-world exploration.

### Choreography Patterns

**No central controller.** Agents react to events autonomously. Coordination emerges from the event flow.

| Pattern | How | Best For |
|---------|-----|----------|
| **Event-driven (pub/sub)** | Agents publish events, others subscribe. | High throughput, loosely coupled, independent streams. |
| **Mesh communication** | Every agent can talk to every other (full or partial mesh). | Resilient (no SPOF) but O(n^2) overhead. |
| **Blackboard / shared state** | Agents read/write shared knowledge store, react to state changes. | Knowledge-intensive tasks. Research, analysis. |

Scaling reality: decentralized MAS shows +9.2% on open-world exploration (BrowseComp-Plus). Error amplification 7.8x. Works when problem space is unknown upfront.

### Decision Framework (When to Use What)

| Property | Favors Orchestration | Favors Choreography |
|----------|---------------------|---------------------|
| **Decomposability** | Sequential, dependent steps | Independent, parallel sub-tasks |
| **Predictability** | Known paths, enumerable outcomes | Open-ended exploration |
| **Consistency need** | Strong (financial, compliance) | Eventual is OK |
| **Failure handling** | Must rollback cleanly | Can tolerate partial failure |
| **Scale** | <10 agents | 10-100+ agents |
| **Domain structure** | Structured (finance, ops) | Unstructured (research, browsing) |

Quantitative boundaries (from research, R^2 = 0.513, 87% accuracy on held-out configs):

| Task Type | Recommended Architecture |
|-----------|-------------------------|
| Planning tasks (~57% baseline, 4 tools) | SINGLE AGENT (multi-agent = -39% to -70% degradation) |
| Analysis tasks (~35% baseline, 5 tools) | CENTRALIZED (+80.9% improvement) |
| Tool-heavy tasks (~63% baseline, 16 tools) | DECENTRALIZED (parallel exploration wins) |
| Baseline > 45% accuracy | SINGLE AGENT (adding agents = negative returns) |

### The Hard Problems This Repo Must Demonstrate

**1. Saga / Rollback**: When step N fails, compensate steps N-1...1 in reverse. Classical sagas assume deterministic compensating actions — but agent outputs are non-deterministic and may have irreversible side effects (sent emails, API calls). SagaLLM approach: three-dimensional state (application + operation + dependency), external constraint tracking, global validation agent.

**2. Observability / Tracing**: Four-layer stack required:
- Layer 1: Infrastructure metrics (CPU, memory, latency)
- Layer 2: System-level observability (syscalls, process trees)
- Layer 3: Agent-level tracing (decisions, tool calls, handoffs)
- Layer 4: Business metrics (task completion, accuracy, cost)

**3. Error Propagation**: Agent A generates slightly wrong data -> Agent B compounds it -> Agent C produces confidently wrong result. Mitigation: independent validation at each agent boundary. Fail fast.

**4. Lineage Reconstruction**: In choreography, "the chain" is implicit. Requires distributed trace context propagation (W3C Trace Context / OpenTelemetry), causal ordering, event store with replay capability.

**5. State Consistency**: Choose per use case — strong consistency (orchestration, financial), eventual consistency (choreography, research), CRDTs (blackboard concurrent writes), event sourcing (audit trail + replay), immutable snapshots (debugging + rollback).

### Anti-Patterns (What NOT to Do)

| Anti-Pattern | Why |
|-------------|-----|
| Independent MAS (no communication) | 17.2x error amplification. Worst architecture. |
| Multi-agent for sequential reasoning | Every MAS variant degraded performance on strict sequential tasks. |
| More agents without task analysis | "More agents is all you need" is demonstrably false. |
| Choreography without distributed tracing | You cannot debug production issues. |
| Orchestration for open-world exploration | Orchestrator can't predict what agents will discover. |

### Communication Protocols (2025-26)

| Protocol | Purpose | Status |
|----------|---------|--------|
| **MCP** | Agent <-> Tools/Data (what an agent CAN access) | De facto standard. Anthropic, adopted by OpenAI/Google/MS. |
| **A2A** | Agent <-> Agent (cross-vendor communication) | Google-initiated. 50+ partners. Linux Foundation. |

MCP and A2A are complementary, not competing.

### Infrastructure Building Blocks

| Component | Options | When |
|-----------|---------|------|
| **Messaging** | Kafka (replay, event sourcing), NATS (low-latency), Redis Streams (simple) | Choreography event bus |
| **Event sourcing** | Append-only event log -> derive current state by replay | Lineage, audit, compensation |
| **Circuit breaker** | CLOSED -> OPEN (on failures) -> HALF-OPEN (probe) -> CLOSED | All external calls (LLM APIs, tools) |
| **Dead letter queue** | Capture failed messages for manual review / retry | Choreography failed tasks |
| **Fault tolerance** | Bulkhead (isolate failures), retry with backoff, leader election | All patterns |

### Key References

1. **Towards a Science of Scaling Agent Systems** (2025) — architecture selection boundaries, error amplification data, scaling laws. https://arxiv.org/html/2512.08296v1
2. **SagaLLM** (VLDB 2025) — saga pattern for multi-agent LLM workflows. https://arxiv.org/abs/2503.11951
3. **AgentSight** (2025) — eBPF-based agent observability, semantic gap. https://arxiv.org/html/2508.02736v2
4. **Google A2A Protocol** (2025) — cross-vendor agent interoperability. https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/
5. **Designing Data-Intensive Applications** — Kleppmann (2017) — distributed systems foundations.
6. **Think Distributed Systems** — Dominik Tornow (2025)