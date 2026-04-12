from __future__ import annotations

import argparse
import asyncio
import json
from importlib import import_module
from pathlib import Path
from typing import Sequence

from core.state import SnapshotStore
from core.tracing import TracingManager

from .agents import ParserAgent, QualityAgent, ReportAgent, SecurityAgent
from .models import AnalysisReport
from .orchestrator import CodeAnalysisOrchestrator
from .saga import SagaCoordinator
from .validation import StepValidator

__all__ = [
    "ParserAgent",
    "SecurityAgent",
    "QualityAgent",
    "ReportAgent",
    "StepValidator",
    "SagaCoordinator",
    "AnalysisReport",
    "CodeAnalysisOrchestrator",
    "main",
]


def _build_default_orchestrator() -> CodeAnalysisOrchestrator:
    orchestrator_module = import_module("orchestration.code_analysis.orchestrator")
    OrchestratorCls = orchestrator_module.CodeAnalysisOrchestrator
    tracer = TracingManager.setup("orchestration.code_analysis", endpoint=None)
    snapshot_store = SnapshotStore()
    saga = SagaCoordinator()
    validator = StepValidator()
    parser = ParserAgent(
        agent_id="parser",
        name="Parser Agent",
        model="qwen3-coder:latest",
        provider="ollama",
        tools=[],
        system_prompt="Extract structural details from the repository.",
        base_url=None,
    )
    security = SecurityAgent(
        agent_id="security",
        name="Security Agent",
        model="qwen3-coder:latest",
        provider="ollama",
        tools=[],
        system_prompt="Identify OWASP and hardcoded secret risks.",
        base_url=None,
    )
    quality = QualityAgent(
        agent_id="quality",
        name="Quality Agent",
        model="qwen3-coder:latest",
        provider="ollama",
        tools=[],
        system_prompt="Review maintainability and style.",
        base_url=None,
    )
    report = ReportAgent(
        agent_id="report",
        name="Report Agent",
        model="qwen3-coder:latest",
        provider="ollama",
        tools=[],
        system_prompt="Summarize findings and recommendations.",
        base_url=None,
    )
    return OrchestratorCls(
        parser=parser,
        security=security,
        quality=quality,
        report=report,
        validator=validator,
        saga=saga,
        snapshot_store=snapshot_store,
        tracer_provider=tracer,
    )


def _print_report(result: AnalysisReport | None) -> None:
    if result is None:
        print("No report generated.")
        return
    payload = {
        "executive_summary": result.executive_summary,
        "security_section": result.security_section,
        "quality_section": result.quality_section,
        "recommendations": [rec.model_dump() for rec in result.recommendations],
    }
    print(json.dumps(payload, indent=2))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the code analysis pipeline")
    parser.add_argument("path", help="Path to a Python file or directory")
    args = parser.parse_args(argv)
    target = Path(args.path).expanduser()
    if not target.exists():
        raise SystemExit(f"Target path {target} does not exist")
    orchestrator = _build_default_orchestrator()
    result = asyncio.run(orchestrator.run(str(target)))
    _print_report(result.report)
    if result.error:
        print(f"Error: {result.error}")
    exit_code = 0 if result.status == "completed" else 1
    raise SystemExit(exit_code)


if __name__ == "__main__":  # pragma: no cover - exercised via CLI
    main()
