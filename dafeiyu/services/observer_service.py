from __future__ import annotations

import logging
import time

from dafeiyu.config import AppConfig
from dafeiyu.repositories.conversation_repo import ConversationRepository
from dafeiyu.repositories.message_repo import MessageRepository
from dafeiyu.runtime import RuntimeState
from dafeiyu.services.memory_service import MemoryService

log = logging.getLogger("dafeiyu")


class ObserverService:
    def __init__(
        self,
        config: AppConfig,
        runtime: RuntimeState,
        message_repo: MessageRepository,
        memory_service: MemoryService,
        conversation_repo: ConversationRepository,
    ):
        self.config = config
        self.runtime = runtime
        self.message_repo = message_repo
        self.memory_service = memory_service
        self.conversation_repo = conversation_repo

    async def flush_observer_buffer(self, conversation_id: int) -> int:
        items = self.runtime.observer_buffer.pop(conversation_id, None)
        if not items:
            return 0
        try:
            await self.message_repo.append_observer_batch(conversation_id, items)
        except Exception:
            log.exception(
                "flush observer batch failed for conversation %d; re-buffering %d msgs",
                conversation_id,
                len(items),
            )
            self.runtime.observer_buffer.setdefault(conversation_id, [])[:0] = items
            return 0
        log.info(
            "conversation %d flushed %d buffered observer msgs",
            conversation_id,
            len(items),
        )
        await self.conversation_repo.mutate_state(
            conversation_id, lambda state: {**state, "last_social_ts": time.time()}
        )
        if (
            await self.message_repo.count_unsummarized(conversation_id)
            > self.config.compress_threshold
        ):
            self.runtime.spawn(self.memory_service.compress_conversation_memory(conversation_id))
        return len(items)

    async def flush_all(self) -> None:
        for conversation_id in list(self.runtime.observer_buffer.keys()):
            try:
                await self.flush_observer_buffer(conversation_id)
            except Exception:
                log.exception("flush observer buffer failed for conversation %d", conversation_id)
