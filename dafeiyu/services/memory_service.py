from __future__ import annotations

import asyncio
import json
import logging
import time

from dafeiyu.config import AppConfig
from dafeiyu.domain.conversation import ConversationContext, spoken_name
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
        self.identities = None

    async def embed_and_store(self, source_id: int, content: str) -> None:
        vec = await self.llm.embed_text(content, purpose="retrieval_document")
        if vec is None:
            return
        await self.memory_repo.store_embedding(source_id, content, vec)

    async def recall_relevant_cards(
        self, conversation_id: int, query: str, k: int = 6, subject_ids=None
    ) -> list[dict]:
        if not query.strip():
            return []
        try:
            q_vec = await self.llm.embed_text(query, purpose="retrieval_query")
        except Exception as exc:
            log.warning("recall embedding unavailable: %s", type(exc).__name__)
            return []
        if q_vec is None:
            return []
        scoped = (
            await self.memory_repo.score_cards(conversation_id, q_vec, 4, subject_ids, True)
            if subject_ids
            else []
        )
        general = await self.memory_repo.score_cards(conversation_id, q_vec, k, subject_ids)
        return scoped + general

    async def build_recall_block(
        self,
        conversation_id: int,
        query: str = "",
        k_relevant: int = 6,
        k_recent: int = 3,
        subject_ids: set[str] | None = None,
    ) -> str:
        cards_by_id: dict[int, dict] = {}
        if query.strip():
            relevant = await self.recall_relevant_cards(
                conversation_id, query, k=k_relevant, subject_ids=subject_ids or set()
            )
            for card in relevant:
                cards_by_id[card["id"]] = card
        for card in await self.memory_repo.recent_cards(conversation_id, k_recent):
            cards_by_id.setdefault(card["id"], card)
        # Per-person facts are retrieved only for current/explicitly mentioned stable IDs.
        subjects = subject_ids or set()
        cards_by_id = {
            key: card
            for key, card in cards_by_id.items()
            if not card.get("subject_ids") or subjects.intersection(card.get("subject_ids", []))
        }
        scoped = await self.memory_repo.subject_cards(conversation_id, subjects, 8)
        for card in scoped:
            cards_by_id.setdefault(card["id"], card)
        cards = list(cards_by_id.values())[:14]
        identity = (
            await self.identities.render(conversation_id, subjects)
            if self.identities is not None
            else ""
        )
        selected = []
        for card in cards:
            candidate = self.memory_domain.render_recall_block(selected + [card])
            if len(candidate) + len(identity) <= 16000:
                selected.append(card)
        return self.memory_domain.render_recall_block(selected) + identity

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
                    "who": spoken_name(
                        row.get("sender_name") or "", row.get("sender_open_id") or ""
                    )
                    or (self.config.character_name if row.get("role") == "assistant" else "群友"),
                    "what": ("梦境原文：" if row.get("source") == "dream" else "聊天原文：")
                    + (row.get("content") or "").strip(),
                    "vibe": "确定性备忘",
                    "hooks": "",
                    "subject_ids": [],
                    "reporter_ids": [row["sender_open_id"]] if row.get("sender_open_id") else [],
                    "evidence": [
                        {
                            "message_id": row.get("id"),
                            "source_message_id": row.get("source_message_id", ""),
                            "ts": row.get("ts"),
                            "source": row.get("source", ""),
                            "role": row.get("role", ""),
                            "reporter_id": row.get("sender_open_id", ""),
                            "relation": "raw_transcript",
                        }
                    ],
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

            # Include prompt overhead in the character budget, not only serialized rows.
            budget = (
                self.config.consolidation["max_context_chars"]
                - len(self.config.compress_prompt)
                - len(self.config.compress_user_message_template.format(chunk=""))
            )
            to_compress = [
                {
                    **row,
                    "sender_name": spoken_name(row["sender_name"], row["sender_open_id"]),
                }
                for row in to_compress
            ]
            chunk = json.dumps(to_compress, ensure_ascii=False)
            while len(to_compress) > 1 and len(chunk) > budget:
                to_compress.pop()
                chunk = json.dumps(to_compress, ensure_ascii=False)
            new_until_id = to_compress[-1]["id"]
            chunks = [chunk]
            if len(chunk) > budget:
                # One source message may need several requests. Keep the checkpoint
                # unchanged until every fragment has a validated, durable result.
                row = to_compress[0]
                record = {k: v for k, v in row.items() if k != "evidence_text"}
                text, offset, chunks = row["content"], 0, []
                while offset < len(text):
                    take = min(len(text) - offset, budget)
                    while take > 0:
                        record.update(
                            content=text[offset : offset + take],
                            start_offset=offset,
                            content_complete=offset + take == len(text),
                        )
                        fragment = json.dumps([record], ensure_ascii=False)
                        if len(fragment) <= budget:
                            break
                        take //= 2
                    if take <= 0:
                        log.error(
                            "compression metadata exceeds budget conversation=%d", conversation_id
                        )
                        return
                    chunks.append(fragment)
                    offset += take
            cards_raw = []
            for chunk in chunks:
                user_msg = self.config.compress_user_message_template.format(chunk=chunk)

                try:
                    content = await asyncio.wait_for(
                        self.llm.chat_json(
                            [
                                {"role": "system", "content": self.config.compress_prompt},
                                {"role": "user", "content": user_msg},
                            ],
                            max_tokens=self.config.compress_max_tokens,
                            temperature=0.4,
                        ),
                        timeout=self.config.consolidation["timeout_sec"],
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
                    if not isinstance(data, dict) or not isinstance(data.get("cards"), list):
                        raise ValueError("expected an object with a cards list")
                    batch_cards = data["cards"]
                    # Validate the whole batch before saving. A malformed/nonempty batch
                    # must never masquerade as an intentional empty result, or advance
                    # the checkpoint after silently dropping only its invalid entries.
                    for card in batch_cards:
                        if not isinstance(card, dict):
                            raise ValueError("memory card is not an object")
                        if not isinstance(card.get("what"), str) or not card["what"].strip():
                            raise ValueError("memory card needs nonempty what")
                        for key in ("kind", "when", "who", "vibe", "hooks"):
                            if key in card and not isinstance(card[key], str):
                                raise ValueError(f"memory card {key} is not text")
                        if card.get("kind", "reality").strip() not in {
                            "reality",
                            "hearsay",
                            "imagination",
                        }:
                            raise ValueError("invalid memory kind")
                        source_ids = card.get("source_ids", [])
                        by_id = {r["id"]: r for r in to_compress}
                        if not isinstance(source_ids, list) or any(
                            type(mid) is not int or mid not in by_id for mid in source_ids
                        ):
                            raise ValueError("compression source outside batch")
                        sources = [by_id[mid] for mid in source_ids] if source_ids else to_compress
                        card[
                            "subject_ids"
                        ] = []  # Only evidence-validated daily analysis assigns people.
                        card["reporter_ids"] = sorted(
                            {r["sender_open_id"] for r in sources if r["sender_open_id"]}
                        )
                        card["evidence"] = [
                            {
                                "message_id": r["id"],
                                "source_message_id": r["source_message_id"],
                                "ts": r["ts"],
                                "source": r["source"],
                                "role": r["role"],
                                "reporter_id": r["sender_open_id"],
                                "relation": "compression_context",
                            }
                            for r in sources
                        ]
                        if any(r["source"] == "dream" for r in sources):
                            card["kind"] = "imagination"
                        elif not source_ids or any(r["role"] == "assistant" for r in sources):
                            card["kind"] = "hearsay"
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

                cards_raw.extend(batch_cards)

            try:
                inserted = await self.memory_repo.save_compressed_cards(
                    conversation_id, cards_raw, new_until_id, allow_empty=not cards_raw
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

            if cards_raw and not inserted:
                # Only an explicit, validated empty list is successful without cards.
                # Nonempty input with no durable cards must retain history for retry.
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
