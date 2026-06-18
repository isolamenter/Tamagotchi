from __future__ import annotations

import asyncio
import sqlite3
import time

from repositories.sqlite import Database


class SystemRepository:
    def __init__(self, db: Database):
        self.db = db

    def _get_sys_cache(self, key: str) -> str | None:
        now = time.time()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT val FROM sys_cache WHERE key = ? AND (expires_at IS NULL OR expires_at > ?)",
                (key, now),
            ).fetchone()
        return row["val"] if row else None

    async def get_sys_cache(self, key: str) -> str | None:
        return await asyncio.to_thread(self._get_sys_cache, key)

    def _set_sys_cache(self, key: str, val: str, expires_in_sec: float | None = None) -> None:
        expires_at = time.time() + expires_in_sec if expires_in_sec is not None else None
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sys_cache (key, val, expires_at) VALUES (?, ?, ?)",
                (key, val, expires_at),
            )

    async def set_sys_cache(
        self, key: str, val: str, expires_in_sec: float | None = None
    ) -> None:
        await asyncio.to_thread(self._set_sys_cache, key, val, expires_in_sec)

    def _delete_sys_cache(self, key: str) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM sys_cache WHERE key = ?", (key,))

    async def delete_sys_cache(self, key: str) -> None:
        await asyncio.to_thread(self._delete_sys_cache, key)

    def _get_cached_user_name(self, open_id: str) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT name FROM user_names WHERE open_id = ?",
                (open_id,),
            ).fetchone()
        return row["name"] if row else None

    async def get_cached_user_name(self, open_id: str) -> str | None:
        return await asyncio.to_thread(self._get_cached_user_name, open_id)

    def _set_cached_user_name(self, open_id: str, name: str) -> None:
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_names (open_id, name, updated_at) VALUES (?, ?, ?)",
                (open_id, name, now),
            )

    async def set_cached_user_name(self, open_id: str, name: str) -> None:
        await asyncio.to_thread(self._set_cached_user_name, open_id, name)

    def _check_and_register_event(self, event_id: str) -> bool:
        now = time.time()
        with self.db.connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO event_dedup (event_id, created_at) VALUES (?, ?)",
                    (event_id, now),
                )
                return False
            except sqlite3.IntegrityError:
                return True

    async def check_and_register_event(self, event_id: str) -> bool:
        return await asyncio.to_thread(self._check_and_register_event, event_id)

    def _clean_old_events(self, max_age_sec: float = 86400) -> None:
        threshold = time.time() - max_age_sec
        with self.db.connect() as conn:
            conn.execute("DELETE FROM event_dedup WHERE created_at < ?", (threshold,))

    async def clean_old_events(self, max_age_sec: float = 86400) -> None:
        await asyncio.to_thread(self._clean_old_events, max_age_sec)

