from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuntimeState:
    token_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    compress_locks: dict[int, asyncio.Lock] = field(default_factory=dict)
    state_locks: dict[int, asyncio.Lock] = field(default_factory=dict)
    chat_locks: dict[int, asyncio.Lock] = field(default_factory=dict)
    observer_buffer: dict[int, list[dict]] = field(default_factory=dict)
    chat_pending_messages: dict[int, list[dict]] = field(default_factory=dict)
    background_tasks: set[asyncio.Task] = field(default_factory=set)
    interject_tasks: dict[int, asyncio.Task] = field(default_factory=dict)
    last_style_ids: dict[int, set[str]] = field(default_factory=dict)

    def spawn(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.background_tasks.add(task)

        def done(completed: asyncio.Task) -> None:
            self.background_tasks.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                logging.getLogger("dafeiyu").error(
                    "background task failed: %s", type(completed.exception()).__name__
                )

        task.add_done_callback(done)
        return task

    async def close(self) -> None:
        tasks = list(self.background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def compress_lock(self, conversation_id: int) -> asyncio.Lock:
        lock = self.compress_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self.compress_locks[conversation_id] = lock
        return lock

    def state_lock(self, conversation_id: int) -> asyncio.Lock:
        """串行化同一会话对 会话运行状态 的所有「读-改-写」。"""
        lock = self.state_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self.state_locks[conversation_id] = lock
        return lock

    def chat_lock(self, conversation_id: int) -> asyncio.Lock:
        """串行化同一会话的直接回复处理，防止并发竞争。"""
        lock = self.chat_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self.chat_locks[conversation_id] = lock
        return lock
