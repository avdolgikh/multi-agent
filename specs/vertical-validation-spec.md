# Spec: Vertical Validation — Scaffolding

DRAFT

## Goal

Build the **scaffolding** needed to run the two shipped verticals
(`orchestration-code-analysis`, `choreography-research`) end-to-end against
real local Ollama + Phoenix: demo fixtures and a CLI runner. The actual
*execution and observation* is a separate manual step — see
`docs/vertical-validation-runbook.md`.

This spec is pipeline-implementable: all acceptance is pytest-verifiable, all
external calls (Ollama, Phoenix HTTP) are mocked per project convention.

## Source Files

- `scripts/validate_vertical.py` — CLI runner with `orchestration` and
  `choreography` subcommands; dispatches to existing entry points.
- `fixtures/validation/sample_module.py` — demo Python file for
  `orchestration-code-analysis` to analyze.
- `fixtures/validation/research_topics.txt` — demo research topics for
  `choreography-research`.

## Requirements

### 1. `fixtures/validation/sample_module.py`

A small, realistic Python module (~40–80 LOC) the orchestration vertical can
analyze. Must contain, each as a top-level function with a docstring:

- **Exactly one function that uses `eval()`** (security smell the scanner
  should flag). Name it `evaluate_expression` for test-matching.
- **Exactly one function with an off-by-one bug** in a range/index operation.
  Name it `compute_average_off_by_one`.
- **Exactly one clean, well-written function** with no issues. Name it
  `clean_sum`.

Module must parse as valid Python (verified via `ast.parse`). No imports
beyond stdlib. No runtime side effects on import.

### 2. `fixtures/validation/research_topics.txt`

Plain text file, one topic per line, no comments, no blank lines. **Between 3
and 5 topics**, each a non-empty string of 10–200 characters. Topics should
be narrow enough that choreography research finishes quickly (e.g. "event
sourcing vs CQRS tradeoffs").

### 3. `scripts/validate_vertical.py`

CLI built with `argparse` (stdlib only; no new deps). Invocation shapes:

```bash
uv run python scripts/validate_vertical.py orchestration --input <path> [--dry-run]
uv run python scripts/validate_vertical.py choreography --topic <str> [--dry-run]
```

Behavior:

- `orchestration` subcommand: requires `--input <path>`; path must exist
  (error with non-zero exit if not). On run, imports
  `orchestration.code_analysis` and invokes its `main()` with the input path.
- `choreography` subcommand: requires `--topic <str>` (non-empty). On run,
  imports `choreography.research` and invokes its `main()` with the topic.
- `--dry-run` flag (both subcommands): prints a one-line summary of what
  *would* run (subcommand + args) and exits 0 **without importing the target
  module and without any network or subprocess call**.
- On normal completion: prints wall-clock elapsed seconds and exit status.
- On exception inside the target entry point: prints the exception type +
  message, exits non-zero.

The script must not call `init_observability()` itself — the entry points
already do. It must not start Phoenix. Phoenix lifecycle is manual (runbook).

### 4. Integration with existing entry points

The existing `main()` functions in `src/orchestration/code_analysis/__init__.py`
and `src/choreography/research/__init__.py` must accept the relevant input
(file path or topic string) as a parameter or via an already-documented
argument convention. If they don't today, add the minimum-viable parameter
without changing their behavior. No refactor beyond that.

## Non-Requirements (out of scope)

- No changes to agent logic, prompts, or core infrastructure.
- No new tests that hit real Ollama or real Phoenix — all external calls
  mocked.
- No observability wiring changes (Phoenix endpoint, OTel config).
- No automation of the manual run itself. The *runbook* lives in
  `docs/vertical-validation-runbook.md` and is executed by a human.
- No evaluation / scoring of agent output quality in code. That's a future
  spec.

## Acceptance Criteria

1. Fixtures exist at the specified paths with the specified structure.
2. `scripts/validate_vertical.py --help` works and documents both
   subcommands.
3. `--dry-run` mode on each subcommand makes no network or subprocess calls
   (enforced in tests by patching the relevant entry-point imports +
   asserting they were never called).
4. Normal mode on each subcommand correctly dispatches to the existing
   entry-point `main()` with the parsed arguments (verified via mock; the
   real entry point is NOT called in tests).
5. Missing/invalid arguments produce non-zero exit + helpful error message.
6. Full test suite still passes (pre-existing + new).

## Testing

All unit tests, all external calls mocked per AGENTS.md Conventions. Add to
`tests/test_vertical_validation.py`:

- **Fixture tests**:
  - `sample_module.py` parses as valid Python (`ast.parse`) and contains the
    three named functions; `evaluate_expression`'s source contains `eval(`;
    `clean_sum`'s source contains none of: `eval`, `exec`, `os.system`.
  - `research_topics.txt` has 3–5 non-empty lines, each within the length
    bounds.
- **CLI tests** (use `subprocess`? No — import the script's `main()` and call
  it, or use `runpy`. Preferred: refactor the script to expose a `main(argv)`
  function and call it directly):
  - `--help` prints both subcommands.
  - `orchestration --input <nonexistent>` exits non-zero.
  - `orchestration --input <tmp file> --dry-run` exits 0 and does NOT import
    `orchestration.code_analysis` (patch `importlib.import_module` or the
    specific import; assert not called).
  - `choreography --topic "" --dry-run` exits non-zero (empty topic).
  - `choreography --topic "x" --dry-run` exits 0 without calling the entry
    point.
  - Normal mode for each subcommand: patch the entry-point `main()` to a
    `MagicMock`, invoke the script, assert the mock was called with expected
    args.
  - Exception inside entry point: patched `main()` raises, script exits
    non-zero, message printed.

No test may start Phoenix, reach Ollama, or make real network calls.
