from __future__ import annotations

import asyncio
import logging

from config import AppConfig
from repositories.message_repo import MessageRepository
from runtime import RuntimeState
from services.memory_service import MemoryService

log = logging.getLogger("tamagotchi")


class ObserverService:
    def __init__(
        self,
        config: AppConfig,
        runtime: RuntimeState,
        message_repo: MessageRepository,
        memory_service: MemoryService,
    ):
        self.config = config
        self.runtime = runtime
        self.message_repo = message_repo
        self.memory_service = memory_service

    async def flush_observer_buffer(self, pet_id: int) -> int:
        items = self.runtime.observer_buffer.pop(pet_id, None)
        if not items:
            return 0
        try:
            await self.message_repo.append_observer_batch(pet_id, items)
        except Exception:
            log.exception(
                "flush observer batch failed for pet %d; re-buffering %d msgs",
                pet_id,
                len(items),
            )
            self.runtime.observer_buffer.setdefault(pet_id, [])[:0] = items
            return 0
        log.info("pet %d flushed %d buffered observer msgs", pet_id, len(items))
        if await self.message_repo.count_unsummarized(pet_id) > self.config.compress_threshold:
            asyncio.create_task(self.memory_service.compress_pet_memory(pet_id))
        return len(items)

    async def flush_all(self) -> None:
        for pet_id in list(self.runtime.observer_buffer.keys()):
            try:
                await self.flush_observer_buffer(pet_id)
            except Exception:
                log.exception("flush observer buffer failed for pet %d", pet_id)

