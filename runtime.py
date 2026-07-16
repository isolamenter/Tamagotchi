from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class RuntimeState:
    token_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    compress_locks: dict[int, asyncio.Lock] = field(default_factory=dict)
    state_locks: dict[int, asyncio.Lock] = field(default_factory=dict)
    card_update_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    observer_buffer: dict[int, list[dict]] = field(default_factory=dict)
    reply_gate: dict[int, float] = field(default_factory=dict)

    def compress_lock(self, pet_id: int) -> asyncio.Lock:
        lock = self.compress_locks.get(pet_id)
        if lock is None:
            lock = asyncio.Lock()
            self.compress_locks[pet_id] = lock
        return lock

    def state_lock(self, pet_id: int) -> asyncio.Lock:
        """串行化同一宠物对 pets.state_json 的所有「读-改-写」。"""
        lock = self.state_locks.get(pet_id)
        if lock is None:
            lock = asyncio.Lock()
            self.state_locks[pet_id] = lock
        return lock

    def card_update_lock(self, message_id: str) -> asyncio.Lock:
        lock = self.card_update_locks.get(message_id)
        if lock is None:
            lock = asyncio.Lock()
            self.card_update_locks[message_id] = lock
        return lock
