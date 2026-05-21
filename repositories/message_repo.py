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
    ) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (pet_id, role, content, ts, sender_name, is_observer) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pet_id, role, content, time.time(), sender_name, 1 if is_observer else 0),
            )
            return int(cur.lastrowid)

    async def append_message(
        self,
        pet_id: int,
        role: str,
        content: str,
        sender_name: str = "",
        is_observer: bool = False,
    ) -> int:
        return await asyncio.to_thread(
            self._append_message, pet_id, role, content, sender_name, is_observer
        )

    def _append_observer_batch(self, pet_id: int, items: list[dict]) -> None:
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT INTO messages (pet_id, role, content, ts, sender_name, is_observer) "
                "VALUES (?, 'user', ?, ?, ?, 1)",
                [(pet_id, item["content"], item["ts"], item["sender_name"]) for item in items],
            )

    async def append_observer_batch(self, pet_id: int, items: list[dict]) -> None:
        await asyncio.to_thread(self._append_observer_batch, pet_id, items)

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

