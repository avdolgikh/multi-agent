from __future__ import annotations

import asyncio
import inspect
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

__all__ = ["CompensationResult", "SagaCoordinator"]


class CompensationResult(BaseModel):
    steps_compensated: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)


class SagaCoordinator:
    """Tracks step compensations and replays them in reverse order."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._stack: list[tuple[str, Callable[[], Awaitable[None] | None]]] = []

    async def register_step(self, step: str, compensate: Callable[[], Awaitable[None] | None]) -> None:
        async with self._lock:
            self._stack.append((step, compensate))

    async def compensate_all(self) -> CompensationResult:
        async with self._lock:
            stack = list(self._stack)
            self._stack.clear()
        steps_compensated: list[str] = []
        failures: list[str] = []
        for step, handler in reversed(stack):
            try:
                maybe_awaitable = handler()
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable  # type: ignore[arg-type]
                steps_compensated.append(step)
            except Exception as exc:  # pragma: no cover - defensive logging path
                failures.append(f"{step}: {exc}")
        return CompensationResult(steps_compensated=steps_compensated, failures=failures)
