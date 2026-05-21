from __future__ import annotations

import asyncio
import json
import logging
import time

from config import AppConfig
from domain.memory import MemoryDomain
from domain.pet import PetDomain
from integrations.llm_client import LLMClient
from repositories.memory_repo import MemoryRepository
from runtime import RuntimeState

log = logging.getLogger("tamagotchi")


class MemoryService:
    def __init__(
        self,
        config: AppConfig,
        runtime: RuntimeState,
        memory_domain: MemoryDomain,
        pet_domain: PetDomain,
        memory_repo: MemoryRepository,
        llm: LLMClient,
    ):
        self.config = config
        self.runtime = runtime
        self.memory_domain = memory_domain
        self.pet_domain = pet_domain
        self.memory_repo = memory_repo
        self.llm = llm

    async def embed_and_store(
        self, pet_id: int, kind: str, source_id: int, content: str
    ) -> None:
        vec = await self.llm.embed_text(content)
        if vec is None:
            return
        await self.memory_repo.store_embedding(pet_id, kind, source_id, content, vec)

    async def recall_relevant_cards(
        self, pet_id: int, query: str, k: int = 6
    ) -> list[dict]:
        if not query.strip():
            return []
        q_vec = await self.llm.embed_text(query)
        if q_vec is None:
            return []
        return await self.memory_repo.score_cards(pet_id, q_vec, k)

    async def build_recall_block(
        self,
        pet_id: int,
        query: str = "",
        k_relevant: int = 6,
        k_recent: int = 3,
    ) -> str:
        cards_by_id: dict[int, dict] = {}
        if query.strip():
            relevant = await self.recall_relevant_cards(pet_id, query, k=k_relevant)
            for card in relevant:
                cards_by_id[card["id"]] = card
        for card in await self.memory_repo.recent_cards(pet_id, k_recent):
            cards_by_id.setdefault(card["id"], card)
        return self.memory_domain.render_recall_block(list(cards_by_id.values()))

    async def _record_compress_failure(
        self, pet_id: int, compress_fail_count: int, new_until_id: int
    ) -> None:
        next_fail_count = await self.memory_repo.handle_compress_failure(
            pet_id, compress_fail_count, new_until_id
        )
        if next_fail_count >= 5:
            log.error(
                "compression failed 5 times for pet %d. Forcefully skipping chunk "
                "to until_id=%d to prevent locking the buffer.",
                pet_id,
                new_until_id,
            )
        else:
            log.warning(
                "memory compression failed for pet %d. fail_count is now %d.",
                pet_id,
                next_fail_count,
            )

    async def compress_pet_memory(self, pet_id: int) -> None:
        async with self.runtime.compress_lock(pet_id):
            (
                _summary_until_id,
                compress_fail_count,
                last_compress_attempt,
                rows,
            ) = await self.memory_repo.get_compress_context(pet_id)
            if not rows:
                return

            if len(rows) <= self.config.buffer_keep + 1:
                return

            to_compress = rows[: len(rows) - self.config.buffer_keep]
            new_until_id = to_compress[-1]["id"]

            now = time.time()
            if compress_fail_count >= 3 and now - last_compress_attempt < 3600:
                log.info(
                    "skipping memory compression for pet %d due to cool-down backoff "
                    "(fail_count=%d, last_attempt=%f)",
                    pet_id,
                    compress_fail_count,
                    last_compress_attempt,
                )
                return

            chunk_lines: list[str] = []
            for row in to_compress:
                if row["role"] == "user":
                    wrapped = self.pet_domain.wrap_user(
                        row["content"],
                        sender_name=row["sender_name"] or "",
                        is_observer=bool(row["is_observer"]),
                    )
                    chunk_lines.append(
                        self.config.compress_user_line_template.format(content=wrapped)
                    )
                else:
                    chunk_lines.append(
                        self.config.compress_assistant_line_template.format(
                            content=row["content"]
                        )
                    )
            chunk = "\n".join(chunk_lines)
            user_msg = self.config.compress_user_message_template.format(chunk=chunk)

            try:
                content = await self.llm.chat_json(
                    [
                        {"role": "system", "content": self.config.compress_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    max_tokens=self.config.compress_max_tokens,
                    temperature=0.4,
                )
            except Exception:
                log.exception("compress llm call failed for pet %d", pet_id)
                await self._record_compress_failure(
                    pet_id, compress_fail_count, new_until_id
                )
                return

            try:
                data = json.loads(content)
                cards_raw = data.get("cards") or []
                if not isinstance(cards_raw, list):
                    raise ValueError("cards is not a list")
            except Exception as exc:
                log.warning(
                    "compress parse/json error for pet %d: %s. raw content: %r",
                    pet_id,
                    str(exc),
                    content[:200],
                )
                await self._record_compress_failure(
                    pet_id, compress_fail_count, new_until_id
                )
                return

            try:
                inserted = await self.memory_repo.save_compressed_cards(
                    pet_id, cards_raw, new_until_id
                )
            except Exception:
                log.exception("failed to save compressed cards for pet %d", pet_id)
                await self._record_compress_failure(
                    pet_id, compress_fail_count, new_until_id
                )
                return

            log.info(
                "compressed pet %d: %d msgs -> %d cards, until_id=%d",
                pet_id,
                len(to_compress),
                len(inserted),
                new_until_id,
            )

            for card_id, card in inserted:
                text = self.memory_domain.format_card_for_embed(card)
                asyncio.create_task(
                    self.embed_and_store(pet_id, "card", card_id, text)
                )

