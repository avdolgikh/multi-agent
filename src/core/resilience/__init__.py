from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable
from uuid import uuid4

from pydantic import BaseModel
from opentelemetry import trace

from core.messaging import Message, MessageBus

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "RetryPolicy",
    "DeadLetterQueue",
    "DeadLetter",
]


class CircuitOpenError(RuntimeError):
    """Raised when a call is attempted while the circuit breaker is open."""


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

    async def call(self, func: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        await self._ensure_state()
        try:
            result = await func(*args, **kwargs)
        except Exception:
            await self._record_failure()
            raise
        await self._record_success()
        return result

    async def _ensure_state(self) -> None:
        async with self._lock:
            if self._state == CircuitState.OPEN and self._opened_at is not None:
                elapsed = time.monotonic() - self._opened_at
                if elapsed >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    self._emit_state_event(self._state)
                else:
                    raise CircuitOpenError("Circuit is open")
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    raise CircuitOpenError("Circuit half-open probe limit reached")
                self._half_open_calls += 1

    async def _record_failure(self) -> None:
        async with self._lock:
            self._failure_count += 1
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                self._half_open_calls = 0
                self._failure_count = self.failure_threshold
                self._emit_state_event(self._state)
                return
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                self._emit_state_event(self._state)

    async def _record_success(self) -> None:
        async with self._lock:
            self._failure_count = 0
            if self._state in (CircuitState.OPEN, CircuitState.HALF_OPEN):
                self._state = CircuitState.CLOSED
                self._half_open_calls = 0
                self._opened_at = None
                self._emit_state_event(self._state)

    def _emit_state_event(self, state: CircuitState) -> None:
        span = trace.get_current_span()
        if not span.get_span_context().is_valid:
            return
        span.add_event(
            "circuitbreaker.state_change",
            attributes={"state": state.value, "failure_count": self._failure_count},
        )


class RetryPolicy:
    def __init__(
        self,
        *,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        exponential_base: float = 2.0,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.circuit_breaker = circuit_breaker

    async def execute(self, func: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            try:
                if self.circuit_breaker is not None:
                    result = await self.circuit_breaker.call(func, *args, **kwargs)
                else:
                    result = await func(*args, **kwargs)
                return result
            except CircuitOpenError:
                raise
            except Exception:
                attempt += 1
                if attempt >= self.max_retries:
                    raise
                delay = min(
                    self.base_delay * (self.exponential_base ** (attempt - 1)), self.max_delay
                )
                jitter = random.uniform(0, delay)
                await asyncio.sleep(min(delay + jitter, self.max_delay))


class DeadLetter(BaseModel):
    id: str
    original_message: Message
    error: str
    source: str
    timestamp: datetime
    retry_count: int = 0


@dataclass
class DeadLetterRecord:
    dead_letter: DeadLetter


class DeadLetterQueue:
    def __init__(self, *, bus: MessageBus) -> None:
        self._bus = bus
        self._store: dict[str, DeadLetterRecord] = {}
        self._lock = asyncio.Lock()

    async def send(self, message: Message, *, error: str, source: str) -> None:
        dead_letter = DeadLetter(
            id=str(uuid4()),
            original_message=message,
            error=error,
            source=source,
            timestamp=datetime.now(timezone.utc),
        )
        async with self._lock:
            self._store[dead_letter.id] = DeadLetterRecord(dead_letter=dead_letter)

    async def list_failed(self, limit: int = 100) -> list[DeadLetter]:
        async with self._lock:
            items = [record.dead_letter for record in self._store.values()]
        sorted_items = sorted(items, key=lambda item: item.timestamp)
        return sorted_items[:limit]

    async def retry(self, dead_letter_id: str) -> bool:
        async with self._lock:
            record = self._store.get(dead_letter_id)
            if record is None:
                return False
            record.dead_letter.retry_count += 1
            message = record.dead_letter.original_message
        await self._bus.publish(message.topic, message)
        return True

    async def purge(self, dead_letter_id: str) -> None:
        async with self._lock:
            self._store.pop(dead_letter_id, None)
