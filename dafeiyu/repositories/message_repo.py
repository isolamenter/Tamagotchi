from __future__ import annotations

import asyncio
import time

from dafeiyu.repositories.sqlite import Database


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
    ) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages "
                "(conversation_id, role, content, ts, sender_name, sender_open_id, is_observer, source, reply_mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    role,
                    content,
                    time.time(),
                    sender_name,
                    sender_open_id,
                    1 if is_observer else 0,
                    source,
                    reply_mode,
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
        )

    def _append_observer_batch(self, conversation_id: int, items: list[dict]) -> None:
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT INTO messages "
                "(conversation_id, role, content, ts, sender_name, sender_open_id, is_observer, source) "
                "VALUES (?, 'user', ?, ?, ?, ?, 1, 'observer')",
                [
                    (
                        conversation_id,
                        item["content"],
                        item["ts"],
                        item["sender_name"],
                        item.get("open_id", ""),
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
                "SELECT m.id, m.role, m.content, m.ts, m.is_observer, m.source, m.reply_mode, m.meme_tag, "
                "COALESCE("
                "  u.name,"
                "  CASE WHEN m.sender_open_id != ''"
                "       THEN '群友-' || substr(m.sender_open_id, -4)"
                "       ELSE m.sender_name END"
                ") AS sender_name "
                "FROM messages m "
                "LEFT JOIN user_names u "
                "  ON u.open_id = m.sender_open_id AND m.sender_open_id != '' "
                "WHERE m.conversation_id = ? AND (? IS NULL OR m.id < ?) ORDER BY m.id DESC LIMIT ?",
                (conversation_id, before_id, before_id, limit),
            ).fetchall()
        items = [
            {
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
