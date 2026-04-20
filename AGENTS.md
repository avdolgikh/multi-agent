# AGENTS.md — Context for AI Agents

## Project Overview

This repository demonstrates **orchestration vs choreography** in multi-agent AI systems.
It is a working system with real LLM-backed agents, not a framework or mock demo.

## Architecture

```
src/
├── core/                    # Shared infrastructure (agents, messaging, tracing, state, resilience)
│   ├── agents/              # Base agent abstractions
│   ├── messaging/           # Event bus (Redis Streams pub/sub, request/reply)
│   ├── tracing/             # OpenTelemetry distributed tracing
│   ├── state/               # Event store + immutable snapshots
│   └── resilience/          # Circuit breakers, retries, dead letter queue
│
├── orchestration/           # Orchestration pattern use cases
│   └── code_analysis/       # Sequential pipeline: parse → scan → check → report
│
├── choreography/            # Choreography pattern use cases
│   └── research/            # Event-driven multi-source research aggregation
│
├── hybrid/                  # Hybrid pattern use cases
│   └── project_analysis/    # Orchestrated teams + choreographed intra-team work
│
└── comparison/              # Same task, both patterns side-by-side
    └── code_review/         # Code review in orchestrated vs choreographed mode
```

## Tech Stack

- Python 3.11+, uv for package management
- LLM APIs: Ollama (local, primary) + OpenAI (cloud, fallback). Both use OpenAI SDK.
- Messaging: In-memory asyncio-based event bus (no external dependencies)
- Tracing: OpenTelemetry
- State: In-memory event sourcing + immutable snapshots (no external DB)
- No heavy frameworks — patterns built from asyncio + in-memory bus + OpenAI SDK

## Conventions

- All infrastructure lives in `src/core/` and is imported by use cases
- Each module has `__init__.py` re-exporting its public API
- Use `asyncio` throughout — agents are async
- Use `pydantic` for message schemas and configuration
- **UNIT TESTS ONLY — mock ALL external calls.** Tests verify logic/functionality, never real integrations. This includes LLM APIs (OpenAI/Ollama), HTTP (OTLP/Phoenix/Traceloop span export), Redis, databases, and any subprocess to external services. `tests/conftest.py` provides autouse fixtures that stub Traceloop + OTLP HTTP exporters; any new test must preserve these stubs or add its own. No test may make a network call.
- No external services needed (no Redis, no cloud APIs in tests)
- Each use case is runnable via `uv run python -m <module>`

## Environment Variables

- `OPENAI_API_KEY` — OpenAI API key (fallback only — prefer Ollama)
- `OLLAMA_BASE_URL` — Ollama API URL (default: `http://localhost:11434/v1`)
- `OTEL_EXPORTER_OTLP_ENDPOINT` — OpenTelemetry collector endpoint (optional)
- `PHOENIX_HOST` / `PHOENIX_PORT` — bind address for `scripts/run_phoenix.py`
  (defaults `127.0.0.1` / `6006`; gRPC ingestion on `4317` is fixed upstream).
  Phoenix's own CLI does **not** take `--host` / `--port` — use env vars.
- `PHOENIX_URL` — base URL used by `scripts/phoenix_trace.py` (default
  `http://localhost:6006`). The script pulls a trace via Phoenix's GraphQL
  endpoint (`/graphql`) so we can inspect span trees from the terminal
  without opening the browser.

### Inspecting a Phoenix trace from the terminal

```bash
# list projects
uv run python scripts/phoenix_trace.py --list-projects <anyid>

# fetch a specific trace (auto-discovers the project)
PYTHONIOENCODING=utf-8 uv run python scripts/phoenix_trace.py <TRACE_ID>

# dump raw span JSON
uv run python scripts/phoenix_trace.py <TRACE_ID> --raw
```

Output: span tree with latencies, plus one line per LLM span
(prompt/completion tokens, status, output preview). Trace IDs are the 32-hex
ID in the Phoenix URL. A healthy `code_analysis.pipeline` run is **13 spans**:
1 root + 4 `STAGE.execute` + 4 `Agent.execute` + 4 `Agent.llm`.

## Dependency Pins (non-obvious)

- `arize-phoenix-evals<3.0.0` — `arize-phoenix-evals==3.0.0` drops
  `phoenix.evals.models`, which `arize-phoenix==13.21` still imports. Pinning
  <3.0.0 is required until Phoenix upstream catches up. Remove the pin only
  after verifying `uv run python scripts/run_phoenix.py` still starts.

## Current Constraints

- **Local-first**: Prefer local Ollama models over cloud APIs. The user has a local GPU.
  OpenAI API key exists as fallback but avoid spending money — use cheap models if needed.
- **No external services in tests**: Tests use `InMemoryBus` and `InMemoryEventStore`.
  No Redis, no real API calls. All test LLM interactions must be mocked.
- **Ollama as primary LLM provider**: Agents should support Ollama via its OpenAI-compatible
  API (http://localhost:11434/v1). This is the preferred runtime provider.
  Available local models (RTX 4070 12GB):
  - `qwen3-coder:latest` — 30.5B MoE, Q4_K_M (best for code tasks)
  - `gemma4:e4b` — 8B, Q4_K_M
  - `glm-4.7-flash:latest` — 29.9B MoE, Q4_K_M
  - `qwen3.5:latest` — 9.7B, Q4_K_M
- **OpenAI as fallback**: Available but costly. Use only when Ollama models can't handle the task.
  When using OpenAI, pick the cheapest suitable model (gpt-4o-mini).

---

## Pipeline-Driven Development

This project is built spec-by-spec using the **spec-driven-dev-pipeline** (TDD pipeline).
Pipeline tool: `D:\dev\avdolgikh_github_repos\spec-driven-dev-pipeline`

### Spec Execution Order (dependency chain)

| # | Task ID | Spec | Status |
|---|---------|------|--------|
| 1 | `core-infrastructure` | Shared infrastructure (agents, messaging, tracing, state, resilience) | DONE |
| 2 | `orchestration-code-analysis` + `-llm` | Orchestrated code analysis pipeline with LLM-backed agents | DONE |
| 3 | `choreography-research` + `-llm-surface` | Event-driven research with surfaced LLM summaries | DONE |
| 4 | `observability-phase1` | Phoenix + OpenLLMetry wiring | DONE |
| 5 | `vertical-validation` | Scaffolding (fixtures + CLI runner) for live Ollama + Phoenix runbook | DONE |


> **`vertical-validation` is split**: the *spec* is pipeline-implementable scaffolding — fixtures + CLI runner, all externals mocked. The *runbook* (`docs/vertical-validation-runbook.md`) is manual — human runs Ollama + Phoenix, inspects traces.

### Milestone 2: Hybrid Analysis (dependency chain)

Master spec: `specs/hybrid-analysis-spec.md`

| # | Task ID | Spec | Status |
|---|---------|------|--------|
| 6a | `hybrid-foundation-team` | Team class + stub agents (single collaboration unit) | DONE |
| 6b | `hybrid-foundation-phase-validator` | PhaseValidator + validation-outcome + project-report models (split from former `hybrid-foundation-pipeline`) | DONE — shipped 2026-04-20 post-split, approved iter 3 under default cap=4, 94 tests pass. |
| 6c | `hybrid-foundation-orchestrator` | ProjectOrchestrator + 3-phase happy path + phase-validated events + snapshots + observability (split from former `hybrid-foundation-pipeline`) | IN PROGRESS — two cap-exits 2026-04-20 (both at iter 6, cap=6). First run: all items were over-pinning public shape. Test-writer prompt patched (helpers forbidden from structural detection). Second run: same pattern persisted — test-writer *must* commit to one constructor shape + one entry-method name to write executable tests against a spec-named class; reviewer kept treating that minimum as over-constraint. Second run was quota-killed during final iter 6 review (resets 21:51 local → 2026-04-20 14:51). Reviewer prompt then patched (minimum-necessary pinning principle). State reset for fresh run under both updated prompts. Next action: re-kick after quota window opens. |
| 6d | `hybrid-foundation-resilience` | Saga rollback + phase-failed event (failure path) | TODO |
| 7 | `hybrid-structure-team` | Discovery: Structure Team — 3 agents, intra-team choreography | TODO |
| 8 | `hybrid-dependencies-team` | Discovery: Dependencies Team — 2 agents, concurrent execution | TODO |
| 9 | `hybrid-deepdive-synthesis` | Deep Dive + Synthesis — full hybrid pipeline end-to-end | TODO |
| 10 | `comparison-orchestrated-review` | Common models + orchestrated code review (4-agent pipeline) | TODO |
| 11 | `comparison-choreographed-review` | Choreographed code review (3 reviewers + aggregator) | TODO |
| 12 | `comparison-harness` | Comparison harness, metrics, CLI entry points | TODO |

> **Former row 6b (`hybrid-foundation-pipeline`) was split 2026-04-20** after cap-exiting at iter 6 with `--max-revisions 6`. Root cause was test-writer over-constraining public names across 4 REQs + AC-8 despite cumulative feedback (not oscillation — each iteration surfaced fresh genuine gaps). The original spec (`specs/hybrid-foundation-pipeline-spec.md`) is marked Superseded; its content is retained for history. The validator surface (former REQ-4 + a piece of REQ-1) and the orchestrator surface (REQ-2/3 + AC-8) became independent slices so each test-writer run has a smaller public surface to over-pin.

### Pipeline Run Commands
```bash
cd D:/dev/avdolgikh_github_repos/spec-driven-dev-pipeline

# Primary: Codex provider (with full validation suite — ruff + format + pyright + pytest)
uv run python scripts/run_pipeline.py <task-id> --provider codex --repo-root D:/dev/avdolgikh_github_repos/multi-agent --config D:/dev/avdolgikh_github_repos/multi-agent/pipeline-config.toml

# For broader-scope slices (hybrid/comparison): bump revision cap from default 4 to 6.
# Reviewer is thorough; some specs need more revision rounds even when feedback is non-recurring.
uv run python scripts/run_pipeline.py <task-id> --provider codex --max-revisions 6 --repo-root D:/dev/avdolgikh_github_repos/multi-agent --config D:/dev/avdolgikh_github_repos/multi-agent/pipeline-config.toml

# Secondary: Gemini provider
uv run python scripts/run_pipeline.py <task-id> --provider gemini --repo-root D:/dev/avdolgikh_github_repos/multi-agent --config D:/dev/avdolgikh_github_repos/multi-agent/pipeline-config.toml
```

### Development Roles
- **Claude**: Orchestrate only — write specs, run pipelines, monitor, fix issues, document.
  Do NOT implement code directly. Save tokens for vital orchestration work.
- **Codex / Gemini**: Generate tests and implementation code via the pipeline.
- **Ollama local models**: Runtime LLM provider for the multi-agent system's agents.

### Live Demo Runners (per-milestone)

After a milestone's slices land, Claude is pre-authorized to write a small ad-hoc driver script under `scripts/run_<milestone>_demo.py` that exercises the new code end-to-end with stubs (no LLM calls), wired to `init_observability(<service-name>)` for Phoenix tracing. Demo scripts are scratch — do not commit unless the user asks. CLI entry points (`__main__.py`) belong to the spec that owns them.

**Hybrid demo (after slices 6a-6c land):** `scripts/run_hybrid_demo.py` — builds 5 stub teams (DISCOVERY x2, DEEP_DIVE x2, SYNTHESIS x1), constructs `ProjectOrchestrator`, runs the happy path, prints workflow id + COMPLETED state + snapshot count = 3. Expected Phoenix span tree: `ProjectOrchestrator.run` → 3 phase spans → team spans (DISCOVERY teams concurrent) → stub `.execute` spans. No LLM spans (stubs). Topology matters more than exact span names — those are implementation choices the spec does not pin.

**Prereq for any demo:** Phoenix running (`uv run python scripts/run_phoenix.py`); see `project_live_run_nuances.md` (personal scratch) for known Windows orphan-port quirks on `:4317`.

### Session Wrap-Up Protocol (Claude)

**AGENTS.md is the canonical, version-controlled long-term memory. Personal memory (`~/.claude/.../memory/`) is local scratch only.** Anything load-bearing for future sessions, future agents, or other humans MUST land in AGENTS.md (or in repo files like specs / docs). Personal memory is appropriate only for ephemeral session state (Monitor task IDs, "we stopped here today", in-flight drafts before they're applied).

When the session is wrapping up, Claude MUST prepare for immediate resume without prompting:
1. Push durable lessons (pipeline behaviors, run commands, design decisions, demo runners) into AGENTS.md.
2. Update the session-resume memory with ephemeral state only: pipeline subprocess status, next 2-5 granular steps, Monitor/TaskList handles.
3. Persist in-conversation drafts (specs, prompt edits, AGENTS.md diffs) as separate memory files so they survive context compaction — but treat them as a staging area, not the destination.
4. List queued edits blocked by hash_targets or other locks so the next session resolves them first.
5. Do not ask permission; this is pre-authorized.

### Lessons Learned (load-bearing rules for future specs)

- **Branch-before-merge spec work risks divergence.** Pipelines cut before a prior spec merges produce rebase conflicts on the same files. Sequence specs, or rebase onto master before Stage 4.
- **Pipeline tests don't catch post-merge integration bugs.** The pipeline validates tests-in-worktree. Dual-import + OTel set-once issues only surface after merge. Plan: trial-merged validation (future pipeline work).
- **Unit-test isolation is load-bearing.** Autouse fixtures stub external I/O (Traceloop, OTLP HTTP) and reset OTel globals — codified in `tests/conftest.py` and the Conventions section. Any new test must preserve or re-establish these stubs. No test makes a network call.
- **Stage 1 NO_EFFECT recovery.** When a task's tests already exist in the repo (e.g. added manually or from a prior session), Stage 1 exits code 10. Recovery: compute current tests hash, seed `.pipeline-state/<task>.json` with `stage=TESTS_FROZEN` + that hash, then re-run — the pipeline resumes at Stage 3 (Implementation).
- **Specs need a `## Source Files` section** listing the `.py` filenames in backticks. The pipeline extracts `test_<stem>` terms from those to match test files to tasks.
- **Specs are high-level intent, NOT pseudo-code.** Target ~150 lines. Keep Goal/Scope/REQ prose/ACs/Package Layout. Do NOT pin exact class/method signatures, attribute names, span names, event topic strings, enum values, or per-test assertions. Leave room for the test-writer and implementer to design the internal shape. An over-specified spec (1) makes the human do the agents' work, (2) creates ambiguity vectors at every pinned string (e.g. `Phase.DISCOVERY` read as literal-string by one agent and as enum-reference by another), (3) masks pipeline issues that don't actually exist with a slim spec. Evidence: M1 shipped 4 specs with the minimal TEMPLATE.md; hybrid-foundation-v1 was 580 lines and burned 3 failed pipeline runs before being slimmed.
- **Spec backticks must use real test-file names; pipeline matches by basename.** When a spec lists `tests/test_foo.py` in backticks under `## Source Files`, the pipeline derives matching terms from the basename (`test_foo`). The pipeline's `_build_task_test_terms` was patched 2026-04-20 to also accept `Path(name).stem` when a backticked `.py` already starts with `test_` — without that patch, Stage 1 false-fails with "No task-specific test files exist" even after Codex writes the file correctly.
- **Reviewer cap calibration: distinguish oscillation from thorough discovery.** The default `max_revisions=4` is calibrated for narrow-scope slices (3-5 ACs). Broader slices (5+ REQs / 8+ ACs) often need `--max-revisions 6`. Diagnostic: if the reviewer keeps re-flagging the SAME items after revisions, that's oscillation → split the spec (`feedback_split_when_oscillating.md`). If each iteration surfaces FRESH substantive gaps without recurrence, the test surface is just larger than 4 rounds — bump the cap, do not split.
- **Revision stages need cumulative blocking feedback.** The pipeline's Stage 2b and Stage 5b were patched 2026-04-20 to pass a "Previously raised in earlier iterations (ensure your revision still addresses these; do not weaken or drop assertions added to satisfy them)" section alongside the current iteration's blocking list. Without it, the test-writer/implementer addresses the latest flags but silently drops earlier tightening, causing reviewer to re-raise old issues — false oscillation. Both pipeline patches are uncommitted in `spec-driven-dev-pipeline/src/spec_driven_dev_pipeline/core.py` as of 2026-04-20.
- **Test-writer tends to over-constrain public shape.** Observed across 6b iterations 3-5: tests pin exact attribute names (`analysis_state`), substring-match class names (`"phase"`/`"valid"` in a payload class name), require topic constants to be UPPERCASE strings, or filter span descendants by exact names (`"run"`, `"execute"`). The spec deliberately leaves these to the implementer — see Spec Philosophy bullet above. The reviewer catches this and flags "over-specifies REQ-N beyond spec". When writing specs, do NOT include example attribute/class/topic/span names even in prose; if a name is load-bearing it must be a REQ, otherwise omit it. When drafting reviewer/test-writer prompts, reinforce "tests assert structural/behavioral properties, not internal naming".
- **Codex usage quota can kill a run mid-revision.** `FAIL: Codex provider execution failed for role test-writer (exit 1)` with stderr `You've hit your usage limit ... try again at <HH:MM>` is a quota, not a code, failure. The pipeline preserves state (`.pipeline-state/<task>.json` stays at the pre-call stage/iter), and a simple re-kick with the same command resumes from the same iteration once the quota window reopens. Do NOT delete state, bump max-revisions, or edit tests in response — it's a transient external failure. If this recurs, consider running during off-peak windows or switching providers for that slice.
- **Split when the surface (not the specificity) is the problem.** Distinct from oscillation: if the reviewer keeps finding FRESH genuine over-constraint items at DIFFERENT REQs across iterations (not re-flagging the same items), bumping the cap doesn't help — the test-writer simply has too many public surfaces to reach for names on, and each cycle exposes another. Observed on `hybrid-foundation-pipeline` (former 6b): 6 iters, fresh items at REQ-1/2/3/4 + AC-8 each round. Fix: vertical split to shrink per-slice public surface. Don't tighten spec ACs to forbid name-pinning (see Spec Philosophy bullet); the fix is fewer public things per slice, not more rules per thing.
- **Minimum-necessary pinning is the testability floor.** When a REQ names a public class but does not name its constructor signature or entry-method, tests still have to commit to ONE constructor shape and ONE method call to construct the scenario. That commitment is not over-constraint — it is the baseline cost of writing an executable test against a spec-named class. Observed on slice 6c (hybrid-foundation-orchestrator) 2026-04-20: 7 reviewer iterations, each flagging the test-writer for "pinning constructor/run() signature" even though the tests literally have no other option. Patching the test-writer prompt to forbid `inspect.signature` / enumerated-kwarg helpers did not resolve it — the test-writer has to commit to something, and enumeration had been the one structural escape hatch. **Fix belongs in the reviewer prompt**: the reviewer must treat "one committed constructor shape + one committed entry-method name" as acceptable pinning, and treat as blockers only what goes beyond it (enumerated alternatives, substring-scanning for class names, mandated extra public surface like a "transition callable"). When diagnosing a stuck cycle, ask: could a compliant implementation exist that passes the test AS WRITTEN, just by reading it and matching its constructor/method expectations? If yes, it's minimum-necessary pinning — approve. If the test requires multiple alternative shapes or extra public APIs, it's over-constraint — block.

---

### How to Resume a Pipeline

```bash
# Pipeline state is saved to .pipeline-state/. Just re-run:
cd D:/dev/avdolgikh_github_repos/spec-driven-dev-pipeline
uv run python scripts/run_pipeline.py <task-id> --provider codex --repo-root D:/dev/avdolgikh_github_repos/multi-agent --config D:/dev/avdolgikh_github_repos/multi-agent/pipeline-config.toml

# To start a spec fresh, delete state + the test artifact (avoids Stage 1 conflict):
rm -f D:/dev/avdolgikh_github_repos/multi-agent/.pipeline-state/<task-id>.json
rm -f D:/dev/avdolgikh_github_repos/multi-agent/tests/test_<spec-stem>.py

# After cap-exit, do NOT patch tests/state to fake an approval. Either:
#  - re-kick with --max-revisions 6 (if reviewer is finding fresh gaps, not regressions)
#  - split the spec into smaller slices (if reviewer is re-flagging the same items)
#  - fix pipeline prompts/loop (if the failure mode is structural)
# See `feedback_no_pipeline_shortcuts.md` and `feedback_split_when_oscillating.md`.
```

