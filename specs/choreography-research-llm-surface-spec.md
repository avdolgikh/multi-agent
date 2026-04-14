# Spec: Choreography Research — Surface LLM Summaries (CHOREO-1)

## Goal

In `src/choreography/research/agents.py`, the three non-web search agents
(`AcademicSearchAgent`, `CodeAnalysisAgent`, `NewsSearchAgent`) already call
the LLM via `SearchAgent._summarize_entries` — but their
`_build_finding_payload` discards the LLM output whenever the tool-provided
`entry` already has a `summary`/`abstract`/`raw_content` field. The
streamed `FindingDiscovered` events therefore read as templated prose, even
though an LLM was invoked (see findings doc §"Phase 1 — RE-VALIDATION",
which confirms `.llm` spans exist for all agents but the output isn't
surfaced).

This spec makes the three non-web agents surface the LLM summary in the
published `FindingDiscovered` payload, matching the pattern
`WebSearchAgent` already uses (line ≈570):

```python
summary = f"{summary_text} {snippet}".strip()
```

Scope is narrow by design: no new LLM call sites, no prompt changes, no new
agents, no orchestration changes.

## Source Files

The implementation edits/creates these files:

- `src/choreography/research/agents.py` — update
  `AcademicSearchAgent._build_finding_payload`,
  `CodeAnalysisAgent._build_finding_payload`, and
  `NewsSearchAgent._build_finding_payload` so the published `summary`
  field is a blend of `summary_text` (LLM output) and the tool-provided
  field, not a fallback-only relationship.
- `tests/test_choreography_research_llm_surface.py` — **new** task-specific
  test file. This is how the pipeline's test-matcher binds tests to this
  task; DO NOT rely on editing the existing
  `tests/test_choreography_research.py` (its frozen assertions stay
  intact — see §4).

## Requirements

### 1. LLM-first summary blending (three subclasses)

Each non-web `_build_finding_payload` must produce a published `summary`
string that contains the LLM's `summary_text` (full, non-empty). Two
acceptable shapes:

- **Prefix-LLM blend** (matches `WebSearchAgent`):
  ```python
  summary = f"{summary_text} {field_from_entry}".strip()
  ```
- **LLM-only** when the tool entry has no useful body (fallback ordering
  reversed):
  ```python
  summary = summary_text or entry.get("abstract") or "..."
  ```

The implementer may pick per-subclass as long as `summary_text` (when
non-empty) is always present verbatim in the `summary` field of the
resulting payload.

#### 1.1 `AcademicSearchAgent`

Currently:
```python
abstract = entry.get("abstract") or entry.get("raw_content") or summary_text
# ...
"summary": abstract,
```
After: the LLM `summary_text` must appear in `summary`. Keep `raw_content`
as the original tool abstract if present, so the underlying source
material is not lost.

#### 1.2 `CodeAnalysisAgent`

Currently:
```python
summary = entry.get("summary") or summary_text
```
After: ensure `summary_text` appears in the published `summary`. Keep the
repo-level fields (`repository`, `language`) untouched.

#### 1.3 `NewsSearchAgent`

Currently:
```python
summary = entry.get("summary") or summary_text
```
After: ensure `summary_text` appears in the published `summary`. Keep
`published_date`, `url`, `raw_content` fields untouched.

### 2. Fallback behavior preserved

When `_summarize_entries` fails (LLM raises) or returns empty, it falls
back via `_fallback_summary_text` today. That behavior stays: the spec
change is ONLY about how a successful non-empty `summary_text` reaches the
publish payload. Do not remove or restructure the fallback path.

### 3. `WebSearchAgent` unchanged

`WebSearchAgent._build_finding_payload` already blends correctly. Do not
touch it.

### 4. Tests (unit only, mock external per CLAUDE.md)

- All new tests live in `tests/test_choreography_research_llm_surface.py`
  (new file). Do NOT edit the existing
  `tests/test_choreography_research.py`; its frozen shape/behavior checks
  stay green.
- Mock `BaseAgent.call_llm` to return a distinctive string (e.g.,
  `"LLM_MARKER_abc123"`) via an autouse fixture in the new file.
- Write three tests (one per agent class) that:
  1. Construct the agent with a minimal fake search tool returning one
     `dict` entry that has a non-empty `summary`/`abstract`/`raw_content`
     field (the tool payload that used to win).
  2. Drive `_generate_findings` (or the event-publishing flow) for a
     sample topic.
  3. Assert the resulting finding payload's `summary` field **contains
     the mocked LLM marker substring** (`"LLM_MARKER_abc123"`).
- Write a fourth test using `AcademicSearchAgent` where the mocked
  `call_llm` raises; assert the payload's `summary` is still non-empty
  (fallback path), confirming §2.

### 5. Preserve existing contract

- `FindingDiscovered.payload` schema unchanged.
- `ReconstructTimeline` ordering unchanged.
- No new events, no new classes.

## Non-Requirements (explicitly out of scope)

- Changing `_summarize_entries`, `_build_summary_prompt`, or
  `_fallback_summary_text`.
- Adding new LLM call sites in the initiator/aggregator.
- Env-var model override (that's ERG-1).
- Touching `WebSearchAgent` beyond verifying it still passes.

## Acceptance Criteria

1. **LLM marker surfaces in all three non-web agents.** With `call_llm`
   mocked to a known string, the published `summary` of
   `AcademicSearchAgent`, `CodeAnalysisAgent`, and `NewsSearchAgent`
   findings contains that string verbatim.

2. **`WebSearchAgent` still passes its existing tests.**

3. **LLM failure fallback preserved.** When `call_llm` raises for a
   non-web agent, the resulting `summary` is still non-empty
   (`_fallback_summary_text` output).

4. **Existing tests untouched and green.**
   `tests/test_choreography_research.py` is not edited and its 19 tests
   continue to pass.

5. **Full pipeline validation suite green.**
   `ruff check src/ tests/`, `ruff format --check src/ tests/`,
   `pyright src/`, and `pytest` all pass.

6. **Published payload shape unchanged.** `FindingDiscovered.payload`
   still carries exactly the keys it did before (e.g., `authors`, `year`
   for academic; `repository`, `language` for code; `published_date` for
   news). Only the `summary` string content changes.

## Verification Loop (manual, post-pipeline)

Operator will run (not required for pipeline to approve):

```bash
uv run python scripts/run_phoenix.py &
uv run python -m choreography.research "event sourcing vs CQRS tradeoffs"
```

Expected:

- Streamed Academic/Code/News findings contain real LLM prose (no longer
  read as `"Peer-reviewed findings on {topic}, scenario 1."`-style
  templates).
- Phoenix shows the same 5 traces as before, each with an `.llm` child
  span — structure unchanged.
