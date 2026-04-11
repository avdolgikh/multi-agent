from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

__all__ = ["Message", "MessageBus", "InMemoryBus", "Subscription"]


class Message(BaseModel):
    message_id: str
    topic: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime
    trace_context: dict = Field(default_factory=dict)
    source_agent: str | None = None


Handler = Callable[[Message], Awaitable[Any | None]]


@dataclass(slots=True)
class Subscription:
    topic: str
    subscription_id: str
    handler: Handler


class MessageBus(Protocol):
    async def publish(self, topic: str, message: Message) -> None: ...

    async def subscribe(self, topic: str, handler: Handler) -> Subscription: ...

    async def request(self, topic: str, message: Message, timeout: float) -> Message: ...

    async def unsubscribe(self, subscription: Subscription) -> None: ...


@dataclass(slots=True)
class _QueueItem:
    message: Message
    reply_future: asyncio.Future[Any] | None = None


@dataclass(slots=True)
class _Subscriber:
    handler: Handler
    queue: asyncio.Queue[_QueueItem]
    task: asyncio.Task[None]


class InMemoryBus(MessageBus):
    def __init__(self) -> None:
        self._subscribers: dict[str, dict[str, _Subscriber]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, topic: str, message: Message) -> None:
        subscribers = await self._snapshot_subscribers(topic)
        if not subscribers:
            return
        await asyncio.gather(
            *(subscriber.queue.put(_QueueItem(message=message)) for subscriber in subscribers)
        )

    async def subscribe(self, topic: str, handler: Handler) -> Subscription:
        subscription = Subscription(topic=topic, subscription_id=str(uuid4()), handler=handler)
        queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
        subscriber = _Subscriber(
            handler=handler,
            queue=queue,
            task=asyncio.create_task(
                self._run_subscription(subscription.subscription_id, queue, handler)
            ),
        )
        async with self._lock:
            self._subscribers.setdefault(topic, {})[subscription.subscription_id] = subscriber
        return subscription

    async def request(self, topic: str, message: Message, timeout: float) -> Message:
        subscribers = await self._snapshot_subscribers(topic)
        if not subscribers:
            raise TimeoutError(f"No subscribers for topic {topic}")
        loop = asyncio.get_running_loop()
        reply_future: asyncio.Future[Any] = loop.create_future()
        for subscriber in subscribers:
            await subscriber.queue.put(_QueueItem(message=message, reply_future=reply_future))
        try:
            reply = await asyncio.wait_for(asyncio.shield(reply_future), timeout=timeout)
        except asyncio.TimeoutError as exc:
            reply_future.cancel()
            raise asyncio.TimeoutError(f"Timed out waiting for reply on {topic}") from exc
        if not isinstance(reply, Message):
            raise TypeError("Request handlers must return Message instances")
        return reply

    async def unsubscribe(self, subscription: Subscription) -> None:
        subscriber: _Subscriber | None = None
        async with self._lock:
            topic_subs = self._subscribers.get(subscription.topic)
            if topic_subs:
                subscriber = topic_subs.pop(subscription.subscription_id, None)
                if not topic_subs:
                    self._subscribers.pop(subscription.topic, None)
        if subscriber is None:
            return
        subscriber.task.cancel()
        with suppress(asyncio.CancelledError):
            await subscriber.task

    async def _snapshot_subscribers(self, topic: str) -> list[_Subscriber]:
        async with self._lock:
            subscribers = list(self._subscribers.get(topic, {}).values())
        return subscribers

    async def _run_subscription(
        self,
        subscription_id: str,
        queue: asyncio.Queue[_QueueItem],
        handler: Handler,
    ) -> None:
        try:
            while True:
                item = await queue.get()
                if item.reply_future and item.reply_future.done():
                    queue.task_done()
                    continue
                try:
                    result = await handler(item.message)
                except Exception as exc:  # noqa: BLE001
                    if item.reply_future and not item.reply_future.done():
                        item.reply_future.set_exception(exc)
                    else:
                        self._report_handler_error(subscription_id, exc)
                else:
                    if item.reply_future and not item.reply_future.done():
                        item.reply_future.set_result(result)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            self._drain_queue(queue)
            raise

    def _drain_queue(self, queue: asyncio.Queue[_QueueItem]) -> None:
        while not queue.empty():
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item.reply_future and not item.reply_future.done():
                item.reply_future.set_exception(RuntimeError("Subscription cancelled"))
            queue.task_done()

    def _report_handler_error(self, subscription_id: str, exc: BaseException) -> None:
        loop = asyncio.get_running_loop()
        loop.call_exception_handler(
            {
                "message": "Unhandled exception in InMemoryBus subscriber",
                "exception": exc,
                "subscription_id": subscription_id,
            }
        )
