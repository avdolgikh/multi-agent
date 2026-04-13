"""Async agents that collaborate via the choreography event bus."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, cast
from uuid import uuid4

from pydantic import ValidationError

from core.agents import AgentResult, AgentTask, BaseAgent, Tool
from core.messaging import Message, MessageBus, Subscription
from core.resilience import DeadLetterQueue
from core.state import Event, EventStore
from core.tracing import inject_context

from . import events as research_events

__all__ = [
    "ResearchContext",
    "ResearchEventPublisher",
    "BaseChoreographyAgent",
    "InitiatorAgent",
    "WebSearchAgent",
    "AcademicSearchAgent",
    "CodeAnalysisAgent",
    "NewsSearchAgent",
    "CrossReferenceAgent",
    "AggregatorAgent",
    "DLQMonitorAgent",
]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResearchContext:
    research_id: str
    topic: str
    scope: str
    deadline: datetime | None
    trace_context: dict[str, Any]
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ResearchEventPublisher:
    """Helper that publishes research events and persists them."""

    def __init__(
        self, *, bus: MessageBus, event_store: EventStore, context: ResearchContext
    ) -> None:
        self._bus = bus
        self._event_store = event_store
        self._context = context
        self._stream = f"research:{context.research_id}"

    @property
    def context(self) -> ResearchContext:
        return self._context

    async def publish(
        self,
        event: research_events.ResearchEvent,
        *,
        trace_context: dict[str, Any] | None = None,
    ) -> None:
        trace = dict(trace_context or event.trace_context or self._context.trace_context)
        if "trace_id" not in trace:
            trace["trace_id"] = self._context.trace_context.get("trace_id", uuid4().hex)
        trace["span_id"] = uuid4().hex
        event.research_id = self._context.research_id
        event.trace_context = trace
        if not event.source_agent:
            event.source_agent = event.source_agent or ""
        event_data = event.model_dump(
            mode="json",
            exclude=research_events.ResearchEvent._base_exclusions,
        )
        if event.payload:
            event_data.update(event.payload)
        stored_event = Event(
            event_id=event.message_id,
            stream=self._stream,
            event_type=event.event_type,
            data=event_data,
            timestamp=event.timestamp,
            trace_context=trace,
        )
        await self._event_store.append(self._stream, stored_event)
        await self._bus.publish(event.topic, event)


class _BaseDomainSearchTool:
    """Simulated domain-specific search utility backing the research agents."""

    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search topic or query"},
            "limit": {
                "type": "integer",
                "description": "Maximum number of domain results",
                "minimum": 1,
            },
        },
        "required": ["query"],
    }

    def __init__(self, *, name: str, description: str) -> None:
        self.name = name
        self.description = description

    async def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query", "")).strip()
        if not query:
            raise ValueError(f"{self.name} requires a non-empty query")
        limit_raw = params.get("limit", 2) or 2
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = 2
        limit = max(1, min(limit, 5))
        await asyncio.sleep(0)
        return {
            "query": query,
            "results": [self._build_entry(query, index) for index in range(limit)],
        }

    def _build_entry(self, query: str, index: int) -> dict[str, Any]:
        raise NotImplementedError


class _WebDiscoveryTool(_BaseDomainSearchTool):
    def __init__(self) -> None:
        super().__init__(
            name="web_search",
            description="Search curated web sources for choreography and research topics.",
        )

    def _build_entry(self, query: str, index: int) -> dict[str, Any]:
        slug = query.lower().replace(" ", "-")
        return {
            "title": f"{query} insight {index + 1}",
            "snippet": f"Perspective {index + 1} on {query} adoption.",
            "url": f"https://research.local/{slug}/web/{index + 1}",
            "raw_content": f"Community coverage #{index + 1} exploring {query} in practice.",
        }


class _AcademicCorpusTool(_BaseDomainSearchTool):
    def __init__(self) -> None:
        super().__init__(
            name="academic_search",
            description="Search representative academic references for the topic.",
        )

    def _build_entry(self, query: str, index: int) -> dict[str, Any]:
        tokens = query.split()
        lead = tokens[0].title() if tokens else "Research"
        slug = query.lower().replace(" ", "-")
        base_year = datetime.now(timezone.utc).year
        return {
            "title": f"{query} empirical study {index + 1}",
            "abstract": f"Peer-reviewed findings on {query}, scenario {index + 1}.",
            "authors": [f"Dr. {lead} {index + 1}", "Prof. Parallel"],
            "year": base_year - index,
            "url": f"https://doi.org/10.1000/{slug}{index + 1}",
            "raw_content": f"Academic abstract describing {query} outcome {index + 1}.",
        }


class _RepositorySearchTool(_BaseDomainSearchTool):
    def __init__(self) -> None:
        super().__init__(
            name="code_search",
            description="Search open repositories exhibiting the research topic.",
        )

    def _build_entry(self, query: str, index: int) -> dict[str, Any]:
        languages = ("python", "rust", "typescript")
        language = languages[index % len(languages)]
        slug = query.lower().replace(" ", "-")
        return {
            "title": f"{query} reference implementation {index + 1}",
            "summary": f"Implementation {index + 1} demonstrating {query} patterns.",
            "repository": f"https://github.com/example/{slug}-{index + 1}",
            "language": language,
            "url": f"https://github.com/example/{slug}-{index + 1}#readme",
            "raw_content": f"README excerpt for {query} repository {index + 1}.",
        }


class _NewsScanTool(_BaseDomainSearchTool):
    def __init__(self) -> None:
        super().__init__(
            name="news_search",
            description="Scan recent news coverage related to the topic.",
        )

    def _build_entry(self, query: str, index: int) -> dict[str, Any]:
        slug = query.lower().replace(" ", "-")
        published = datetime.now(timezone.utc) - timedelta(hours=index * 3)
        return {
            "title": f"{query} headline {index + 1}",
            "summary": f"Recent coverage describing {query} trend {index + 1}.",
            "url": f"https://news.local/{slug}/{index + 1}",
            "published_date": published,
            "raw_content": f"News article synopsis for {query} headline {index + 1}.",
        }


class BaseChoreographyAgent(BaseAgent):
    """Base class that wires agents into the message bus."""

    def __init__(
        self,
        *,
        bus: MessageBus,
        publisher: ResearchEventPublisher | None,
        dead_letter_queue: DeadLetterQueue | None,
        logger: logging.Logger | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.bus = bus
        self.publisher = publisher
        self.dead_letter_queue = dead_letter_queue
        self._logger = logger or logging.getLogger(self.agent_id)
        self._subscriptions: list[Subscription] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        while self._subscriptions:
            subscription = self._subscriptions.pop()
            try:
                await self.bus.unsubscribe(subscription)
            except Exception:
                self._logger.debug("Failed to unsubscribe %s", subscription.topic, exc_info=True)

    async def _subscribe(
        self,
        event_cls: type[research_events.ResearchEvent],
        handler: Callable[[Any], Awaitable[None]],
    ) -> None:
        async def _invoke(message: Message) -> None:
            event: research_events.ResearchEvent
            if isinstance(message, event_cls):
                event = message
            else:
                try:
                    event = event_cls.model_validate(message.model_dump())
                except ValidationError as exc:  # pragma: no cover - defensive
                    self._logger.error("Invalid %s message: %s", event_cls.__name__, exc)
                    return
            try:
                await handler(event)
            except Exception as exc:  # noqa: BLE001
                await self._handle_processing_error(event, exc)

        subscription = await self.bus.subscribe(event_cls.topic_name(), _invoke)
        self._subscriptions.append(subscription)

    async def _handle_processing_error(self, message: Message, exc: Exception) -> None:
        self._logger.error("Agent %s failed: %s", self.agent_id, exc, exc_info=True)
        if self.dead_letter_queue is not None:
            await self.dead_letter_queue.send(message, error=str(exc), source=self.agent_id)
        if self.publisher is None:
            return
        agent_error = research_events.AgentError(
            research_id=self.publisher.context.research_id,
            agent_id=self.agent_id,
            error=str(exc),
            details={"event_type": getattr(message, "event_type", message.topic)},
            trace_context=message.trace_context or self.publisher.context.trace_context,
            source_agent=self.agent_id,
        )
        await self.publisher.publish(agent_error)

    def _require_publisher(self) -> "ResearchEventPublisher":
        if self.publisher is None:
            raise RuntimeError(f"{self.__class__.__name__} requires a publisher")
        return self.publisher


class InitiatorAgent(BaseChoreographyAgent):
    """Publishes a single ResearchRequested event and hands off control."""

    async def request_research(
        self,
        *,
        topic: str,
        scope: str,
        deadline: datetime | None,
    ) -> research_events.ResearchRequested:
        trace = inject_context() or self.publisher.context.trace_context if self.publisher else {}
        task = AgentTask(
            task_id=f"init-{self.agent_id}-{uuid4().hex}",
            input_data={"topic": topic, "scope": scope, "deadline": deadline},
            trace_context=trace,
        )
        result = await self.execute(task)
        payload = result.output_data.get("event")
        if isinstance(payload, research_events.ResearchRequested):
            return payload
        return research_events.ResearchRequested.model_validate(payload)

    async def execute(self, task: AgentTask) -> AgentResult:
        publisher = self._require_publisher()
        deadline = task.input_data.get("deadline")
        payload_data: dict[str, Any] = {
            "topic": task.input_data["topic"],
            "scope": task.input_data["scope"],
        }
        if deadline is not None:
            payload_data["deadline"] = deadline
        event = research_events.ResearchRequested(
            research_id=publisher.context.research_id,
            scope=task.input_data["scope"],
            deadline=deadline,
            trace_context=task.trace_context or publisher.context.trace_context,
            source_agent=self.agent_id,
            payload=payload_data,
        )
        await publisher.publish(event)
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data={"event": event},
            status="success",
            duration_ms=0.0,
            trace_context=event.trace_context,
        )


class SearchAgent(BaseChoreographyAgent):
    """Common behaviour for specialist research agents."""

    def __init__(
        self,
        *,
        source_type: research_events.SourceType,
        concurrency_offset_ms: int,
        search_tool: Tool,
        findings_limit: int = 2,
        **kwargs: Any,
    ) -> None:
        tools = list(kwargs.pop("tools", []))
        if search_tool not in tools:
            tools.append(search_tool)
        super().__init__(tools=tools, **kwargs)
        self.source_type: research_events.SourceType = source_type
        self._offset_ms = concurrency_offset_ms
        self._known_findings: set[str] = set()
        self._search_tool = search_tool
        self._search_limit = max(1, findings_limit)
        self._require_publisher()

    async def start(self) -> None:
        await super().start()
        await self._subscribe(research_events.ResearchRequested, self._handle_request)
        await self._subscribe(research_events.CrossReferenceFound, self._handle_cross_reference)

    async def execute(self, task: AgentTask) -> AgentResult:
        findings = await self._generate_findings(task)
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data={"findings": findings},
            status="success",
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )

    async def _handle_request(self, event: research_events.ResearchRequested) -> None:
        task = AgentTask(
            task_id=f"{self.agent_id}-{event.research_id}",
            input_data={
                "topic": self._resolve_topic(event),
                "scope": event.scope,
                "deadline": event.deadline,
            },
            trace_context=event.trace_context,
        )
        try:
            result = await self.execute(task)
        except Exception as exc:  # noqa: BLE001
            await self._handle_processing_error(event, exc)
            await self._publish_source_exhausted(available=False, reason=str(exc))
            return
        await self._publish_findings(event, result)

    async def _publish_findings(
        self,
        request: research_events.ResearchRequested,
        result: AgentResult,
    ) -> None:
        publisher = self._require_publisher()
        findings = result.output_data.get("findings", [])
        for index, payload in enumerate(findings):
            finding_event = research_events.FindingDiscovered(
                research_id=request.research_id,
                source_type=self.source_type,
                timestamp=self._timestamp_for(index),
                trace_context=request.trace_context,
                source_agent=self.agent_id,
                **payload,
            )
            await publisher.publish(finding_event)
            self._known_findings.add(finding_event.finding_id)
        await self._publish_source_exhausted(available=True, reason=None)

    async def _publish_source_exhausted(self, *, available: bool, reason: str | None) -> None:
        publisher = self._require_publisher()
        exhausted_event = research_events.SourceExhausted(
            research_id=publisher.context.research_id,
            source_type=self.source_type,
            available=available,
            reason=reason,
            timestamp=self._timestamp_for(5),
            trace_context=publisher.context.trace_context,
            source_agent=self.agent_id,
        )
        await publisher.publish(exhausted_event)

    async def _handle_cross_reference(self, event: research_events.CrossReferenceFound) -> None:
        if event.finding_a_id in self._known_findings or event.finding_b_id in self._known_findings:
            self._logger.debug(
                "Agent %s acknowledged cross reference %s ? %s",
                self.agent_id,
                event.finding_a_id,
                event.finding_b_id,
            )

    def _timestamp_for(self, index: int) -> datetime:
        publisher = self._require_publisher()
        base = publisher.context.started_at
        return base + timedelta(milliseconds=self._offset_ms + index * 25)

    async def _generate_findings(self, task: AgentTask) -> list[dict[str, Any]]:
        topic = task.input_data["topic"]
        entries = await self._run_domain_search(topic)
        if not entries:
            return []
        summary_text = await self._summarize_entries(topic, entries, task)
        findings: list[dict[str, Any]] = []
        for rank, entry in enumerate(entries):
            findings.append(
                self._build_finding_payload(
                    topic=topic,
                    entry=entry,
                    summary_text=summary_text,
                    rank=rank,
                )
            )
        return findings

    async def _run_domain_search(self, topic: str) -> list[dict[str, Any]]:
        if self._search_tool is None:
            return []
        params = {
            "query": topic,
            "limit": self._search_limit,
            "source": self.source_type,
        }
        result = await self._search_tool.execute(params)
        raw_entries = result.get("results", [])
        entries = [entry for entry in raw_entries if isinstance(entry, dict)]
        return entries[: self._search_limit]

    async def _summarize_entries(
        self,
        topic: str,
        entries: list[dict[str, Any]],
        task: AgentTask,
    ) -> str:
        prompt = self._build_summary_prompt(topic, entries)
        message = Message(
            message_id=f"{self.agent_id}-summary-{uuid4().hex}",
            topic=f"{self.agent_id}.summary",
            payload={"role": "user", "content": prompt},
            timestamp=datetime.now(timezone.utc),
            trace_context=task.trace_context or {},
            source_agent=self.agent_id,
        )
        try:
            response = await self.call_llm([message])
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("LLM summary failed for %s: %s", self.agent_id, exc)
            return self._fallback_summary_text(topic, entries)
        content = response.content.strip()
        return content or self._fallback_summary_text(topic, entries)

    def _build_summary_prompt(self, topic: str, entries: list[dict[str, Any]]) -> str:
        lines = [
            f"Topic: {topic}",
            "Summarize the following domain findings in two sentences, calling out corroboration and gaps:",
        ]
        for entry in entries:
            title = entry.get("title") or "Untitled insight"
            snippet = entry.get("snippet") or entry.get("summary") or entry.get("raw_content", "")
            lines.append(f"- {title}: {snippet}")
        return "\n".join(lines)

    def _fallback_summary_text(self, topic: str, entries: list[dict[str, Any]]) -> str:
        titles = ", ".join(entry.get("title", "insight").strip() or "insight" for entry in entries)
        return f"Findings for {topic} highlight: {titles}."

    def _build_finding_payload(
        self,
        *,
        topic: str,
        entry: dict[str, Any],
        summary_text: str,
        rank: int,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _resolve_topic(self, event: research_events.ResearchRequested) -> str:
        topic_value = event.payload.get("topic")
        if isinstance(topic_value, str) and topic_value:
            return topic_value
        publisher = self._require_publisher()
        return publisher.context.topic


class WebSearchAgent(SearchAgent):
    def __init__(self, *, search_tool: Tool | None = None, **kwargs: Any) -> None:
        super().__init__(
            source_type=cast(research_events.SourceType, "web"),
            concurrency_offset_ms=0,
            search_tool=search_tool or _WebDiscoveryTool(),
            findings_limit=2,
            agent_id="web-search",
            name="Web Search Agent",
            model="qwen3-coder:latest",
            provider="ollama",
            system_prompt="Summarize recent web findings.",
            **kwargs,
        )

    def _build_finding_payload(
        self,
        *,
        topic: str,
        entry: dict[str, Any],
        summary_text: str,
        rank: int,
    ) -> dict[str, Any]:
        snippet = entry.get("snippet") or entry.get("summary") or summary_text
        title = entry.get("title") or f"{topic} web insight"
        url = entry.get("url", f"https://research.local/{topic.lower().replace(' ', '-')}/web")
        raw_content = entry.get("raw_content") or snippet
        relevance = round(max(0.6, 0.92 - 0.08 * rank), 2)
        summary = f"{summary_text} {snippet}".strip()
        return {
            "title": title,
            "summary": summary,
            "url": url,
            "relevance_score": relevance,
            "raw_content": raw_content,
        }


class AcademicSearchAgent(SearchAgent):
    def __init__(self, *, search_tool: Tool | None = None, **kwargs: Any) -> None:
        super().__init__(
            source_type=cast(research_events.SourceType, "academic"),
            concurrency_offset_ms=30,
            search_tool=search_tool or _AcademicCorpusTool(),
            findings_limit=1,
            agent_id="academic-search",
            name="Academic Scholar Agent",
            model="qwen3.5:latest",
            provider="ollama",
            system_prompt="Summarize academic findings.",
            **kwargs,
        )

    def _build_finding_payload(
        self,
        *,
        topic: str,
        entry: dict[str, Any],
        summary_text: str,
        rank: int,
    ) -> dict[str, Any]:
        authors = [str(author) for author in entry.get("authors", [])] or ["Dr. Parallel"]
        year = int(entry.get("year") or datetime.now(timezone.utc).year)
        abstract = entry.get("abstract") or entry.get("raw_content") or summary_text
        url = entry.get("url", "https://doi.org/10.1000/choreo")
        relevance = round(max(0.7, 0.95 - 0.1 * rank), 2)
        return {
            "title": entry.get("title") or f"{topic} empirical study",
            "summary": abstract,
            "url": url,
            "relevance_score": relevance,
            "raw_content": abstract,
            "authors": authors,
            "year": year,
        }


class CodeAnalysisAgent(SearchAgent):
    def __init__(self, *, search_tool: Tool | None = None, **kwargs: Any) -> None:
        super().__init__(
            source_type=cast(research_events.SourceType, "code"),
            concurrency_offset_ms=60,
            search_tool=search_tool or _RepositorySearchTool(),
            findings_limit=1,
            agent_id="code-search",
            name="Code Analysis Agent",
            model="qwen3-coder:latest",
            provider="ollama",
            system_prompt="Inspect repositories related to the topic.",
            **kwargs,
        )

    def _build_finding_payload(
        self,
        *,
        topic: str,
        entry: dict[str, Any],
        summary_text: str,
        rank: int,
    ) -> dict[str, Any]:
        repository = entry.get("repository") or "https://github.com/example/reference"
        language = entry.get("language") or "python"
        summary = entry.get("summary") or summary_text
        relevance = round(max(0.7, 0.9 - 0.1 * rank), 2)
        return {
            "title": entry.get("title") or f"{topic} reference implementation",
            "summary": summary,
            "url": entry.get("url", repository),
            "relevance_score": relevance,
            "raw_content": entry.get("raw_content") or summary,
            "repository": repository,
            "language": language,
        }


class NewsSearchAgent(SearchAgent):
    def __init__(self, *, search_tool: Tool | None = None, **kwargs: Any) -> None:
        super().__init__(
            source_type=cast(research_events.SourceType, "news"),
            concurrency_offset_ms=90,
            search_tool=search_tool or _NewsScanTool(),
            findings_limit=1,
            agent_id="news-search",
            name="News Scanner Agent",
            model="glm-4.7-flash:latest",
            provider="ollama",
            system_prompt="Track current news topics.",
            **kwargs,
        )

    def _build_finding_payload(
        self,
        *,
        topic: str,
        entry: dict[str, Any],
        summary_text: str,
        rank: int,
    ) -> dict[str, Any]:
        published = entry.get("published_date")
        if not isinstance(published, datetime):
            published = datetime.now(timezone.utc)
        summary = entry.get("summary") or summary_text
        relevance = round(max(0.65, 0.9 - 0.1 * rank), 2)
        return {
            "title": entry.get("title") or f"{topic} news",
            "summary": summary,
            "url": entry.get("url", "https://news.local"),
            "relevance_score": relevance,
            "raw_content": entry.get("raw_content") or summary,
            "published_date": published,
        }


class CrossReferenceAgent(BaseChoreographyAgent):
    """Compares findings from different sources."""

    def __init__(self, *, expected_sources: Iterable[str], **kwargs: Any) -> None:
        super().__init__(
            agent_id="cross-reference",
            name="Cross Reference Agent",
            model="qwen3.5:latest",
            provider="ollama",
            system_prompt="Connect semantically similar findings.",
            tools=[],
            **kwargs,
        )
        self._findings: dict[str, research_events.FindingDiscovered] = {}
        self._linked_pairs: set[tuple[str, str]] = set()
        self._pending_findings = 0
        self._expected_sources = {source for source in expected_sources}
        self._exhausted_sources: set[str] = set()
        self._finding_queue: asyncio.Queue[research_events.FindingDiscovered] = asyncio.Queue()
        self._finding_task: asyncio.Task[None] | None = None
        self._require_publisher()

    async def start(self) -> None:
        await super().start()
        await self._subscribe(research_events.FindingDiscovered, self._enqueue_finding)
        await self._subscribe(research_events.SourceExhausted, self._handle_source_exhausted)
        self._finding_task = asyncio.create_task(self._process_findings())

    async def stop(self) -> None:
        if self._finding_task is not None:
            self._finding_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._finding_task
            self._finding_task = None
        await super().stop()

    async def execute(self, task: AgentTask) -> AgentResult:
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data={},
            status="success",
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )

    async def _enqueue_finding(self, event: research_events.FindingDiscovered) -> None:
        publish_busy = self._mark_busy()
        if publish_busy:
            await self._publish_status(is_idle=False)
        await self._finding_queue.put(event)

    async def _process_findings(self) -> None:
        while True:
            event = await self._finding_queue.get()
            try:
                await self._process_finding(event)
            finally:
                self._finding_queue.task_done()
                await self._mark_idle()

    async def _process_finding(self, event: research_events.FindingDiscovered) -> None:
        for previous in self._findings.values():
            if previous.source_type == event.source_type:
                continue
            pair = cast(
                tuple[str, str],
                tuple(sorted((previous.finding_id, event.finding_id))),
            )
            if pair in self._linked_pairs:
                continue
            if self._share_keywords(previous, event):
                await self._publish_cross_reference(previous, event)
                self._linked_pairs.add(pair)
                break
        self._findings[event.finding_id] = event

    async def _handle_source_exhausted(self, event: research_events.SourceExhausted) -> None:
        self._exhausted_sources.add(event.source_type)
        if self._pending_findings == 0:
            await self._publish_status()

    def _share_keywords(
        self,
        a: research_events.FindingDiscovered,
        b: research_events.FindingDiscovered,
    ) -> bool:
        def tokenize(text: str) -> set[str]:
            return {token.lower() for token in text.split() if len(token) > 4}

        text_a = f"{a.title} {a.summary}"
        text_b = f"{b.title} {b.summary}"
        return bool(tokenize(text_a) & tokenize(text_b))

    async def _publish_cross_reference(
        self,
        a: research_events.FindingDiscovered,
        b: research_events.FindingDiscovered,
    ) -> None:
        publisher = self._require_publisher()
        explanation = (
            f"{a.source_type.title()} and {b.source_type.title()} findings both describe "
            f"{a.title.lower()}."
        )
        event = research_events.CrossReferenceFound(
            research_id=publisher.context.research_id,
            finding_a_id=a.finding_id,
            finding_b_id=b.finding_id,
            relationship="corroborates",
            explanation=explanation,
            trace_context=a.trace_context or publisher.context.trace_context,
            source_agent=self.agent_id,
        )
        await publisher.publish(event)

    def _mark_busy(self) -> bool:
        self._pending_findings += 1
        return self._pending_findings == 1

    async def _mark_idle(self) -> None:
        self._pending_findings = max(0, self._pending_findings - 1)
        if self._pending_findings == 0:
            await self._publish_status()

    async def _publish_status(self, *, is_idle: bool | None = None) -> None:
        publisher = self._require_publisher()
        pending = max(0, self._pending_findings)
        status = research_events.CrossReferenceStatus(
            research_id=publisher.context.research_id,
            pending_findings=pending,
            is_idle=pending == 0 if is_idle is None else is_idle,
            all_sources_exhausted=self._all_sources_exhausted(),
            trace_context=publisher.context.trace_context,
            source_agent=self.agent_id,
        )
        await self.bus.publish(status.topic, status)

    def _all_sources_exhausted(self) -> bool:
        if not self._expected_sources:
            return True
        return self._expected_sources.issubset(self._exhausted_sources)


class AggregatorAgent(BaseChoreographyAgent):
    """Synthesizes the final research brief once all sources finish."""

    def __init__(
        self,
        *,
        expected_sources: Iterable[str],
        completion_future: asyncio.Future[research_events.ResearchComplete] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.expected_sources = {source for source in expected_sources}
        loop = asyncio.get_running_loop()
        self._completion_future = completion_future or loop.create_future()
        self._cross_reference_idle = False
        self._cross_reference_sources_complete = False
        self._findings: list[research_events.FindingDiscovered] = []
        self._cross_references: list[research_events.CrossReferenceFound] = []
        self._source_counts: defaultdict[str, int] = defaultdict(int)
        self._exhausted_sources: set[str] = set()
        self._availability: dict[str, bool] = {}
        self._completed = False
        self._require_publisher()

    async def start(self) -> None:
        await super().start()
        await self._subscribe(research_events.FindingDiscovered, self._record_finding)
        await self._subscribe(research_events.CrossReferenceFound, self._record_cross_reference)
        await self._subscribe(research_events.SourceExhausted, self._record_exhaustion)
        await self._subscribe(
            research_events.CrossReferenceStatus, self._record_cross_reference_status
        )

    async def execute(self, task: AgentTask) -> AgentResult:
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data={},
            status="success",
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )

    async def wait_for_completion(self) -> research_events.ResearchComplete:
        return await self._completion_future

    async def _record_finding(self, event: research_events.FindingDiscovered) -> None:
        self._findings.append(event)
        self._source_counts[event.source_type] += 1

    async def _record_cross_reference(self, event: research_events.CrossReferenceFound) -> None:
        self._cross_references.append(event)

    async def _record_exhaustion(self, event: research_events.SourceExhausted) -> None:
        self._exhausted_sources.add(event.source_type)
        self._availability[event.source_type] = event.available
        await self._maybe_finalize()

    async def _record_cross_reference_status(
        self, event: research_events.CrossReferenceStatus
    ) -> None:
        self._cross_reference_idle = event.is_idle
        self._cross_reference_sources_complete = event.all_sources_exhausted
        if self._cross_reference_idle and self._cross_reference_sources_complete:
            await self._maybe_finalize()

    async def _maybe_finalize(self) -> None:
        if self._completed:
            return
        if not self.expected_sources.issubset(self._exhausted_sources):
            return
        if not self._cross_reference_idle or not self._cross_reference_sources_complete:
            return
        await asyncio.sleep(0)
        publisher = self._require_publisher()
        summary = await self._synthesize_summary()
        brief = research_events.ResearchBrief(
            topic=publisher.context.topic,
            summary=summary,
            key_findings=[
                research_events.FindingSummary(
                    finding_id=item.finding_id,
                    source_type=item.source_type,
                    title=item.title,
                    summary=item.summary,
                    url=item.url,
                )
                for item in self._findings
            ],
            cross_references=[
                research_events.CrossReferenceSummary(
                    finding_a_id=item.finding_a_id,
                    finding_b_id=item.finding_b_id,
                    relationship=item.relationship,
                    explanation=item.explanation,
                )
                for item in self._cross_references
            ],
            sources_consulted={
                source: self._source_counts.get(source, 0)
                for source in sorted(self.expected_sources)
            },
            confidence_score=self._confidence_score(),
        )
        complete_event = research_events.ResearchComplete(
            research_id=publisher.context.research_id,
            brief=brief,
            trace_context=publisher.context.trace_context,
            source_agent=self.agent_id,
            timestamp=self._completion_timestamp(),
        )
        await publisher.publish(complete_event)
        self._completed = True
        if not self._completion_future.done():
            self._completion_future.set_result(complete_event)

    def _confidence_score(self) -> float:
        if not self.expected_sources:
            return 1.0
        available = sum(1 for src in self.expected_sources if self._availability.get(src, True))
        return round(available / len(self.expected_sources), 2)

    async def _synthesize_summary(self) -> str:
        publisher = self._require_publisher()
        missing = self._missing_sources()
        prompt = self._build_summary_prompt(publisher.context.topic, missing)
        message = Message(
            message_id=f"{self.agent_id}-brief-{uuid4().hex}",
            topic=f"{self.agent_id}.brief",
            payload={"role": "user", "content": prompt},
            timestamp=datetime.now(timezone.utc),
            trace_context=publisher.context.trace_context,
            source_agent=self.agent_id,
        )
        summary = ""
        try:
            response = await self.call_llm([message])
            summary = response.content.strip()
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("Aggregator summary generation failed: %s", exc)
        if not summary:
            summary = self._fallback_summary(publisher.context.topic, missing)
        elif missing:
            summary = summary.rstrip(".") + f". Sources unavailable: {', '.join(sorted(missing))}."
        return summary

    def _build_summary_prompt(self, topic: str, missing: list[str]) -> str:
        lines = [
            f"Topic: {topic}",
            "Produce a concise 2-3 sentence research brief highlighting coverage, corroborations, and gaps.",
            "Findings:",
        ]
        if self._findings:
            for finding in self._findings:
                lines.append(f"- [{finding.source_type}] {finding.title}: {finding.summary}")
        else:
            lines.append("- No findings were reported.")
        if self._cross_references:
            lines.append("Cross references:")
            for ref in self._cross_references:
                lines.append(
                    f"- {ref.relationship} between {ref.finding_a_id} and {ref.finding_b_id}: {ref.explanation}"
                )
        if missing:
            lines.append(f"Sources unavailable: {', '.join(sorted(missing))}")
        return "\n".join(lines)

    def _fallback_summary(self, topic: str, missing: list[str]) -> str:
        summary = (
            f"Synthesized {len(self._findings)} findings for {topic} "
            f"across {len(self.expected_sources)} sources."
        )
        if missing:
            summary += f" Sources unavailable: {', '.join(sorted(missing))}."
        return summary

    def _missing_sources(self) -> list[str]:
        return [src for src in self.expected_sources if not self._availability.get(src, True)]

    def _completion_timestamp(self) -> datetime:
        publisher = self._require_publisher()
        return publisher.context.started_at + timedelta(milliseconds=1000)


class DLQMonitorAgent:
    """Listens for AgentError events and logs their context."""

    def __init__(
        self,
        *,
        bus: MessageBus | None = None,
        message_bus: MessageBus | None = None,
        event_bus: MessageBus | None = None,
        dead_letter_queue: DeadLetterQueue | None = None,
        dlq: DeadLetterQueue | None = None,
        logger: logging.Logger | None = None,
        log: logging.Logger | None = None,
        agent_id: str = "dlq-monitor",
        name: str = "DLQ Monitor",
        model: str = "monitor",
        provider: str = "ollama",
        **_: Any,
    ) -> None:
        self.agent_id = agent_id
        self.name = name
        self.model = model
        self.provider = provider
        resolved_bus = bus or message_bus or event_bus
        if resolved_bus is None:
            raise ValueError("A message bus is required for DLQMonitorAgent")
        self.bus: MessageBus = resolved_bus
        self.dead_letter_queue = dead_letter_queue or dlq
        self.logger = logger or log or logging.getLogger(self.agent_id)
        self._subscription: Subscription | None = None

    async def start(self) -> None:
        async def _handle(message: Message) -> None:
            if isinstance(message, research_events.AgentError):
                event = message
            else:
                try:
                    event = research_events.AgentError.model_validate(message.model_dump())
                except ValidationError as exc:  # pragma: no cover - defensive
                    self.logger.error("Failed to parse AgentError: %s", exc)
                    return
            self.logger.error(
                f"Agent {event.agent_id} reported error: {event.error}",
                extra={
                    "agent_id": event.agent_id,
                    "error": event.error,
                    "details": event.details,
                },
            )

        self._subscription = await self.bus.subscribe(
            research_events.AgentError.topic_name(),
            _handle,
        )

    async def stop(self) -> None:
        if self._subscription is None:
            return
        await self.bus.unsubscribe(self._subscription)
        self._subscription = None
