from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.agents import AgentResult, AgentTask, BaseAgent
from core.messaging import MessageBus
from core.state import EventStore
from core.tracing import traced

from .team import Team

__all__ = ["StubAgent", "make_stub_team"]


class StubAgent(BaseAgent):
    def __init__(
        self,
        *,
        agent_id: str,
        name: str,
        model: str = "stub-model",
        provider: str = "ollama",
        canned_output: Mapping[str, Any] | None = None,
        fail: bool = False,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            name=name,
            model=model,
            provider=provider,
            tools=[],
            system_prompt="",
            base_url=None,
        )
        self._canned_output = dict(canned_output or {})
        self._fail = fail

    @traced
    async def execute(self, task: AgentTask) -> AgentResult:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self._fail:
            return AgentResult(
                task_id=task.task_id,
                agent_id=self.agent_id,
                output_data={},
                status="failure",
                error="boom",
                duration_ms=0.0,
                trace_context=task.trace_context or {},
            )
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data=dict(self._canned_output),
            status="success",
            error=None,
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )


def make_stub_team(
    *,
    name: str,
    agent_pairs: list[tuple[str, Mapping[str, Any]]],
    bus: MessageBus,
    event_store: EventStore,
    tracer_provider: Any | None = None,
) -> Team:
    agents = [
        StubAgent(
            agent_id=agent_id,
            name=agent_id,
            canned_output=canned_output,
        )
        for agent_id, canned_output in agent_pairs
    ]

    def _merge(outputs: list[Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for output in outputs:
            if isinstance(output, AgentResult):
                merged.update(output.output_data)
                continue
            if isinstance(output, Mapping):
                merged.update(output)
                continue
            if hasattr(output, "model_dump"):
                dumped = output.model_dump()
                if isinstance(dumped, Mapping):
                    merged.update(dumped)
        return merged

    return Team(
        name=name,
        agents=agents,
        bus=bus,
        event_store=event_store,
        aggregator=_merge,
        tracer_provider=tracer_provider,
    )
