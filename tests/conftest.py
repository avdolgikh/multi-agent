"""Pytest configuration and compatibility shims."""

from __future__ import annotations

from importlib import import_module
import sys
from unittest.mock import MagicMock

import pytest


def _ensure_inmemory_exporter() -> None:
    """
    opentelemetry-sdk>=1.25 stops re-exporting ``InMemorySpanExporter``
    from ``opentelemetry.sdk.trace.export``.  The tests rely on the legacy
    import path, so we mirror the attribute whenever it is missing.
    """
    try:
        from opentelemetry.sdk.trace.export import InMemorySpanExporter  # type: ignore  # noqa: F401
    except Exception:
        try:
            export_module = import_module("opentelemetry.sdk.trace.export")
            in_memory_module = import_module(
                "opentelemetry.sdk.trace.export.in_memory_span_exporter"
            )
        except Exception:
            return
        setattr(export_module, "InMemorySpanExporter", in_memory_module.InMemorySpanExporter)


_ensure_inmemory_exporter()


# Mock modules only when they genuinely can't be imported yet.
def _ensure_test_module(name: str) -> None:
    if name in sys.modules:
        return
    try:
        import_module(name)
    except ModuleNotFoundError:
        sys.modules[name] = MagicMock()


_ensure_test_module("src.core.observability")
_ensure_test_module("src.core.tracing")


@pytest.fixture(autouse=True)
def _stub_external_telemetry(monkeypatch):
    """Unit-test rule: no test may perform real external I/O.

    Stubs Traceloop SDK init and the OTLP HTTP span exporter so they never
    touch the network. Tests that want to verify telemetry call sites can
    override these locally via their own monkeypatch.
    """
    from types import ModuleType, SimpleNamespace

    traceloop_sdk = ModuleType("traceloop.sdk")
    traceloop_sdk.Traceloop = SimpleNamespace(init=lambda **kwargs: None)  # type: ignore[attr-defined]
    traceloop_pkg = ModuleType("traceloop")
    traceloop_pkg.sdk = traceloop_sdk  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "traceloop", traceloop_pkg)
    monkeypatch.setitem(sys.modules, "traceloop.sdk", traceloop_sdk)

    try:
        from opentelemetry.exporter.otlp.proto.http import trace_exporter as _otlp_http

        class _NoopOTLPExporter:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def export(self, spans):
                from opentelemetry.sdk.trace.export import SpanExportResult

                return SpanExportResult.SUCCESS

            def shutdown(self) -> None:
                pass

            def force_flush(self, timeout_millis: int = 30000) -> bool:
                return True

        monkeypatch.setattr(_otlp_http, "OTLPSpanExporter", _NoopOTLPExporter)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_tracer_provider_state():
    """Reset TracingManager and OpenTelemetry global TracerProvider between tests.

    OTel's global TracerProvider can only be set once per process; without a reset
    each test after the first one picks up a stale provider and exporters wired in
    later tests receive no spans.
    """
    yield
    try:
        from core.tracing import TracingManager

        TracingManager._provider = None
    except Exception:
        pass
    try:
        import opentelemetry.trace as _trace_api

        _trace_api._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
        _trace_api._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    except Exception:
        pass
