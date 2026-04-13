"""Entry point for driving the choreography research system."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence
from uuid import uuid4

from core.messaging import InMemoryBus, Message, MessageBus, Subscription
from core.resilience import DeadLetterQueue
from core.state import EventStore

from . import events as research_events
from .agents import (
    AcademicSearchAgent,
    AggregatorAgent,
    BaseChoreographyAgent,
    CodeAnalysisAgent,
    CrossReferenceAgent,
    DLQMonitorAgent,
    InitiatorAgent,
    NewsSearchAgent,
    ResearchContext,
    ResearchEventPublisher,
    WebSearchAgent,
)
from .event_log import EVENT_STORE as GLOBAL_EVENT_STORE

__all__ = ["ResearchRunner", "main"]

logger = logging.getLogger(__name__)


class ResearchRunner:
    """Coordinates agent start-up and waits for the aggregated brief."""

    _EXPECTED_SOURCES = ("academic", "code", "news", "web")

    def __init__(
        self,
        *,
        bus: MessageBus | None = None,
        event_store: EventStore | None = None,
        dead_letter_queue: DeadLetterQueue | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.bus = bus or InMemoryBus()
        self.event_store = event_store or GLOBAL_EVENT_STORE
        self.dead_letter_queue = dead_letter_queue or DeadLetterQueue(bus=self.bus)
        self.logger = logger or logging.getLogger(__name__)

    async def run(
        self,
        *,
        topic: str,
        scope: str = "global",
        deadline: datetime | None = None,
    ) -> research_events.ResearchComplete:
        research_id = f"research-{uuid4().hex}"
        trace_context = {"trace_id": uuid4().hex, "span_id": uuid4().hex}
        context = ResearchContext(
            research_id=research_id,
            topic=topic,
            scope=scope,
            deadline=deadline,
            trace_context=trace_context,
            started_at=datetime.now(timezone.utc),
        )
        publisher = ResearchEventPublisher(
            bus=self.bus, event_store=self.event_store, context=context
        )
        loop = asyncio.get_running_loop()
        completion_future: asyncio.Future[research_events.ResearchComplete] = loop.create_future()

        monitor = DLQMonitorAgent(
            bus=self.bus,
            dead_letter_queue=self.dead_letter_queue,
            logger=self.logger,
        )
        agents: list[BaseChoreographyAgent] = [
            AggregatorAgent(
                bus=self.bus,
                publisher=publisher,
                dead_letter_queue=self.dead_letter_queue,
                expected_sources=self._EXPECTED_SOURCES,
                completion_future=completion_future,
                agent_id="aggregator",
                name="Aggregator Agent",
                model="qwen3.5:latest",
                provider="ollama",
                system_prompt="Summarize research findings.",
                tools=[],
            ),
            CrossReferenceAgent(
                bus=self.bus,
                publisher=publisher,
                dead_letter_queue=self.dead_letter_queue,
                expected_sources=self._EXPECTED_SOURCES,
            ),
            WebSearchAgent(
                bus=self.bus,
                publisher=publisher,
                dead_letter_queue=self.dead_letter_queue,
            ),
            AcademicSearchAgent(
                bus=self.bus,
                publisher=publisher,
                dead_letter_queue=self.dead_letter_queue,
            ),
            CodeAnalysisAgent(
                bus=self.bus,
                publisher=publisher,
                dead_letter_queue=self.dead_letter_queue,
            ),
            NewsSearchAgent(
                bus=self.bus,
                publisher=publisher,
                dead_letter_queue=self.dead_letter_queue,
            ),
        ]
        initiator = InitiatorAgent(
            bus=self.bus,
            publisher=publisher,
            dead_letter_queue=self.dead_letter_queue,
            agent_id="initiator",
            name="Initiator Agent",
            model="qwen3.5:latest",
            provider="ollama",
            system_prompt="Kick off research requests.",
            tools=[],
        )

        started_agents: list[BaseChoreographyAgent] = []
        try:
            for agent in agents:
                await agent.start()
                started_agents.append(agent)
            await monitor.start()
            await initiator.request_research(topic=topic, scope=scope, deadline=deadline)
            completion_event = await completion_future
            return completion_event
        finally:
            await monitor.stop()
            await asyncio.gather(
                *(agent.stop() for agent in started_agents), return_exceptions=True
            )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the choreography research system.")
    parser.add_argument("topic", help="Research topic to investigate")
    parser.add_argument("--scope", default="global", help="Optional scope description")
    parser.add_argument(
        "--deadline-minutes",
        type=int,
        default=60,
        help="Approximate deadline offset in minutes",
    )
    args = parser.parse_args(argv)
    deadline: datetime | None = None
    if args.deadline_minutes:
        deadline = datetime.now(timezone.utc) + timedelta(minutes=args.deadline_minutes)
    runner = ResearchRunner()

    async def _run_with_streaming() -> research_events.ResearchComplete:
        subscription: Subscription | None = None
        bus: Any = getattr(runner, "bus", None)
        can_stream = hasattr(bus, "subscribe") and hasattr(bus, "unsubscribe")

        async def _handle_finding(message: Message) -> None:
            if isinstance(message, research_events.FindingDiscovered):
                finding = message
            else:
                finding = research_events.FindingDiscovered.model_validate(message.model_dump())
            timestamp = finding.timestamp.astimezone(timezone.utc).strftime("%H:%M:%S")
            title = finding.title.strip()
            summary = finding.summary.strip()
            print(
                f"[{timestamp}] ({finding.source_type}) {title} - {summary}",
                flush=True,
            )

        try:
            if can_stream:
                subscription = await bus.subscribe(
                    research_events.FindingDiscovered.topic_name(),
                    _handle_finding,
                )
                print("Streaming findings as they arrive...\n", flush=True)
            return await runner.run(topic=args.topic, scope=args.scope, deadline=deadline)
        finally:
            if subscription is not None and can_stream:
                await bus.unsubscribe(subscription)

    try:
        completion_event = asyncio.run(_run_with_streaming())
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}")
        raise SystemExit(1) from exc

    brief = completion_event.brief
    print(f"Research topic: {brief.topic}")
    print(f"Summary: {brief.summary}")
    print("Sources consulted:")
    for source, count in brief.sources_consulted.items():
        print(f" - {source}: {count}")
    raise SystemExit(0)
