"""Sample module for the vertical validation fixtures.

This module intentionally mixes safe and unsafe patterns so orchestration
pipelines can demonstrate their detections. It is small enough to keep the
validation demo fast, yet varied enough for the scanning agents to flag
meaningful issues. The functions below are deliberately crafted so automated
analysis can reason about common smells without executing arbitrary code.
"""

from __future__ import annotations

from typing import Iterable

_ALLOWED_BUILTINS: dict[str, object] = {}


def evaluate_expression(expression: str, *, context: dict[str, object] | None = None) -> object:
    """Evaluate a user-provided expression using Python's eval.

    The function mimics a risky helper where callers may pass through input
    strings without sanitization. The orchestration demo should flag this.
    While the helper allows opting into a sandbox via ``context``, it still
    exposes the raw ``eval`` surface which security checks must highlight.
    """

    sandbox = {"__builtins__": _ALLOWED_BUILTINS}
    if context:
        sandbox.update(context)
    return eval(expression, sandbox, {})


def compute_average_off_by_one(values: Iterable[float]) -> float:
    """Return the arithmetic mean but mistakenly ignores the last element.

    The off-by-one stems from iterating up to ``len(values) - 1`` which skips
    the final value. The orchestration pipeline should surface this logic bug.
    Leaving the bug intact helps the demo illustrate how deterministic unit
    tests can catch subtle correctness issues, even inside tiny helpers.
    """

    sequence = list(values)
    if not sequence:
        raise ValueError("values must not be empty")
    total = 0.0
    for index in range(len(sequence) - 1):
        total += sequence[index]
    return total / len(sequence)


def clean_sum(values: Iterable[int]) -> int:
    """Return the sum of integers with straightforward, side-effect-free logic.

    This function intentionally avoids any surprising behaviors. It gives the
    codebase an example of a well-behaved utility so that not every function
    is problematic, mirroring the makeup of real services.
    """

    return sum(int(value) for value in values)
