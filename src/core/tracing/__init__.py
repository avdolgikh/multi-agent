from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, Coroutine, Mapping

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExportResult,
    SpanExporter,
)
from opentelemetry.trace import (
    NonRecordingSpan,
    Span,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
    TraceState,
    set_span_in_context,
)

__all__ = [
    "TracingManager",
    "traced",
    "inject_context",
    "extract_context",
]


class _NoOpSpanExporter(SpanExporter):
    """Minimal exporter that silently discards spans."""

    def export(self, spans: list[Span]) -> SpanExportResult:  # type: ignore[override]
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # type: ignore[override]
        return None

    def force_flush(self, timeout_millis: int | None = None) -> bool:  # type: ignore[override]
        return True


class TracingManager:
    """Utility for configuring a tracer provider once per process."""

    _provider: TracerProvider | None = None

    @classmethod
    def setup(cls, service_name: str, endpoint: str | None = None) -> TracerProvider:
        if cls._provider is not None:
            return cls._provider

        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        if endpoint:
            exporter = OTLPSpanExporter(endpoint=endpoint)
            processor = BatchSpanProcessor(exporter)
            provider.add_span_processor(processor)
        else:
            provider.add_span_processor(SimpleSpanProcessor(_NoOpSpanExporter()))
        trace.set_tracer_provider(provider)
        cls._provider = provider
        return provider


_SENSITIVE_KEYS = ("key", "token", "secret", "password", "authorization")


def traced(func: Callable[..., Coroutine[Any, Any, Any]]):
    """Async decorator that records spans and errors around the coroutine."""

    if not asyncio.iscoroutinefunction(func):
        msg = "@traced can only be applied to async callables"
        raise TypeError(msg)

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any):
        tracer = trace.get_tracer(func.__module__)
        attributes = _sanitize_arguments(args, kwargs)
        agent_task = _find_agent_task(args, kwargs)
        span_kwargs: dict[str, Any] = {}
        parent_context = _resolve_task_parent_context(agent_task)
        if parent_context is not None:
            span_kwargs["context"] = parent_context
        with tracer.start_as_current_span(func.__qualname__, **span_kwargs) as span:
            for key, value in attributes.items():
                span.set_attribute(key, value)
            _store_task_trace_context(agent_task)
            try:
                return await func(*args, **kwargs)
            except Exception as exc:  # pragma: no cover - exception path observed via tests
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                raise

    return wrapper


def _find_agent_task(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any | None:
    for value in args:
        candidate = _coerce_agent_task(value)
        if candidate is not None:
            return candidate
    for value in kwargs.values():
        candidate = _coerce_agent_task(value)
        if candidate is not None:
            return candidate
    return None


def _coerce_agent_task(value: Any) -> Any | None:
    if value is None:
        return None
    if value.__class__.__name__ != "AgentTask":
        return None
    if not hasattr(value, "trace_context") or not hasattr(value, "task_id"):
        return None
    return value


def _resolve_task_parent_context(agent_task: Any | None):
    if agent_task is None:
        return None
    trace_ctx = getattr(agent_task, "trace_context", None)
    if not trace_ctx:
        return None
    return extract_context(trace_ctx)


def _store_task_trace_context(agent_task: Any | None) -> None:
    if agent_task is None:
        return
    agent_task.trace_context = inject_context()


def inject_context() -> dict:
    """Serialize the current span context into a plain dictionary."""

    span = trace.get_current_span()
    context = span.get_span_context()
    if context is None or not context.is_valid:
        return {}
    return {
        "trace_id": context.trace_id,
        "span_id": context.span_id,
        "trace_flags": int(context.trace_flags),
        "is_remote": context.is_remote,
    }


def extract_context(ctx: Mapping[str, Any] | None) -> otel_context.Context:
    """Restore a span context from a dictionary produced by inject_context."""

    if not ctx:
        return otel_context.get_current()
    trace_id_value = ctx.get("trace_id")
    span_id_value = ctx.get("span_id")
    if trace_id_value is None or span_id_value is None:
        return otel_context.get_current()
    trace_id_int = _parse_identifier(trace_id_value)
    span_id_int = _parse_identifier(span_id_value)
    flags = TraceFlags(int(ctx.get("trace_flags", 1)))
    span_context = SpanContext(
        trace_id=trace_id_int,
        span_id=span_id_int,
        is_remote=bool(ctx.get("is_remote", True)),
        trace_flags=flags,
        trace_state=TraceState(),
    )
    return set_span_in_context(NonRecordingSpan(span_context))


def _sanitize_arguments(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for index, value in enumerate(args):
        sanitized[f"arg.{index}"] = _scrub_value(value)
    for key, value in kwargs.items():
        sanitized[f"kw.{key}"] = _scrub_value(value, key)
    return sanitized


def _scrub_value(value: Any, key_hint: str | None = None) -> Any:
    if key_hint and any(token in key_hint.lower() for token in _SENSITIVE_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            dict_key: _scrub_value(dict_value, dict_key)
            for dict_key, dict_value in value.items()
            if not any(token in dict_key.lower() for token in _SENSITIVE_KEYS)
        }
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _parse_identifier(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        base = 16 if any(char in value.lower() for char in "abcdef") else 10
        return int(value, base)
    raise ValueError("Unsupported trace context identifier")
