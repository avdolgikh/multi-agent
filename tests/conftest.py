"""Pytest configuration and compatibility shims."""

from __future__ import annotations

from importlib import import_module


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
