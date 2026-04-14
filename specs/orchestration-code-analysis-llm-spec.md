# Spec: Orchestration Code Analysis — LLM-Powered Agents (ORCH-1)

## Goal

Rewrite the four agents in `src/orchestration/code_analysis/agents.py`
(`ParserAgent`, `SecurityAgent`, `QualityAgent`, `ReportAgent`) so their
`execute()` methods actually call `self.call_llm(...)` instead of returning
deterministic AST/pattern-matched output.

This addresses Finding 1 and Finding 2 from
`docs/vertical-validation-findings-2026-04-13.md`: today the orchestration
vertical demonstrates the *coordination pattern* (pipeline + validation +
saga) but not *LLM-powered reasoning*. The `model=` kwargs on agents are
cosmetic because the concrete agents never invoke the LLM. The off-by-one bug
in `fixtures/validation/sample_module.py::compute_average_off_by_one` is
silently missed because no semantic reasoning runs.

The return-type contracts (`ParseResult`, `SecurityResult`, `QualityResult`,
`AnalysisReport` in `src/orchestration/code_analysis/models.py`) MUST NOT
change. The orchestrator, validator, saga, and existing integration tests
stay intact.

## Source Files

The implementation edits/creates these files:

- `src/orchestration/code_analysis/agents.py` — rewrite `ParserAgent`,
  `SecurityAgent`, `QualityAgent`, `ReportAgent`
- `tests/test_orchestration_code_analysis_llm.py` — **new** task-specific
  test file containing all tests introduced by this spec. This file MUST
  be created; it is how the pipeline's test-matcher binds tests to this
  task.
- `tests/test_orchestration_code_analysis.py` — existing file; may be
  minimally edited ONLY to install a shared `call_llm` mock fixture or to
  relax deterministic-output assertions that no longer hold after the
  rewrite. Do not add new test cases here; put new cases in the file
  above.
- `tests/conftest.py` — may be extended with an autouse fixture mocking
  `BaseAgent.call_llm` for `src.orchestration.code_analysis` tests.

## Requirements

### 1. AST extraction becomes a pre-step, not a replacement

Each agent that currently walks the AST keeps doing so — but the extraction
is the *context* fed into the LLM prompt, not the final answer. Concretely:

- `ParserAgent.execute` still parses source with `ast.parse` to collect
  function/class/import descriptors. It then asks the LLM to **confirm,
  correct, or enrich** that structural view (e.g., infer likely return type
  from body when annotation is missing). The LLM response is merged into
  `ParseResult`.
- `SecurityAgent.execute` still reads source. AST/regex heuristics build a
  shortlist of *candidate* locations (eval/exec, os.system/subprocess,
  password/secret literals, SQL string concatenation). The LLM takes the
  full source plus candidate list and produces the final `SecurityResult`
  findings with severity, location, description, recommendation.
- `QualityAgent.execute` still parses source and computes cyclomatic
  complexity heuristics. It then asks the LLM to review the source for
  quality issues **including logic bugs** (off-by-one, incorrect boundary
  conditions, unreachable code) — not just style. Heuristic metrics go into
  `QualityResult.metrics`; LLM-identified issues go into
  `QualityResult.issues`. The LLM also produces the final `score`.
- `ReportAgent.execute` consumes the three prior results plus the input
  path. It asks the LLM to synthesize an `AnalysisReport` whose
  `executive_summary` is a natural-language paragraph grounded in the
  structured findings, and whose `recommendations` are prioritized with
  reasoning.

### 2. Prompt contract

Each agent:
- Reads its `system_prompt` from `BaseAgent` (already set in
  `_build_default_orchestrator`) and passes it through `call_llm`'s system
  channel. Do not hardcode a new system prompt inside `agents.py`.
- Builds **one user message** containing: (a) a description of what the
  agent is expected to produce, (b) an explicit JSON schema for the
  expected response (derived from the Pydantic model), (c) the AST-extracted
  context, (d) the relevant source code (truncated per file to a sensible
  cap — 8000 chars default).
- Instructs the LLM to respond with **JSON only**, no prose, no code fences.

### 3. LLM response parsing

A shared helper (private to `agents.py`) parses the LLM response:

1. Strips optional code fences (```json ... ```).
2. Parses JSON.
3. Validates against the target Pydantic model with `model_validate`.
4. On failure (non-JSON, schema mismatch, empty), raises a typed error that
   the orchestrator will treat as a step failure → saga compensation runs
   in reverse per existing saga semantics. Do not silently fall back to the
   deterministic output; that would mask the very thing we're trying to
   demonstrate.

### 4. Tracing

`self.call_llm(...)` already opens a `{agent.name}.llm` child span (see
`src/core/agents/__init__.py:123`). Do not add additional span plumbing.
The `@traced` decorator on each `execute()` remains so the LLM span nests
under `{AgentClass}.execute`.

### 5. Preserve existing public interface

- `AgentResult.output_data["result"]` still holds the Pydantic model
  instance (not a dict).
- `ParserAgent` still reads files via `_gather_sources`.
- `StepValidator` assertions still pass (Parser produces ≥1 function/class,
  Security findings have valid severity + non-empty description, Quality
  score is 0–100).

### 6. Tests (unit only, mock external per CLAUDE.md)

- All new tests live in **`tests/test_orchestration_code_analysis_llm.py`**
  (a new file). The existing `tests/test_orchestration_code_analysis.py`
  is NOT modified — its frozen deterministic-shape assertions still hold
  after the rewrite (with `call_llm` mocked to return valid JSON via the
  autouse fixture; see below).
- All new tests mock `BaseAgent.call_llm` — no real LLM/HTTP calls. Follow
  the autouse fixture conventions in `tests/conftest.py`.
- The new test file must include:
  - An autouse fixture that patches `BaseAgent.call_llm` so that, by
    default, it returns a schema-valid JSON string for whichever agent
    called it (detect via the calling agent's `name` or by inspecting the
    outermost Pydantic schema the caller expects). Individual tests can
    override this fixture's return value via a helper.
  - One test per agent that injects a specific mocked `call_llm` response
    and asserts the parsed output matches verbatim (after Pydantic
    validation).
  - For each agent: assert `call_llm` was awaited exactly once with a
    `messages` argument whose system content equals the agent's
    `system_prompt`, and whose user content contains both the AST context
    marker (e.g., "AST context:") and at least part of the source.
  - A test confirming that malformed LLM output (e.g., `"not json"`)
    causes `execute()` to raise and that the orchestrator enters
    `ROLLING_BACK` via the existing saga test harness (import the harness
    from `test_orchestration_code_analysis` or re-create it locally).
  - A test confirming that a QualityAgent run over
    `fixtures/validation/sample_module.py` with a mocked LLM response
    listing an off-by-one issue at `compute_average_off_by_one` produces a
    `QualityResult` whose `issues[*].location` includes
    `compute_average_off_by_one` and whose score is less than 100.

### 7. No new modules

Everything fits inside `agents.py` plus a small private helper for
JSON-parse + validate. No new files under `src/orchestration/code_analysis/`.

## Non-Requirements (explicitly out of scope)

- Changing `_build_default_orchestrator` in `__init__.py`.
- Swapping `ValidationAgent`'s `use_llm` default (separate follow-up).
- Env-var model override (`MULTI_AGENT_OLLAMA_MODEL` — that's ERG-1, a
  separate ticket).
- Choreography agents (that's CHOREO-1, post-ORCH-1).

## Acceptance Criteria

1. **Each agent calls `self.call_llm` exactly once per `execute()`**
   (verified by unit test with a mock).

2. **LLM output determines the structured result.** With a mocked LLM
   returning a specific finding set, the agent's `AgentResult` reflects
   those findings verbatim (after Pydantic validation).

3. **Malformed LLM output fails the step.** When `call_llm` returns
   non-JSON, `execute()` raises, the orchestrator transitions to
   `ROLLING_BACK`, and saga compensation logs completed prior steps.

4. **Return-type contract unchanged.** Existing tests in
   `tests/test_orchestration_code_analysis.py` (which this spec does NOT
   edit) continue to pass. Whatever `call_llm` mocking mechanism the new
   test file installs must also keep the existing test file green — either
   by applying the mock autouse at `conftest.py` level, or by ensuring the
   existing tests' assertions about `ParseResult`/`SecurityResult`/
   `QualityResult`/`AnalysisReport` *shape* (not content) still hold when
   the LLM returns schema-valid stub JSON.

5. **Quality agent surfaces off-by-one.** A unit test feeds the quality
   agent a mocked LLM response that flags
   `compute_average_off_by_one` as an off-by-one; the resulting
   `QualityResult.issues[*].location` contains
   `compute_average_off_by_one` and the score is less than 100.

6. **Full pipeline validation suite green.**
   `ruff check src/ tests/`, `ruff format --check src/ tests/`,
   `pyright src/`, and `pytest` all pass.

7. **Tracing unchanged structurally.** Each `*.execute` span has exactly
   one `{agent.name}.llm` child span (existing assertion in the smoke
   test stays green).

## Verification Loop (manual, post-pipeline)

Operator will run (not required for pipeline to approve):

```bash
uv run python scripts/run_phoenix.py &
uv run python -m orchestration.code_analysis fixtures/validation/sample_module.py
```

Expected:

- Wall time in tens of seconds (real LLM inference, glm-4.7-flash:latest).
- Phoenix shows full trace tree with `.llm` child spans under each
  `*.execute`.
- `quality_section.issues` includes a description referencing
  `compute_average_off_by_one`.
- `security_section.findings` includes the `eval(` risk from
  `evaluate_expression`.
- Exit code 0.
