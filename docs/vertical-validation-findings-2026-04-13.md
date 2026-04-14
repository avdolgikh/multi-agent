# Vertical Validation Findings — 2026-04-13

First live walkthrough of `docs/vertical-validation-runbook.md`. Captured
while iterating; choreography findings added after its run lands.

## What we ran

- **Date / hardware**: 2026-04-13, Windows 11, RTX 4070 12GB
- **Ollama model(s)**: `glm-4.7-flash:latest` (29.9B MoE, Q4_K_M) — configured
  via hardcoded `model=` kwargs in agent wiring (10 call sites edited)
- **Phoenix version**: 13.21.0 (pinned with `arize-phoenix-evals<3.0.0` to
  restore `phoenix.evals.models` import)
- **Multi-agent commit SHA**: TBD — uncommitted working tree includes the
  model swaps, Phoenix script rewrite, evals pin, README/AGENTS/runbook docs

## Orchestration run

- **Input file**: `fixtures/validation/sample_module.py`
- **Elapsed wall time**: **3.51 seconds** (too fast — see Finding 1)
- **Total tokens**: **0** (no LLM calls made)
- **Sample final output**:

  ```json
  {
    "executive_summary": "Analyzed 3 functions and 0 classes with quality score 100.",
    "security_section": { "findings": ["Dynamic code execution detected"] },
    "quality_section": { "score": 100, "issues": [] },
    "recommendations": [ { "title": "Address security findings", "priority": "high",
                           "detail": "Resolve high and critical issues before deployment." } ]
  }
  ```
- **Agent graph shape — expected vs actual**:
  - *Expected*: linear — Parser → Security → Quality → Report, each as an
    `@traced` span with an LLM span inside.
  - *Actual*: only one trace (1.5 ms duration) landed in Phoenix's `default`
    project. No per-agent spans visible; no LLM spans (there were no LLM
    calls to instrument).
- **LLM span completeness**: n/a — no LLM calls.
- **Notable latencies**: whole run ≈ 3.5 s; wall time dominated by Python
  import + AST parse + pattern matching, not model inference.

## Choreography run

- **Topic**: `event sourcing vs CQRS tradeoffs`
- **Elapsed wall time**: **366.46 seconds** (~6 min — genuine LLM inference)
- **Total tokens**: unknown (not reported; see Finding 7)
- **Sample final brief** (excerpt):
  > "Research coverage encompasses news, code implementations, and academic
  > studies detailing the tradeoffs involved in combining Event Sourcing
  > with CQRS. Significant divergence exists among perspectives, with one
  > view validating the pattern for large-scale systems due to performance
  > and separation of concerns, while another highlights the steep
  > operational complexity and maintenance costs…"
- **Sources consulted**: web ×2, academic ×1, code ×1, news ×1
- **Streaming output worked**: findings appeared in real-time with
  timestamps and source tags — nice UX.
- **Agent graph shape — expected vs actual**:
  - *Expected*: event-driven fan-out in Phoenix's Agent Graph tab.
  - *Actual*: **zero traces** in Phoenix from this run (see Finding 6).
- **LLM span completeness**: n/a — no spans exported.
- **Notable latencies**: web-source findings took longest (LLM summarization
  + corroboration analysis). Non-web sources fired near-instantly, suggesting
  they use templated content rather than LLM.

## Gaps & surprises

### Finding 1 (critical): Orchestration agents are 100% deterministic, not LLM-backed

The 4 agents in `src/orchestration/code_analysis/agents.py` — `ParserAgent`,
`SecurityAgent`, `QualityAgent`, `ReportAgent` — all override `execute()`
with pure Python logic and **never call `self.call_llm()`**:

- `ParserAgent`: uses `ast.parse()`.
- `SecurityAgent`: pattern matching over AST nodes.
- `QualityAgent`: heuristic counters.
- `ReportAgent`: string composition from prior step outputs.

The `model=` kwarg is stored on the agent (`self.model = model` in
`BaseAgent.__init__`) but the concrete agents don't read it. So our 4 model
swaps in `__init__.py` had **no effect on orchestration behavior** — they're
cosmetic until an agent actually invokes `call_llm`.

Only `ValidationAgent` (`validation.py:32`) can hit the LLM, and only when
constructed with `use_llm=True`. `_build_default_orchestrator()` does not
pass that flag, so even the validation path is deterministic ("todo" keyword
check at `validation.py:56–57`).

**Impact**: the shipped "orchestration vertical" demonstrates the
*coordination pattern* (sequential pipeline + validation gates + rollback)
but does not demonstrate *LLM-powered reasoning*. The pattern runs; the
intelligence doesn't.

### Finding 2 (critical): Quality output is wrong by construction

Given a file with a deliberate off-by-one bug
(`compute_average_off_by_one`), the deterministic `QualityAgent` returned
`score: 100, issues: []`. No static heuristic in the current implementation
looks for range/index offsets — which is fine, that's what an LLM is for.
But since no LLM is called, the finding is silently missed.

### Finding 3 (critical): Phoenix capture is near-empty

- Only one trace recorded in Phoenix's `default` project.
- Trace duration: **1.5 ms** for a 3.5 s run — 99.96% of execution is
  untraced.
- No custom project for `orchestration-code-analysis` despite
  `init_observability("orchestration-code-analysis")` being called — service
  name routing isn't creating a separate project bucket.
- No visible per-agent spans.

Implication: the `@traced` decorator wiring in `src/core/tracing` or its
application sites isn't emitting what the runbook expected. Something
about the tracer provider + OTLP exporter integration is half-wired.

Stderr warning seen during the run:
```
Overriding of current TracerProvider is not allowed
```
Suggests a second `TracerProvider` setup attempt is colliding with the first
— OTel's set-once global. Known issue (referenced in AGENTS.md Run 7 lessons
as a post-merge bug, fixed via a conftest reset fixture *for tests* but
evidently not for live runs).

### Finding 4 (non-critical): Traceloop SDK complains on startup

```
Error: Missing Traceloop API key, go to https://app.traceloop.com/...
Set the TRACELOOP_API_KEY environment variable to the key
```

`init_observability` initializes Traceloop, which expects a cloud API key.
For local-only Phoenix use, this is noise. Either make Traceloop init
optional when `TRACELOOP_API_KEY` is unset, or suppress the warning.

### Finding 6 (critical): Choreography never calls `init_observability()`

Grep of `src/choreography` for `init_observability` or `TracingManager.setup`
returns **zero matches**. `src/choreography/research/runner.py:main()` builds
the agent graph and runs it, but no OTel configuration happens, so no OTLP
exporter is attached and spans go nowhere.

Result: the 6-minute real-LLM choreography run produced **zero traces** in
Phoenix — the most inspectable, expensive run we have is completely
unobserved.

Fix: mirror what orchestration does — call
`init_observability("choreography-research")` at the top of
`choreography.research.main()`.

### Finding 7 (non-critical): Non-web choreography sources appear templated

Looking at the streaming output, `news`, `code`, and `academic` findings
arrived near-instantly with content that reads templated:

- `"Recent coverage describing event sourcing vs CQRS tradeoffs trend 1."`
- `"Implementation 1 demonstrating event sourcing vs CQRS tradeoffs patterns."`
- `"Peer-reviewed findings on event sourcing vs CQRS tradeoffs, scenario 1."`

The `web` source produced two substantive LLM-generated summaries; the
others look like `f"{adjective} coverage describing {topic} trend {n}"`
templates. Needs source-code confirmation (likely in
`src/choreography/research/agents.py`'s `_build_finding_payload` methods for
each search-agent subclass).

Impact: choreography demonstrates real LLM coordination on `web` findings
only. Other source agents are cosmetic, like orchestration's.

### Finding 5 (minor): No env-var override for model choice

Model name is source-hardcoded in 10 sites across orchestration + choreography.
To swap models the operator must edit source and either commit or stash.
An env override (e.g. `MULTI_AGENT_OLLAMA_MODEL`) read by
`_build_default_orchestrator()` and the choreography runner would let
operators swap without touching source. Minor ergonomic fix.

## Action items for `hybrid-analysis` spec

Before the hybrid spec is written:

1. **Decide**: is the point of these verticals (a) the coordination pattern,
   or (b) the LLM-powered reasoning, or both? The shipped orchestration is
   (a)-only — if (b) is intended, that's a spec-level fix affecting all
   current verticals.
2. **Fix tracing** so the hybrid demo produces a proper trace tree. Without
   it, side-by-side comparison is uninspectable.
3. **Plan model-swap ergonomics** (env var) before wiring a 3rd vertical and
   multiplying hardcoded sites.

## Follow-up tickets (fix plan, prioritized)

### Phase 1 — observability works at all (foundation)

- **[OBS-1]** **Choreography observability wiring.** Call
  `init_observability("choreography-research")` at the top of
  `choreography.research.main()`. Without this, all subsequent OBS fixes
  are invisible on the choreography side.
- **[OBS-2]** **Apply `@traced` decorator.** Zero use-sites in the whole
  repo. Decorate `BaseAgent.execute` subclasses' `execute()` methods (both
  verticals) plus orchestrator step methods and choreography publish/handle
  methods. Without this, no child spans exist to inspect.
- **[OBS-3]** **Force span flush on exit.** Register an `atexit` hook in
  `TracingManager.setup` that calls `provider.force_flush()` +
  `provider.shutdown()`. `BatchSpanProcessor` holds spans in memory; short
  runs exit before flush, dropping them (explains the orchestration 1.5 ms
  root span).
- **[OBS-4]** **Gate Traceloop init on env.** Skip Traceloop entirely unless
  `TRACELOOP_API_KEY` is set. Silences the "Missing Traceloop API key"
  error and the "Overriding TracerProvider" warning on every local run.
- **[OBS-5]** **Per-service Phoenix project.** Verify that
  `Resource.create({"service.name": ...})` on the `TracerProvider` routes
  spans to a Phoenix project matching the service name. Only "default"
  project exists today — may be a Phoenix config quirk we can fix via
  `OTEL_RESOURCE_ATTRIBUTES` or an explicit project header.

### Phase 2 — orchestration actually uses the LLM

- **[ORCH-1]** Rewrite `ParserAgent` / `SecurityAgent` / `QualityAgent` /
  `ReportAgent` `execute()` methods to call `self.call_llm()` with prompts
  that include AST context (for Parser/Quality/Security) or prior step
  outputs (for Report). Keep AST extraction as a *pre-step* that feeds the
  LLM, not a replacement for it.
- **[CHOREO-1]** Wire `AcademicSearchAgent`, `CodeAnalysisAgent`,
  `NewsSearchAgent` to actually invoke `call_llm` for their finding
  summaries. Currently only `WebSearchAgent` produces real prose;
  others return templated strings (see Finding 7).

### Phase 1 — RE-VALIDATION on 2026-04-13 (post-fix)

Second live walkthrough after applying OBS-1..5.

**Orchestration re-run** (`uv run python -m orchestration.code_analysis
fixtures/validation/sample_module.py`):
- Wall time: ~2.7 s (still deterministic, no LLM — Phase 2 pending).
- Phoenix trace: **9 spans per run**, all nested under one root:
  `code_analysis.pipeline` (root) → 4× step spans (`PARSING.execute`,
  `SCANNING.execute`, `CHECKING.execute`, `REPORTING.execute`) → 4× agent
  spans (`ParserAgent.execute`, `SecurityAgent.execute`,
  `QualityAgent.execute`, `ReportAgent.execute`).
- Project: `orchestration-code-analysis` (separate from `default`).
- "Overriding TracerProvider" warning: **gone**. Root cause was a double
  `set_tracer_provider` call in `orchestrator.py:109` after
  `init_observability` already set one; fixed via `get_tracer_provider()`
  identity check.

**Choreography re-run** (`uv run python -m choreography.research
"observability tradeoffs"`, deadline 5 min):
- Wall time: **549 s** (~9 min) — real LLM inference.
- Phoenix trace: 5 separate traces, each with `.execute` + nested `.llm`:
  - `Aggregator Agent.llm` — 61 s
  - `News Scanner Agent.llm` — 54 s
  - `Code Analysis Agent.llm` — 83 s
  - `Academic Scholar Agent.llm` — 319 s (5 min!)
  - `Web Search Agent.llm` — 485 s (8 min!)
- Project: routed correctly (verified by resource attribute wiring).

**Finding 7 was wrong.** Non-web agents DO call the LLM — the `.llm`
spans prove it. Their summaries only *look* templated because the
template-like prefix (e.g. `"Peer-reviewed findings on {topic}"`) is
what `_build_finding_payload` publishes, while the LLM output is
discarded or not surfaced in the stream. Clarification for future work:
the bug is not "no LLM" but "LLM output not used" — a narrower, cheaper
fix for CHOREO-1.

**Finding 3 resolved.** The 1.5 ms root-span-only orchestration trace is
gone. Full tree now visible.

### Phase 3 — ergonomics + re-validation

- **[ERG-1]** Add `MULTI_AGENT_OLLAMA_MODEL` env override read by
  `_build_default_orchestrator` and the choreography runner, so operators
  don't edit source to swap models.
- **[REV-1]** Re-run the runbook after Phase 1+2, update this findings
  doc, confirm: orchestration takes real wall time, quality agent flags the
  off-by-one bug, Phoenix shows full trace trees with LLM spans, per-agent
  latencies visible, tokens counted.
