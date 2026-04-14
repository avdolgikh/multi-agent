from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Sequence

import pytest

from core.agents import AgentTask, BaseAgent
from core.messaging import InMemoryBus
from core.resilience import DeadLetterQueue
from core.state import InMemoryEventStore

import choreography.research.agents as research_agents


LLM_MARKER = "LLM_MARKER_abc123"


class _FakeSearchTool:
    def __init__(self, results: Sequence[dict[str, Any]]) -> None:
        self.name = "fake_search"
        self.description = "Fake search tool for unit tests"
        self.parameters: dict[str, Any] = {}
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(params))
        return {"results": list(self._results)}


def _build_agent(
    agent_factory: Callable[..., Any],
    *,
    search_tool: _FakeSearchTool,
) -> Any:
    bus = InMemoryBus()
    store = InMemoryEventStore()
    context = research_agents.ResearchContext(
        research_id="research-123",
        topic="event sourcing vs CQRS",
        scope="global",
        deadline=None,
        trace_context={"trace_id": "trace-123", "span_id": "span-123"},
        started_at=datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc),
    )
    publisher = research_agents.ResearchEventPublisher(
        bus=bus,
        event_store=store,
        context=context,
    )
    return agent_factory(
        search_tool=search_tool,
        bus=bus,
        publisher=publisher,
        dead_letter_queue=DeadLetterQueue(bus=bus),
    )


@pytest.fixture(autouse=True)
def _stub_llm_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_call_llm(self: BaseAgent, messages: Sequence[Any]) -> SimpleNamespace:
        return SimpleNamespace(content=LLM_MARKER)

    monkeypatch.setattr(BaseAgent, "call_llm", _fake_call_llm)


@pytest.mark.asyncio
async def test_academic_search_agent_surfaces_llm_summary() -> None:
    search_tool = _FakeSearchTool(
        [
            {
                "title": "Academic result",
                "abstract": "Tool abstract that used to win.",
                "raw_content": "Original academic abstract body.",
                "authors": ["Dr. Ada"],
                "year": 2025,
                "url": "https://doi.example/academic",
            }
        ]
    )
    agent = _build_agent(research_agents.AcademicSearchAgent, search_tool=search_tool)

    findings = await agent._generate_findings(
        AgentTask(task_id="academic-task", input_data={"topic": "event sourcing"})
    )

    assert len(findings) == 1
    payload = findings[0]
    assert LLM_MARKER in payload["summary"]
    assert payload["raw_content"] == "Tool abstract that used to win."
    assert payload["authors"] == ["Dr. Ada"]
    assert payload["year"] == 2025
    assert set(payload) == {
        "title",
        "summary",
        "url",
        "relevance_score",
        "raw_content",
        "authors",
        "year",
    }


@pytest.mark.asyncio
async def test_code_analysis_agent_surfaces_llm_summary() -> None:
    search_tool = _FakeSearchTool(
        [
            {
                "title": "Code result",
                "summary": "Tool summary that used to win.",
                "raw_content": "Original repository excerpt.",
                "repository": "https://github.com/example/repo",
                "language": "rust",
                "url": "https://github.com/example/repo#readme",
            }
        ]
    )
    agent = _build_agent(research_agents.CodeAnalysisAgent, search_tool=search_tool)

    findings = await agent._generate_findings(
        AgentTask(task_id="code-task", input_data={"topic": "CQRS"})
    )

    assert len(findings) == 1
    payload = findings[0]
    assert LLM_MARKER in payload["summary"]
    assert payload["repository"] == "https://github.com/example/repo"
    assert payload["language"] == "rust"
    assert payload["raw_content"] == "Original repository excerpt."
    assert set(payload) == {
        "title",
        "summary",
        "url",
        "relevance_score",
        "raw_content",
        "repository",
        "language",
    }


@pytest.mark.asyncio
async def test_news_search_agent_surfaces_llm_summary() -> None:
    published_date = datetime(2026, 4, 12, 15, 30, tzinfo=timezone.utc)
    search_tool = _FakeSearchTool(
        [
            {
                "title": "News result",
                "summary": "Tool news summary that used to win.",
                "raw_content": "Original news article excerpt.",
                "published_date": published_date,
                "url": "https://news.example/story",
            }
        ]
    )
    agent = _build_agent(research_agents.NewsSearchAgent, search_tool=search_tool)

    findings = await agent._generate_findings(
        AgentTask(task_id="news-task", input_data={"topic": "distributed systems"})
    )

    assert len(findings) == 1
    payload = findings[0]
    assert LLM_MARKER in payload["summary"]
    assert payload["published_date"] == published_date
    assert payload["raw_content"] == "Original news article excerpt."
    assert set(payload) == {
        "title",
        "summary",
        "url",
        "relevance_score",
        "raw_content",
        "published_date",
    }


@pytest.mark.asyncio
async def test_academic_search_agent_uses_fallback_summary_when_llm_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _raise_call_llm(self: BaseAgent, messages: Sequence[Any]) -> SimpleNamespace:
        raise RuntimeError("llm unavailable")

    monkeypatch.setattr(BaseAgent, "call_llm", _raise_call_llm)

    search_tool = _FakeSearchTool(
        [
            {
                "title": "Academic fallback result",
                "abstract": "Tool abstract that still needs to be preserved.",
                "raw_content": "Original academic abstract body.",
                "authors": ["Dr. Ada"],
                "year": 2025,
                "url": "https://doi.example/academic-fallback",
            }
        ]
    )
    agent = _build_agent(research_agents.AcademicSearchAgent, search_tool=search_tool)

    findings = await agent._generate_findings(
        AgentTask(task_id="academic-fallback-task", input_data={"topic": "event sourcing"})
    )

    assert len(findings) == 1
    payload = findings[0]
    assert payload["summary"]
    assert payload["summary"].startswith("Findings for")
    assert payload["raw_content"] == "Tool abstract that still needs to be preserved."

