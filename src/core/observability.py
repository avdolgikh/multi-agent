from __future__ import annotations

import atexit
import logging
import os

from core.tracing import TracingManager

__all__ = ["init_observability"]

_LOGGER = logging.getLogger(__name__)
_TRACELLOOP_INITIALIZED = False
_TRACELLOOP_UNAVAILABLE = False
_ATEXIT_REGISTERED = False


def init_observability(
    service_name: str,
    phoenix_endpoint: str = "http://localhost:6006/v1/traces",
) -> None:
    """Configure OTLP export plus optional OpenLLMetry auto-instrumentation.

    Traceloop is only initialized when `TRACELOOP_API_KEY` is set in the
    environment — otherwise its SDK prints a loud missing-key error on every
    local run and also tries to install a second `TracerProvider`, which
    triggers OTel's "Overriding of current TracerProvider is not allowed"
    warning.

    Registers an `atexit` handler that force-flushes + shuts down the tracer
    provider on process exit. Without this, short runs exit before
    `BatchSpanProcessor` flushes and spans are silently dropped (the cause
    of the 1.5 ms root-span-only traces observed on 2026-04-13).
    """

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or phoenix_endpoint
    _LOGGER.info("init_observability service=%s endpoint=%s", service_name, endpoint)
    provider = TracingManager.setup(service_name, endpoint=endpoint)
    _register_atexit_flush(provider)
    _enable_traceloop(service_name)


def _enable_traceloop(service_name: str) -> None:
    global _TRACELLOOP_INITIALIZED
    global _TRACELLOOP_UNAVAILABLE

    if _TRACELLOOP_INITIALIZED or _TRACELLOOP_UNAVAILABLE:
        return

    if not os.getenv("TRACELOOP_API_KEY"):
        _TRACELLOOP_UNAVAILABLE = True
        _LOGGER.info(
            "TRACELOOP_API_KEY not set; skipping Traceloop init "
            "(spans still go to OTLP endpoint directly)."
        )
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


def _register_atexit_flush(provider: object) -> None:
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return

    def _flush_and_shutdown() -> None:
        try:
            force_flush = getattr(provider, "force_flush", None)
            if callable(force_flush):
                force_flush()
            shutdown = getattr(provider, "shutdown", None)
            if callable(shutdown):
                shutdown()
        except Exception:  # pragma: no cover - best-effort on exit
            _LOGGER.exception("Failed to flush/shutdown tracer provider on exit")

    atexit.register(_flush_and_shutdown)
    _ATEXIT_REGISTERED = True
