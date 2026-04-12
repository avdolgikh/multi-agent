"""Tests for the Observability Phase 1 spec (Arize Phoenix wiring)."""

from __future__ import annotations

import builtins
import importlib
import logging
import runpy
import sys
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import core.agents as core_agents
import core.tracing as core_tracing
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import InMemorySpanExporter, SimpleSpanProcessor


def _load_observability_module():
    """Reload the module to keep each test independent."""
    for key in ("core.observability", "src.core.observability"):
        sys.modules.pop(key, None)
    try:
        module = importlib.import_module("core.observability")
    except ModuleNotFoundError:
        pytest.fail(
            "core.observability is not implemented yet; Observability Phase 1 spec "
            "requires init_observability() to exist."
        )
    return importlib.reload(module)


def _provide_fake_traceloop(monkeypatch, init_calls: list[dict[str, bool]]):
    """Insert a fake traceloop.sdk module that records init() invocations."""
    class FakeTraceloop:
        @staticmethod
        def init(*, app_name: str, disable_batch: bool) -> None:
            init_calls.append({"app_name": app_name, "disable_batch": disable_batch})

    fake_sdk = ModuleType("traceloop.sdk")
    fake_sdk.Traceloop = FakeTraceloop

    fake_pkg = ModuleType("traceloop")
    fake_pkg.sdk = fake_sdk

    monkeypatch.setitem(sys.modules, "traceloop", fake_pkg)
    monkeypatch.setitem(sys.modules, "traceloop.sdk", fake_sdk)


def _provide_fake_async_openai(monkeypatch):
    """Replace AsyncOpenAI client with a stub so no real LLM calls happen."""

    class FakeChatResponse:
        def __init__(self, *, content: str, model: str) -> None:
            self.choices = [
                SimpleNamespace(message=SimpleNamespace(role="assistant", content=content))
            ]
            self.usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
            self.model = model

    class FakeClient:
        def __init__(self, **config) -> None:
            self.config = dict(config)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        async def _create(self, **kwargs) -> FakeChatResponse:
            model = kwargs.get("model", "stub-model")
            messages = kwargs.get("messages", [])
            content = "ok"
            if isinstance(messages, list) and messages:
                message = messages[0]
                if isinstance(message, dict):
                    content = message.get("content", content)
                else:
                    content = getattr(message, "content", content)
            return FakeChatResponse(content=content, model=model)

    monkeypatch.setattr(core_agents, "AsyncOpenAI", FakeClient, raising=False)
    if hasattr(core_agents, "openai"):
        monkeypatch.setattr(core_agents.openai, "AsyncOpenAI", FakeClient)


def test_pyproject_includes_observability_dependencies():
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies: list[str] = project["project"]["dependencies"]
    assert any("arize-phoenix" in dep for dep in dependencies)
    assert any("opentelemetry-exporter-otlp" in dep for dep in dependencies)
    assert any("traceloop-sdk" in dep for dep in dependencies)


def test_readme_mentions_traces_live():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "See traces live" in text
    assert "uv run python scripts/run_phoenix.py" in text
    assert "uv run python -m src.orchestration.code_analysis" in text
    assert "http://localhost:6006" in text


def test_run_phoenix_script_exists():
    script_path = Path("scripts/run_phoenix.py")
    assert script_path.exists(), "Phoenix launcher script should exist"
    content = script_path.read_text(encoding="utf-8")
    assert "phoenix.server.main" in content or "launch_app" in content
    assert "6006" in content


def test_init_observability_sets_up_tracing_and_traceloop(monkeypatch):
    module = _load_observability_module()
    core_tracing.TracingManager._provider = None
    setup_calls: list[tuple[str, str | None]] = []

    def fake_setup(service_name: str, endpoint: str | None = None):
        provider = core_tracing.TracingManager._provider
        if provider is not None:
            return provider
        provider = object()
        setup_calls.append((service_name, endpoint))
        core_tracing.TracingManager._provider = provider
        return provider

    monkeypatch.setattr(core_tracing.TracingManager, "setup", fake_setup)
    traceloop_calls: list[dict[str, bool]] = []
    _provide_fake_traceloop(monkeypatch, traceloop_calls)

    module.init_observability(
        "orchestration-code-analysis", phoenix_endpoint="http://localhost:6006/v1/traces"
    )
    module.init_observability(
        "orchestration-code-analysis", phoenix_endpoint="http://localhost:6006/v1/traces"
    )

    assert setup_calls == [
        ("orchestration-code-analysis", "http://localhost:6006/v1/traces")
    ]
    assert traceloop_calls == [
        {"app_name": "orchestration-code-analysis", "disable_batch": True}
    ]


def test_init_observability_respects_otel_endpoint_env(monkeypatch):
    module = _load_observability_module()
    core_tracing.TracingManager._provider = None
    endpoints: list[str | None] = []

    def fake_setup(service_name: str, endpoint: str | None = None):
        endpoints.append(endpoint)
        return object()

    monkeypatch.setattr(core_tracing.TracingManager, "setup", fake_setup)
    _provide_fake_traceloop(monkeypatch, [])
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://custom:4318/v1/traces")

    module.init_observability("choreography-research", phoenix_endpoint="http://localhost:6006/v1/traces")

    assert endpoints[-1] == "https://custom:4318/v1/traces"


def test_init_observability_warns_when_traceloop_missing(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    core_tracing.TracingManager._provider = None
    setup_called = False

    def fake_setup(service_name: str, endpoint: str | None = None):
        nonlocal setup_called
        setup_called = True
        return object()

    monkeypatch.setattr(core_tracing.TracingManager, "setup", fake_setup)

    original_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("traceloop"):
            raise ModuleNotFoundError("traceloop is not installed")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    module = _load_observability_module()
    module.init_observability("hybrid-analysis")

    assert setup_called
    assert "traceloop" in caplog.text.lower()


def test_orchestration_code_analysis_smoke_exports_spans(monkeypatch, tmp_path):
    """Running the code_analysis demo should export spans via the tracing manager."""
    exporter = InMemorySpanExporter()
    providers: list[TracerProvider] = []
    service_names: list[str] = []

    def fake_setup(service_name: str, endpoint: str | None = None) -> TracerProvider:
        service_names.append(service_name)
        provider = core_tracing.TracingManager._provider
        if provider is None:
            provider = TracerProvider()
            trace.set_tracer_provider(provider)
            core_tracing.TracingManager._provider = provider
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        providers.append(provider)
        return provider

    core_tracing.TracingManager._provider = None
    monkeypatch.setattr(core_tracing.TracingManager, "setup", fake_setup)
    _provide_fake_traceloop(monkeypatch, [])
    _provide_fake_async_openai(monkeypatch)

    sample_file = tmp_path / "demo.py"
    sample_file.write_text("print('observability demo')")
    monkeypatch.setattr(sys, "argv", ["python", str(sample_file)])

    try:
        runpy.run_module("src.orchestration.code_analysis", run_name="__main__")
    except ModuleNotFoundError:
        pytest.fail(
            "src.orchestration.code_analysis is not implemented yet; Observability Phase 1 "
            "requires the orchestrated demo entry point."
        )
    except SystemExit as exc:
        if exc.code not in (None, 0):
            pytest.fail(f"code_analysis entry point exited with {exc.code}")

    assert providers, "TracingManager.setup was not called during the entry point run"
    assert "orchestration-code-analysis" in service_names, (
        "init_observability must be invoked with service name 'orchestration-code-analysis'"
    )
    provider = providers[-1]
    provider.force_flush()
    spans = exporter.get_finished_spans()
    assert spans, "Running the orchestration demo should generate at least one span"
