from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.context.context import Context as _OtelContext

from core.agents import AgentResult, AgentTask, BaseAgent
from core.messaging import Message, MessageBus
from core.state import Event, EventStore
from core.tracing import inject_context, traced

from .events import TEAM_COMPLETE_TOPIC, TeamCompleteEvent as _TeamCompleteEvent, build_team_topic
from .models import AgentOutput as _AgentOutput, TeamResult as _TeamResult

__all__ = ["Team"]


def _context_trace_id(context: _OtelContext) -> int:
    return trace.get_current_span(context).get_span_context().trace_id


if not hasattr(_OtelContext, "trace_id"):
    setattr(_OtelContext, "trace_id", property(_context_trace_id))


class Team:
    def __init__(
        self,
        *,
        name: str,
        agents: Sequence[BaseAgent],
        bus: MessageBus,
        event_store: EventStore,
        aggregator: Callable[[list[Any]], dict[str, Any]],
        tracer_provider: Any | None = None,
    ) -> None:
        self.name = name
        self.agents: list[BaseAgent] = list(agents)
        self.bus = bus
        self.event_store = event_store
        self.aggregator = aggregator
        if tracer_provider is not None:
            current = trace.get_tracer_provider()
            if current is not tracer_provider:
                trace.set_tracer_provider(tracer_provider)

    @traced
    async def run(self, task: AgentTask) -> _TeamResult:
        child_trace_context = task.trace_context or inject_context()
        member_tasks = [
            self._build_member_task(task=task, agent=agent, trace_context=child_trace_context)
            for agent in self.agents
        ]
        results = await asyncio.gather(
            *[
                self._run_member(agent=agent, member_task=member_task)
                for agent, member_task in zip(self.agents, member_tasks, strict=False)
            ]
        )
        merged = self._coerce_aggregated_result(self.aggregator(list(results)))
        agent_outputs = [
            _AgentOutput(
                agent_id=result.agent_id,
                output=dict(result.output_data),
                status=result.status,
                error=result.error,
            )
            for result in results
        ]
        failures = [
            output.error or output.status
            for output in agent_outputs
            if str(output.status).lower() == "failure" or output.error
        ]

        stream = build_team_topic(self.name, "members")
        for output in agent_outputs:
            event = Event(
                event_id=str(uuid4()),
                stream=stream,
                event_type="team.member.completed",
                data=output.model_dump(exclude_none=True),
                timestamp=datetime.now(timezone.utc),
                trace_context=child_trace_context,
            )
            await self.event_store.append(stream, event)

        completion = _TeamCompleteEvent(
            team_name=self.name,
            result=merged,
            agent_outputs=[output.model_dump(exclude_none=True) for output in agent_outputs],
            failures=failures,
        )
        completion_message = Message(
            message_id=str(uuid4()),
            topic=TEAM_COMPLETE_TOPIC,
            payload=completion.model_dump(),
            timestamp=datetime.now(timezone.utc),
            trace_context=child_trace_context,
            source_agent=self.name,
        )
        await self.bus.publish(TEAM_COMPLETE_TOPIC, completion_message)
        return _TeamResult(
            team_name=self.name,
            result=merged,
            agent_outputs=agent_outputs,
            failures=failures,
        )

    async def _run_member(self, *, agent: BaseAgent, member_task: AgentTask) -> AgentResult:
        try:
            return await agent.execute(member_task)
        except Exception as exc:  # noqa: BLE001
            return AgentResult(
                task_id=member_task.task_id,
                agent_id=getattr(agent, "agent_id", agent.__class__.__name__),
                output_data={},
                status="failure",
                error=str(exc),
                duration_ms=0.0,
                trace_context=member_task.trace_context or {},
            )

    def _build_member_task(
        self,
        *,
        task: AgentTask,
        agent: BaseAgent,
        trace_context: dict[str, Any],
    ) -> AgentTask:
        metadata = dict(task.metadata)
        metadata["team"] = self.name
        return AgentTask(
            task_id=f"{task.task_id}:{getattr(agent, 'agent_id', agent.__class__.__name__)}",
            input_data=dict(task.input_data),
            metadata=metadata,
            trace_context=dict(trace_context),
        )

    def _coerce_aggregated_result(self, value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        if hasattr(value, "model_dump"):
            dumped = value.model_dump()
            if isinstance(dumped, Mapping):
                return dict(dumped)
        return {"result": value}
