from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class RuntimeState:
    token_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    compress_locks: dict[int, asyncio.Lock] = field(default_factory=dict)
    card_locks: dict[int, asyncio.Lock] = field(default_factory=dict)
    card_followup_buffer: dict[str, dict] = field(default_factory=dict)
    observer_buffer: dict[int, list[dict]] = field(default_factory=dict)
    reply_gate: dict[int, float] = field(default_factory=dict)

    def compress_lock(self, pet_id: int) -> asyncio.Lock:
        lock = self.compress_locks.get(pet_id)
        if lock is None:
            lock = asyncio.Lock()
            self.compress_locks[pet_id] = lock
        return lock

    def card_lock(self, pet_id: int) -> asyncio.Lock:
        lock = self.card_locks.get(pet_id)
        if lock is None:
            lock = asyncio.Lock()
            self.card_locks[pet_id] = lock
        return lock

