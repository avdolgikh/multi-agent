from __future__ import annotations

import logging
import os

from core.tracing import TracingManager

__all__ = ["init_observability"]

_LOGGER = logging.getLogger(__name__)
_TRACELLOOP_INITIALIZED = False
_TRACELLOOP_UNAVAILABLE = False


def init_observability(
    service_name: str,
    phoenix_endpoint: str = "http://localhost:6006/v1/traces",
) -> None:
    """Configure OTLP export plus OpenLLMetry auto-instrumentation."""

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or phoenix_endpoint
    TracingManager.setup(service_name, endpoint=endpoint)
    _enable_traceloop(service_name)


def _enable_traceloop(service_name: str) -> None:
    global _TRACELLOOP_INITIALIZED
    global _TRACELLOOP_UNAVAILABLE

    if _TRACELLOOP_INITIALIZED or _TRACELLOOP_UNAVAILABLE:
        return

    try:
        from traceloop.sdk import Traceloop  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        _TRACELLOOP_UNAVAILABLE = True
        _LOGGER.warning(
            "Traceloop SDK is not installed; continuing without LLM span instrumentation."
        )
        return
    except Exception:
        _TRACELLOOP_UNAVAILABLE = True
        _LOGGER.warning(
            "Failed to import Traceloop SDK; continuing without LLM span instrumentation.",
            exc_info=True,
        )
        return

    try:
        Traceloop.init(app_name=service_name, disable_batch=True)
    except Exception:
        _TRACELLOOP_UNAVAILABLE = True
        _LOGGER.warning(
            "Traceloop initialization failed; continuing without OpenLLMetry instrumentation.",
            exc_info=True,
        )
        return

    _TRACELLOOP_INITIALIZED = True
