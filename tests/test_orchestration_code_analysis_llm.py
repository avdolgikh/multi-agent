from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque

import pytest

from core.agents import AgentTask, BaseAgent, LLMResponse, TokenUsage
from orchestration.code_analysis.agents import (
    LLMResponseFormatError,
    ParserAgent,
    QualityAgent,
    ReportAgent,
    SecurityAgent,
)
from orchestration.code_analysis.models import (
    AnalysisReport,
    ParseResult,
    QualityResult,
    Recommendation,
    SecurityFinding,
    SecurityResult,
)
from orchestration.code_analysis.orchestrator import CodeAnalysisOrchestrator
from orchestration.code_analysis.validation import StepValidator
from orchestration.code_analysis.saga import SagaCoordinator
import test_orchestration_code_analysis as legacy_tests


@dataclass
class _RecordedCall:
    agent: BaseAgent
    messages: list[dict[str, Any]]


class _LLMStub:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._queued_payloads: dict[str, Deque[str]] = defaultdict(deque)
        self.calls: list[_RecordedCall] = []

        async def _fake_call(agent: BaseAgent, messages: list[dict[str, Any]]) -> LLMResponse:
            agent_name = getattr(agent, "name", agent.agent_id)
            self.calls.append(_RecordedCall(agent=agent, messages=messages))
            queue = self._queued_payloads[agent_name]
            if queue:
                content = queue.popleft()
            else:
                content = self._default_payload(agent)
            return LLMResponse(
                content=content,
                tool_calls=None,
                usage=TokenUsage(),
                model="stub-model",
                provider="test",
            )

        monkeypatch.setattr(BaseAgent, "call_llm", _fake_call)

    def queue_response(self, agent_name: str, payload: str | dict[str, Any]) -> None:
        serialized = payload if isinstance(payload, str) else json.dumps(payload)
        self._queued_payloads[agent_name].append(serialized)

    def call_count(self, agent_name: str) -> int:
        return sum(1 for call in self.calls if call.agent.name == agent_name)

    def last_call(self, agent_name: str) -> _RecordedCall:
        for call in reversed(self.calls):
            if call.agent.name == agent_name:
                return call
        raise AssertionError(f"No call recorded for {agent_name}")

    def _default_payload(self, agent: BaseAgent) -> str:
        if isinstance(agent, ParserAgent):
            result = ParseResult(
                functions=[
                    {
                        "name": "placeholder_function",
                        "params": ["value"],
                        "return_type": "int",
                        "line_range": "1-5",
                    }
                ],
                classes=[],
                imports=[],
                dependencies={},
            )
            return result.model_dump_json()
        if isinstance(agent, SecurityAgent):
            result = SecurityResult(
                findings=[
                    SecurityFinding(
                        severity="low",
                        location="module.py:1",
                        description="Placeholder finding",
                        recommendation="Review manually.",
                    )
                ]
            )
            return result.model_dump_json()
        if isinstance(agent, QualityAgent):
            result = QualityResult(
                score=90,
                issues=[],
                metrics={"cyclomatic_complexity": {"placeholder_function": 3}},
            )
            return result.model_dump_json()
        if isinstance(agent, ReportAgent):
            report = AnalysisReport(
                executive_summary="Automated summary",
                security_section={"findings": ["Placeholder finding"]},
                quality_section={"score": 90, "issues": []},
                recommendations=[
                    Recommendation(title="Monitor", priority="low", detail="All good."),
                ],
            )
            return report.model_dump_json()
        raise AssertionError(f"Unsupported agent for default payload: {agent.name}")


@pytest.fixture(autouse=True)
def llm_stub(monkeypatch: pytest.MonkeyPatch):
    """Patch BaseAgent.call_llm for every test, returning schema-valid JSON by default."""

    stub = _LLMStub(monkeypatch)
    yield stub


def _sample_agent(agent_cls: type[BaseAgent], name: str) -> BaseAgent:
    return agent_cls(
        agent_id=name.lower().replace(" ", "-"),
        name=name,
        model="stub-model",
        provider="ollama",
        tools=[],
        system_prompt=f"{name} system prompt",
        base_url=None,
    )


def _build_task_for_path(input_path: str) -> AgentTask:
    return AgentTask(
        task_id="task",
        input_data={"input_path": input_path},
        metadata={},
        trace_context={},
    )


@pytest.mark.asyncio
async def test_parser_agent_merges_llm_output_and_uses_system_prompt(tmp_path, llm_stub):
    target = tmp_path / "module.py"
    target.write_text("def foo(x):\n    return x + 1\n")
    agent = _sample_agent(ParserAgent, "Parser Agent")
    payload = {
        "functions": [
            {"name": "foo", "params": ["x"], "return_type": "int", "line_range": "1-2"},
            {"name": "bar", "params": [], "return_type": None, "line_range": "4-5"},
        ],
        "classes": [{"name": "Helper", "line_range": "7-10", "methods": ["run"]}],
        "imports": ["math"],
        "dependencies": {"math": "stdlib"},
    }
    llm_stub.queue_response(agent.name, payload)

    result = await agent.execute(_build_task_for_path(str(target)))
    parse_result = result.output_data["result"]
    assert any(item.get("name") == "Helper" for item in _serialize_list(parse_result.classes))
    assert any(item.get("name") == "bar" for item in _serialize_list(parse_result.functions))
    assert "math" in parse_result.imports

    assert llm_stub.call_count(agent.name) == 1
    recorded = llm_stub.last_call(agent.name)
    assert recorded.messages[0]["role"] == "system"
    assert recorded.messages[0]["content"] == agent.system_prompt
    assert recorded.messages[1]["role"] == "user"
    assert "AST context:" in recorded.messages[1]["content"]
    assert "module.py" in recorded.messages[1]["content"]


@pytest.mark.asyncio
async def test_security_agent_uses_llm_findings(tmp_path, llm_stub):
    target = tmp_path / "module.py"
    target.write_text("import os\n\nos.system('ls')\n")
    agent = _sample_agent(SecurityAgent, "Security Agent")
    payload = {
        "findings": [
            {
                "severity": "critical",
                "location": "module.py:3",
                "description": "Dynamic command execution detected.",
                "recommendation": "Use parameterized shell calls or remove execution.",
            }
        ]
    }
    llm_stub.queue_response(agent.name, payload)

    result = await agent.execute(_build_task_for_path(str(target)))
    security_result = result.output_data["result"]
    assert security_result.findings[0].description.startswith("Dynamic command")
    assert llm_stub.call_count(agent.name) == 1
    recorded = llm_stub.last_call(agent.name)
    assert recorded.messages[0]["content"] == agent.system_prompt
    assert "ast context:" in recorded.messages[1]["content"].lower()
    assert target.name in recorded.messages[1]["content"]
    assert "candidate" in recorded.messages[1]["content"].lower()


@pytest.mark.asyncio
async def test_quality_agent_returns_llm_score_and_issues(tmp_path, llm_stub):
    target = tmp_path / "module.py"
    target.write_text(
        """
def buggy(values):
    total = 0
    for value in values:
        total += value
    return total
"""
    )
    agent = _sample_agent(QualityAgent, "Quality Agent")
    payload = {
        "score": 64,
        "issues": [
            {
                "location": "buggy",
                "description": "Loop lacks error handling.",
                "severity": "medium",
            }
        ],
        "metrics": {"custom": {"buggy": {"length": 5}}},
    }
    llm_stub.queue_response(agent.name, payload)

    result = await agent.execute(_build_task_for_path(str(target)))
    quality_result = result.output_data["result"]
    assert quality_result.score == 64
    assert quality_result.issues[0].location == "buggy"
    assert "cyclomatic_complexity" in quality_result.metrics
    assert llm_stub.call_count(agent.name) == 1
    recorded = llm_stub.last_call(agent.name)
    assert "AST context" in recorded.messages[1]["content"]
    assert "buggy" in recorded.messages[1]["content"]
    assert target.name in recorded.messages[1]["content"]


@pytest.mark.asyncio
async def test_report_agent_summarizes_llm_payload(tmp_path, llm_stub):
    agent = _sample_agent(ReportAgent, "Report Agent")
    parse = ParseResult(
        functions=[{"name": "foo", "params": [], "return_type": None, "line_range": "1-2"}],
        classes=[],
        imports=["math"],
        dependencies={"math": "stdlib"},
    )
    security = SecurityResult(
        findings=[
            SecurityFinding(
                severity="high",
                location="module.py:5",
                description="Dangerous eval",
                recommendation="Remove eval",
            )
        ]
    )
    quality = QualityResult(score=75, issues=[], metrics={"cyclomatic_complexity": {"foo": 2}})
    payload = AnalysisReport(
        executive_summary="LLM summary referencing eval.",
        security_section={"findings": ["Dangerous eval"]},
        quality_section={"score": 75, "issues": []},
        recommendations=[Recommendation(title="Fix eval", priority="high")],
    ).model_dump()
    llm_stub.queue_response(agent.name, payload)

    target = tmp_path / "module.py"
    target.write_text("def foo():\n    return 1\n")
    task = AgentTask(
        task_id="report",
        input_data={
            "input_path": str(target),
            "results": {
                CodeAnalysisOrchestrator.STEP_PARSING: parse,
                CodeAnalysisOrchestrator.STEP_SCANNING: security,
                CodeAnalysisOrchestrator.STEP_CHECKING: quality,
            },
        },
        metadata={},
        trace_context={},
    )
    result = await agent.execute(task)
    report = result.output_data["result"]
    assert report.executive_summary.startswith("LLM summary")
    assert report.recommendations[0].title == "Fix eval"
    assert llm_stub.call_count(agent.name) == 1
    recorded = llm_stub.last_call(agent.name)
    assert recorded.messages[0]["content"] == agent.system_prompt
    assert "AST context" in recorded.messages[1]["content"]
    assert target.name in recorded.messages[1]["content"]


@pytest.mark.asyncio
async def test_malformed_llm_output_triggers_rollback(tmp_path, llm_stub):
    target = tmp_path / "module.py"
    target.write_text("def foo(x):\n    return x * 2\n")
    quality_agent = _sample_agent(QualityAgent, "Quality Agent")
    llm_stub.queue_response(quality_agent.name, "not json")
    with pytest.raises(LLMResponseFormatError):
        await quality_agent.execute(_build_task_for_path(str(target)))

    orchestrator, *_ = legacy_tests._build_orchestrator_components(
        validator=StepValidator(),
        snapshot_store=None,
        saga=SagaCoordinator(),
        tracer_provider=None,
    )
    llm_stub.queue_response("Quality Agent", "not json")

    result = await orchestrator.run(str(target))
    assert result.status == "rolled_back"
    assert "invalid LLM response" in (result.error or "")
    assert CodeAnalysisOrchestrator.STEP_CHECKING not in result.step_results
    assert CodeAnalysisOrchestrator.STEP_SCANNING in result.step_results


@pytest.mark.asyncio
async def test_quality_agent_surfaces_off_by_one_issue(llm_stub):
    sample_module = (
        Path(__file__).resolve().parents[1] / "fixtures" / "validation" / "sample_module.py"
    )
    agent = _sample_agent(QualityAgent, "Quality Agent")
    payload = {
        "score": 72,
        "issues": [
            {
                "location": "compute_average_off_by_one",
                "description": "Loop stops at len(values) - 1 causing off-by-one.",
                "severity": "high",
            }
        ],
        "metrics": {},
    }
    llm_stub.queue_response(agent.name, payload)

    result = await agent.execute(_build_task_for_path(str(sample_module)))
    quality_result = result.output_data["result"]
    assert any(issue.location == "compute_average_off_by_one" for issue in quality_result.issues)
    assert quality_result.score < 100


def _serialize_list(items: list[Any]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            serialized.append(item)
        elif hasattr(item, "model_dump"):
            serialized.append(item.model_dump())
    return serialized
