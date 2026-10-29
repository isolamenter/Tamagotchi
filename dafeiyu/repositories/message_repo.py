from __future__ import annotations

import asyncio
import json
import time

from dafeiyu.repositories.sqlite import Database

CARD_LINK_WINDOW_SEC = 60


class MessageRepository:
    def __init__(self, db: Database):
        self.db = db

    def _append_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        sender_name: str = "",
        is_observer: bool = False,
        sender_open_id: str = "",
        source: str = "direct",
        reply_mode: str = "",
        source_message_id: str = "",
        mentions: tuple[dict, ...] = (),
        ts: float | None = None,
        content_kind: str = "text",
        evidence_text: str | None = None,
    ) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages "
                "(conversation_id, role, content, ts, sender_name, sender_open_id, is_observer, source, reply_mode, source_message_id, mentions_json, content_kind, evidence_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    role,
                    content,
                    time.time() if ts is None else ts,
                    sender_name,
                    sender_open_id,
                    1 if is_observer else 0,
                    source,
                    reply_mode,
                    source_message_id,
                    json.dumps(mentions, ensure_ascii=False),
                    content_kind,
                    content if evidence_text is None and content_kind == "text" else evidence_text,
                ),
            )
            return int(cur.lastrowid)

    async def append_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        sender_name: str = "",
        is_observer: bool = False,
        sender_open_id: str = "",
        source: str = "direct",
        reply_mode: str = "",
        source_message_id: str = "",
        mentions: tuple[dict, ...] = (),
        ts: float | None = None,
        content_kind: str = "text",
        evidence_text: str | None = None,
    ) -> int:
        return await asyncio.to_thread(
            self._append_message,
            conversation_id,
            role,
            content,
            sender_name,
            is_observer,
            sender_open_id,
            source,
            reply_mode,
            source_message_id,
            mentions,
            ts,
            content_kind,
            evidence_text,
        )

    def _append_observer_batch(self, conversation_id: int, items: list[dict]) -> None:
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT INTO messages "
                "(conversation_id, role, content, ts, sender_name, sender_open_id, is_observer, source, source_message_id, mentions_json, content_kind, evidence_text) "
                "VALUES (?, 'user', ?, ?, ?, ?, 1, 'observer', ?, ?, ?, ?)",
                [
                    (
                        conversation_id,
                        item["content"],
                        item["ts"],
                        item["sender_name"],
                        item.get("open_id", ""),
                        item.get("source_message_id", ""),
                        json.dumps(item.get("mentions", []), ensure_ascii=False),
                        item.get("content_kind", "text"),
                        item.get("evidence_text", item["content"]),
                    )
                    for item in items
                ],
            )

    async def append_observer_batch(self, conversation_id: int, items: list[dict]) -> None:
        await asyncio.to_thread(self._append_observer_batch, conversation_id, items)

    def _recent_messages(
        self, conversation_id: int, limit: int, before_id: int | None = None
    ) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
                (conversation_id, before_id, before_id, limit),
            ).fetchall()
        items = [
            {
                **dict(row),
                "mentions": json.loads(row["mentions_json"]),
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "ts": row["ts"],
                "is_observer": bool(row["is_observer"]),
                "sender_name": row["sender_name"] or "",
                "source": row["source"],
                "reply_mode": row["reply_mode"],
                "meme_tag": row["meme_tag"],
            }
            for row in rows
        ]
        items.reverse()
        return items

    async def recent_messages(
        self, conversation_id: int, limit: int, before_id: int | None = None
    ) -> list[dict]:
        return await asyncio.to_thread(self._recent_messages, conversation_id, limit, before_id)

    def _count_unsummarized(self, conversation_id: int) -> int:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages "
                "WHERE conversation_id = ? "
                "AND id > (SELECT summary_until_id FROM conversations WHERE id = ?)",
                (conversation_id, conversation_id),
            ).fetchone()
        return row["c"]

    async def count_unsummarized(self, conversation_id: int) -> int:
        return await asyncio.to_thread(self._count_unsummarized, conversation_id)

    def _set_meme_tag(self, message_id: int, tag: str) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE messages SET meme_tag=? WHERE id=?", (tag, message_id))

    async def set_meme_tag(self, message_id: int, tag: str) -> None:
        await asyncio.to_thread(self._set_meme_tag, message_id, tag)

    def _published_journal(self, conversation_id: int, limit: int) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id, source, content, ts FROM messages WHERE conversation_id=? "
                "AND role='assistant' AND source IN ('diary','dream') ORDER BY id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
            cards = conn.execute(
                "SELECT card_id, content, image_key, created_at FROM published_cards "
                "WHERE conversation_id=? ORDER BY created_at",
                (conversation_id,),
            ).fetchall()
        # DeliveryService writes the card row, then the message row, for one successful send;
        # a card links only to the message with identical text recorded right after it.
        unmatched = [dict(card) for card in cards]
        entries = []
        for row in reversed(rows):
            card = next(
                (
                    c
                    for c in reversed(unmatched)
                    if c["content"] == row["content"]
                    and 0 <= row["ts"] - c["created_at"] <= CARD_LINK_WINDOW_SEC
                ),
                None,
            )
            if card is not None:
                unmatched.remove(card)
            entries.append(
                {
                    "kind": row["source"],
                    "status": "sent",
                    "ts": row["ts"],
                    "text": row["content"],
                    "card": card,
                }
            )
        oldest = rows[-1]["ts"] if len(rows) == limit else float("-inf")
        # A recorded card is a confirmed send even if the message row is missing; its kind
        # (diary or dream) was never stored, so it stays unknown.
        entries += [
            {"kind": "", "status": "sent", "ts": c["created_at"], "text": c["content"], "card": c}
            for c in unmatched
            if c["created_at"] >= oldest
        ]
        for entry in entries:
            if entry["card"] is not None:
                entry["card"] = {
                    "card_id": entry["card"]["card_id"],
                    "has_image": bool(entry["card"]["image_key"]),
                }
        entries.sort(key=lambda e: e["ts"], reverse=True)
        return entries

    async def published_journal(self, conversation_id: int, limit: int) -> list[dict]:
        return await asyncio.to_thread(self._published_journal, conversation_id, limit)

    def _card_image_key(self, conversation_id: int, card_id: str) -> str:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT image_key FROM published_cards WHERE conversation_id=? AND card_id=?",
                (conversation_id, card_id),
            ).fetchone()
        return row["image_key"] if row else ""

    async def card_image_key(self, conversation_id: int, card_id: str) -> str:
        return await asyncio.to_thread(self._card_image_key, conversation_id, card_id)
