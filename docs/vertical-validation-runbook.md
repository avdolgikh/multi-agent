# Vertical Validation Runbook

Manual end-to-end exercise of the shipped verticals
(`orchestration-code-analysis`, `choreography-research`) against real local
Ollama + Phoenix. Performed after the `vertical-validation` spec's
scaffolding lands.

**Not** pipeline-driven. Produces observations, not code.

## Why

We've built the system spec-by-spec with mocked tests and have never watched
a real run. This runbook closes that gap before we commit to `hybrid-analysis`:

- Does the agent graph in Phoenix match the pattern (orchestration vs
  choreography)?
- Are LLM spans complete (prompt, completion, token counts)?
- Latency / cost hotspots?
- Does prompt quality produce useful output, or garbage?
- Any gaps in trace propagation across the bus?

Findings feed back into the `hybrid-analysis` spec.

## Prerequisites

- Ollama running locally with at least one of:
  - `qwen3-coder:latest` — recommended for orchestration (code tasks)
  - `gemma4:e4b` — lighter, fine for choreography
  - `glm-4.7-flash:latest`, `qwen3.5:latest` — alternatives
- Scaffolding from `specs/vertical-validation-spec.md` merged to master:
  - `scripts/validate_vertical.py`
  - `fixtures/validation/sample_module.py`
  - `fixtures/validation/research_topics.txt`
- Full test suite green on master.

## Procedure

### 1. Start Phoenix

Terminal 1:
```bash
cd D:/dev/avdolgikh_github_repos/multi-agent
uv run python scripts/run_phoenix.py
```
Phoenix UI at <http://localhost:6006>. Leave running.

### 2. Confirm Ollama

Terminal 2:
```bash
ollama list
```
Confirm target model is present; pull if not.

### 3. Dry-run the validator

Verifies the scaffolding wiring before paying LLM latency:
```bash
uv run python scripts/validate_vertical.py orchestration \
    --input fixtures/validation/sample_module.py --dry-run
uv run python scripts/validate_vertical.py choreography \
    --topic "event sourcing vs CQRS tradeoffs" --dry-run
```
Both should exit 0 with a one-line summary.

### 4. Run orchestration vertical

```bash
uv run python scripts/validate_vertical.py orchestration \
    --input fixtures/validation/sample_module.py
```
While running, open Phoenix UI → Traces tab.

### 5. Run choreography vertical

Pick a topic from `fixtures/validation/research_topics.txt`:
```bash
uv run python scripts/validate_vertical.py choreography \
    --topic "event sourcing vs CQRS tradeoffs"
```

### 6. Inspect in Phoenix

For each run, verify:
- **Agent Graph tab**: expected agent topology appears. Orchestration should
  look linear (parse → scan → check → report); choreography should look
  event-driven fan-out.
- **Trace tree**: parent span per run, child spans per `@traced` agent
  method, LLM spans with prompt + completion + model + token counts.
- **Latency**: note p50/p95 of LLM calls and agent-method spans.
- **Errors**: red spans? Circuit-breaker state changes?
- **Cross-run comparison**: does the pattern difference actually *show up*
  in graph shape?

### 7. Capture findings

Create `docs/vertical-validation-findings-YYYY-MM-DD.md` using the template
below. Fill in as you observe; do NOT fix bugs inline — log them as
follow-up items.

## Findings template

```markdown
# Vertical Validation Findings — YYYY-MM-DD

## What we ran
- Date / hardware
- Ollama model(s)
- Phoenix version
- Multi-agent commit SHA

## Orchestration run
- Input file
- Elapsed wall time
- Total tokens (prompt / completion)
- Sample final output (1–2 paragraphs)
- Agent graph shape — expected vs actual
- LLM span completeness — present / missing fields
- Notable latencies

## Choreography run
- Topic
- Elapsed wall time
- Total tokens
- Sample final brief
- Agent graph shape — expected vs actual
- LLM span completeness
- Notable latencies

## Gaps & surprises
- Missing spans, prompt issues, unexpected loops, cost concerns,
  trace-propagation gaps.

## Action items for `hybrid-analysis` spec
- Concrete things to adjust based on what we saw.

## Follow-up tickets
- Bugs / tech-debt to fix outside this exercise.
```

## Troubleshooting

**Phoenix startup fails with `ModuleNotFoundError: No module named 'phoenix.evals.models'`.**
`arize-phoenix-evals` 3.0.0 removed the module that `arize-phoenix` 13.x still
imports. `pyproject.toml` pins `arize-phoenix-evals<3.0.0` to avoid this. If
you see the error again, confirm the pin is present and re-run `uv sync`.

**Phoenix startup fails with `Failed to bind to address [::]:4317`.**
Another process still holds OTLP gRPC port 4317 — almost always a prior
Phoenix that did not shut down cleanly. Find and kill it:

```bash
# Windows (PowerShell or Git Bash)
netstat -ano -p tcp | grep -E ":4317|:6006"
taskkill //F //PID <pid>

# macOS / Linux
lsof -iTCP:4317 -sTCP:LISTEN
kill -9 <pid>
```

Then re-run `scripts/run_phoenix.py`. Ctrl+C in the Phoenix terminal normally
releases both ports; the leftover happens when the process was killed
mid-startup.

**`scripts/run_phoenix.py` ignores `--host` / `--port`.**
It doesn't accept CLI flags. Override via env before the command:
`PHOENIX_HOST=0.0.0.0 PHOENIX_PORT=7000 uv run python scripts/run_phoenix.py`.

**Ollama model pick.**
Each vertical hardcodes `model="..."` in its agent wiring. To swap models,
edit the `model=` kwargs in:

- `src/orchestration/code_analysis/__init__.py` (4 call sites)
- `src/choreography/research/runner.py` + `agents.py` (6 call sites)

There's no env-var override today — that's a known ergonomic gap; see
follow-ups.

## Exit criteria

- Both verticals ran at least once against real Ollama and produced
  non-empty, sensible output.
- Phoenix showed complete trace trees for each run.
- `docs/vertical-validation-findings-YYYY-MM-DD.md` exists and is filled in.
- Any bugs are logged as follow-ups, not silently patched.
- Full test suite still green.

Then proceed to `hybrid-analysis` spec, incorporating findings.
