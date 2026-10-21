from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from dafeiyu.domain.state import StateDomain
from dafeiyu.repositories.sqlite import Database
from dafeiyu.runtime import RuntimeState


class ConversationRepository:
    def __init__(self, db: Database, state_domain: StateDomain, runtime: RuntimeState):
        self.db, self.state_domain, self.runtime = db, state_domain, runtime

    def _get_or_create_conversation(self, chat_id: str) -> int:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO conversations(chat_id,created_at,last_update_ts) VALUES(?,?,?) ON CONFLICT(chat_id) DO NOTHING",
                (chat_id, time.time(), time.time()),
            )
            return conn.execute(
                "SELECT id FROM conversations WHERE chat_id=?", (chat_id,)
            ).fetchone()["id"]

    async def get_or_create_conversation(self, chat_id: str) -> int:
        return await asyncio.to_thread(self._get_or_create_conversation, chat_id)

    def _find_conversation(self, chat_id: str) -> int | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM conversations WHERE chat_id=?", (chat_id,)
            ).fetchone()
            return row["id"] if row else None

    async def find_conversation(self, chat_id: str) -> int | None:
        return await asyncio.to_thread(self._find_conversation, chat_id)

    def _load_conversation_context(self, conversation_id: int) -> tuple[list[dict], dict]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None:
                raise ValueError("conversation not found")
            messages = conn.execute(
                """
                SELECT m.*, COALESCE(u.name, NULLIF(m.sender_name,''), '群友-' || substr(m.sender_open_id,-4)) AS resolved_name
                FROM messages m LEFT JOIN user_names u ON u.open_id=m.sender_open_id
                WHERE m.conversation_id=? AND m.id>? ORDER BY m.id
            """,
                (conversation_id, row["summary_until_id"]),
            ).fetchall()
        history = [
            {
                **dict(m),
                "sender_name": m["resolved_name"],
                "is_observer": bool(m["is_observer"]),
            }
            for m in messages
        ]
        return history, self.state_domain.normalize_state(dict(row))

    async def load_conversation_context(self, conversation_id: int) -> tuple[list[dict], dict]:
        return await asyncio.to_thread(self._load_conversation_context, conversation_id)

    def _load_conversation_state(self, conversation_id: int) -> dict:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
        if row is None:
            raise ValueError("conversation not found")
        return self.state_domain.normalize_state(dict(row))

    async def load_conversation_state(self, conversation_id: int) -> dict:
        return await asyncio.to_thread(self._load_conversation_state, conversation_id)

    def _update_conversation_state(self, conversation_id: int, state: dict) -> None:
        state = self.state_domain.normalize_state(state)
        assignments = ",".join(f"{key}=?" for key in state)
        with self.db.connect() as conn:
            conn.execute(
                f"UPDATE conversations SET {assignments} WHERE id=?",
                (*state.values(), conversation_id),
            )

    async def update_conversation_state(self, conversation_id: int, state: dict) -> None:
        await asyncio.to_thread(self._update_conversation_state, conversation_id, state)

    async def mutate_state(self, conversation_id: int, mutator: Callable[[dict], dict]) -> dict:
        async with self.runtime.state_lock(conversation_id):
            state = self.state_domain.normalize_state(
                mutator(await self.load_conversation_state(conversation_id))
            )
            await self.update_conversation_state(conversation_id, state)
            return state

    def _get_gm_conversations(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("""
                SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count,
                (SELECT COUNT(*) FROM memories m WHERE m.conversation_id=c.id) AS memory_count,
                (SELECT MAX(ts) FROM messages m WHERE m.conversation_id=c.id) AS last_message_ts
                FROM conversations c ORDER BY COALESCE((SELECT MAX(ts) FROM messages m WHERE m.conversation_id=c.id),c.created_at) DESC,c.id
            """).fetchall()
        return [
            {
                "id": r["id"],
                "chat_id": r["chat_id"],
                "title": r["title"],
                "created_at": r["created_at"],
                "message_count": r["message_count"],
                "memory_count": r["memory_count"],
                "last_message_ts": r["last_message_ts"],
                "summary_until_id": r["summary_until_id"],
                "state": self.state_domain.normalize_state(dict(r)),
            }
            for r in rows
        ]

    async def get_gm_conversations(self) -> list[dict]:
        return await asyncio.to_thread(self._get_gm_conversations)

    async def rename(self, conversation_id: int, title: str) -> None:
        def write():
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE conversations SET title=? WHERE id=?",
                    (title, conversation_id),
                )

        await asyncio.to_thread(write)

    async def load_all_conversations(self) -> list[dict]:
        return await self.get_gm_conversations()

    async def resolve_conversation(
        self, conversation_id: int | None
    ) -> tuple[dict | None, list[dict]]:
        if conversation_id is None:
            return None, await self.get_gm_conversations()

        def read():
            with self.db.connect() as conn:
                row = conn.execute(
                    "SELECT id,chat_id,title FROM conversations WHERE id=?",
                    (conversation_id,),
                ).fetchone()
            return dict(row) if row else None

        return await asyncio.to_thread(read), []
