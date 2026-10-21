from __future__ import annotations

import json
import logging
import time

from dafeiyu.config import AppConfig
from dafeiyu.domain.conversation import ConversationContext
from dafeiyu.domain.memory import MemoryDomain
from dafeiyu.integrations.llm_client import LLMClient
from dafeiyu.repositories.memory_repo import MemoryRepository
from dafeiyu.runtime import RuntimeState

log = logging.getLogger("dafeiyu")


class MemoryService:
    def __init__(
        self,
        config: AppConfig,
        runtime: RuntimeState,
        memory_domain: MemoryDomain,
        conversation_domain: ConversationContext,
        memory_repo: MemoryRepository,
        llm: LLMClient,
    ):
        self.config = config
        self.runtime = runtime
        self.memory_domain = memory_domain
        self.conversation_domain = conversation_domain
        self.memory_repo = memory_repo
        self.llm = llm

    async def embed_and_store(self, source_id: int, content: str) -> None:
        vec = await self.llm.embed_text(content, purpose="retrieval_document")
        if vec is None:
            return
        await self.memory_repo.store_embedding(source_id, content, vec)

    async def recall_relevant_cards(
        self, conversation_id: int, query: str, k: int = 6
    ) -> list[dict]:
        if not query.strip():
            return []
        q_vec = await self.llm.embed_text(query, purpose="retrieval_query")
        if q_vec is None:
            return []
        return await self.memory_repo.score_cards(conversation_id, q_vec, k)

    async def build_recall_block(
        self,
        conversation_id: int,
        query: str = "",
        k_relevant: int = 6,
        k_recent: int = 3,
    ) -> str:
        cards_by_id: dict[int, dict] = {}
        if query.strip():
            relevant = await self.recall_relevant_cards(conversation_id, query, k=k_relevant)
            for card in relevant:
                cards_by_id[card["id"]] = card
        for card in await self.memory_repo.recent_cards(conversation_id, k_recent):
            cards_by_id.setdefault(card["id"], card)
        return self.memory_domain.render_recall_block(list(cards_by_id.values()))

    async def _record_compress_failure(
        self,
        conversation_id: int,
        compress_fail_count: int,
        new_until_id: int,
        rows: list[dict],
    ) -> None:
        next_fail_count = await self.memory_repo.handle_compress_failure(
            conversation_id, compress_fail_count, new_until_id
        )
        if next_fail_count >= 5:
            log.error(
                "compression failed 5 times for conversation %d; using deterministic "
                "memory cards instead of discarding history.",
                conversation_id,
            )
            fallback = [
                {
                    "kind": "imagination" if row.get("source") == "dream" else "hearsay",
                    "when": "聊天记录",
                    "who": row.get("sender_name")
                    or (self.config.character_name if row.get("role") == "assistant" else "群友"),
                    "what": ("梦境原文：" if row.get("source") == "dream" else "聊天原文：")
                    + (row.get("content") or "").strip(),
                    "vibe": "确定性备忘",
                    "hooks": "",
                }
                for row in rows
                if (row.get("content") or "").strip()
            ]
            inserted = await self.memory_repo.save_compressed_cards(
                conversation_id, fallback, new_until_id
            )
            if inserted:
                for card_id, card in inserted:
                    self.runtime.spawn(
                        self.embed_and_store(
                            card_id,
                            self.memory_domain.format_card_for_embed(card),
                        )
                    )
        else:
            log.warning(
                "memory compression failed for conversation %d. fail_count is now %d.",
                conversation_id,
                next_fail_count,
            )

    async def compress_conversation_memory(self, conversation_id: int) -> None:
        async with self.runtime.compress_lock(conversation_id):
            (
                _summary_until_id,
                compress_fail_count,
                last_compress_attempt,
                rows,
            ) = await self.memory_repo.get_compress_context(conversation_id)
            if not rows:
                return

            if len(rows) <= self.config.buffer_keep + 1:
                return

            to_compress = rows[: len(rows) - self.config.buffer_keep]
            new_until_id = to_compress[-1]["id"]

            now = time.time()
            if compress_fail_count >= 3 and now - last_compress_attempt < 3600:
                log.info(
                    "skipping memory compression for conversation %d due to cool-down backoff "
                    "(fail_count=%d, last_attempt=%f)",
                    conversation_id,
                    compress_fail_count,
                    last_compress_attempt,
                )
                return

            chunk_lines: list[str] = []
            for row in to_compress:
                if row["role"] == "user":
                    wrapped = self.conversation_domain.wrap_user(
                        row["content"],
                        sender_name=row["sender_name"] or "",
                        is_observer=bool(row["is_observer"]),
                    )
                    chunk_lines.append(
                        self.config.compress_user_line_template.format(content=wrapped)
                    )
                else:
                    chunk_lines.append(
                        self.config.compress_assistant_line_template.format(content=row["content"])
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
            except Exception as exc:
                log.warning(
                    "compress llm call failed conversation=%d error=%s",
                    conversation_id,
                    type(exc).__name__,
                )
                await self._record_compress_failure(
                    conversation_id, compress_fail_count, new_until_id, to_compress
                )
                return

            try:
                data = json.loads(content)
                cards_raw = data.get("cards") or []
                if not isinstance(cards_raw, list):
                    raise ValueError("cards is not a list")
            except Exception as exc:
                log.warning(
                    "compress parse/json error for conversation %d: %s",
                    conversation_id,
                    type(exc).__name__,
                )
                await self._record_compress_failure(
                    conversation_id, compress_fail_count, new_until_id, to_compress
                )
                return

            try:
                inserted = await self.memory_repo.save_compressed_cards(
                    conversation_id, cards_raw, new_until_id
                )
            except Exception:
                log.exception(
                    "failed to save compressed cards for conversation %d",
                    conversation_id,
                )
                await self._record_compress_failure(
                    conversation_id, compress_fail_count, new_until_id, to_compress
                )
                return

            if not inserted:
                # 解析成功但 0 张可用卡片：不推进 summary_until_id，失败五次后
                # 改用逐条确定性记忆卡，绝不丢弃原始历史。
                log.warning(
                    "compress produced no usable cards for conversation %d (raw=%d)",
                    conversation_id,
                    len(cards_raw),
                )
                await self._record_compress_failure(
                    conversation_id, compress_fail_count, new_until_id, to_compress
                )
                return

            log.info(
                "compressed conversation %d: %d msgs -> %d cards, until_id=%d",
                conversation_id,
                len(to_compress),
                len(inserted),
                new_until_id,
            )

            for card_id, card in inserted:
                text = self.memory_domain.format_card_for_embed(card)
                self.runtime.spawn(self.embed_and_store(card_id, text))
