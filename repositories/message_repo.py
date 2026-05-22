from __future__ import annotations

import asyncio
import time

from repositories.sqlite import Database


class MessageRepository:
    def __init__(self, db: Database):
        self.db = db

    def _append_message(
        self,
        pet_id: int,
        role: str,
        content: str,
        sender_name: str = "",
        is_observer: bool = False,
        sender_open_id: str = "",
    ) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages "
                "(pet_id, role, content, ts, sender_name, sender_open_id, is_observer) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    pet_id,
                    role,
                    content,
                    time.time(),
                    sender_name,
                    sender_open_id,
                    1 if is_observer else 0,
                ),
            )
            return int(cur.lastrowid)

    async def append_message(
        self,
        pet_id: int,
        role: str,
        content: str,
        sender_name: str = "",
        is_observer: bool = False,
        sender_open_id: str = "",
    ) -> int:
        return await asyncio.to_thread(
            self._append_message,
            pet_id,
            role,
            content,
            sender_name,
            is_observer,
            sender_open_id,
        )

    def _append_observer_batch(self, pet_id: int, items: list[dict]) -> None:
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT INTO messages "
                "(pet_id, role, content, ts, sender_name, sender_open_id, is_observer) "
                "VALUES (?, 'user', ?, ?, ?, ?, 1)",
                [
                    (
                        pet_id,
                        item["content"],
                        item["ts"],
                        item["sender_name"],
                        item.get("open_id", ""),
                    )
                    for item in items
                ],
            )

    async def append_observer_batch(self, pet_id: int, items: list[dict]) -> None:
        await asyncio.to_thread(self._append_observer_batch, pet_id, items)

    def _recent_messages(self, pet_id: int, limit: int) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT m.id, m.role, m.content, m.ts, m.is_observer, "
                "COALESCE("
                "  u.name,"
                "  CASE WHEN m.sender_open_id != ''"
                "       THEN '群友-' || substr(m.sender_open_id, -4)"
                "       ELSE m.sender_name END"
                ") AS sender_name "
                "FROM messages m "
                "LEFT JOIN user_names u "
                "  ON u.open_id = m.sender_open_id AND m.sender_open_id != '' "
                "WHERE m.pet_id = ? ORDER BY m.id DESC LIMIT ?",
                (pet_id, limit),
            ).fetchall()
        items = [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "ts": row["ts"],
                "is_observer": bool(row["is_observer"]),
                "sender_name": row["sender_name"] or "",
            }
            for row in rows
        ]
        items.reverse()
        return items

    async def recent_messages(self, pet_id: int, limit: int) -> list[dict]:
        return await asyncio.to_thread(self._recent_messages, pet_id, limit)

    def _count_unsummarized(self, pet_id: int) -> int:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages "
                "WHERE pet_id = ? "
                "AND id > (SELECT summary_until_id FROM pets WHERE id = ?)",
                (pet_id, pet_id),
            ).fetchone()
        return row["c"]

    async def count_unsummarized(self, pet_id: int) -> int:
        return await asyncio.to_thread(self._count_unsummarized, pet_id)

