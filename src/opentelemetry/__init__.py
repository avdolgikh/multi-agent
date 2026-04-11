from __future__ import annotations

from importlib import import_module
from pkgutil import extend_path

# Preserve all namespace package contributors provided by the actual
# opentelemetry installation.
__path__ = extend_path(__path__, __name__)  # type: ignore[name-defined]


def _ensure_inmemory_exporter() -> None:
    """
    opentelemetry-sdk>=1.25 stops re-exporting ``InMemorySpanExporter``
    from ``opentelemetry.sdk.trace.export``.  The frozen tests and our
    tracing helpers rely on the legacy import path, so we mirror the
    attribute whenever it is missing.
    """

    try:  # Already present on older versions.
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
