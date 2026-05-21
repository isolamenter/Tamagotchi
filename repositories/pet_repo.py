from __future__ import annotations

import asyncio
import json
import time

from domain.state import StateDomain
from repositories.sqlite import Database


class PetRepository:
    def __init__(self, db: Database, state_domain: StateDomain):
        self.db = db
        self.state_domain = state_domain

    def decode_state(self, state_json: str | None) -> dict:
        try:
            stored = json.loads(state_json or "{}")
        except json.JSONDecodeError:
            stored = {}
        return stored or self.state_domain.initial_state()

    def _get_or_create_pet(self, chat_id: str) -> int:
        now = time.time()
        initial_state_json = json.dumps(self.state_domain.initial_state())
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO pets (chat_id, born_at, state_json) VALUES (?, ?, ?)",
                (chat_id, now, initial_state_json),
            )
            row = conn.execute(
                "SELECT id FROM pets WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return row["id"]

    async def get_or_create_pet(self, chat_id: str) -> int:
        return await asyncio.to_thread(self._get_or_create_pet, chat_id)

    def _find_pet(self, chat_id: str) -> int | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM pets WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return row["id"] if row else None

    async def find_pet(self, chat_id: str) -> int | None:
        return await asyncio.to_thread(self._find_pet, chat_id)

    def _load_pet_context(self, pet_id: int) -> tuple[list[dict], dict]:
        with self.db.connect() as conn:
            pet_row = conn.execute(
                "SELECT summary_until_id, state_json FROM pets WHERE id = ?", (pet_id,)
            ).fetchone()
            msg_rows = conn.execute(
                "SELECT id, role, content, sender_name, is_observer FROM messages "
                "WHERE pet_id = ? AND id > ? ORDER BY id",
                (pet_id, pet_row["summary_until_id"]),
            ).fetchall()
        history = [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "sender_name": row["sender_name"] or "",
                "is_observer": bool(row["is_observer"]),
            }
            for row in msg_rows
        ]
        current = self.state_domain.decay_state(
            self.decode_state(pet_row["state_json"]), time.time(), pet_id
        )
        return history, current

    async def load_pet_context(self, pet_id: int) -> tuple[list[dict], dict]:
        return await asyncio.to_thread(self._load_pet_context, pet_id)

    def _update_pet_state(self, pet_id: int, state: dict) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE pets SET state_json = ? WHERE id = ?",
                (json.dumps(state), pet_id),
            )

    async def update_pet_state(self, pet_id: int, state: dict) -> None:
        await asyncio.to_thread(self._update_pet_state, pet_id, state)

    def _load_pet_state(self, pet_id: int) -> dict:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM pets WHERE id = ?", (pet_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"pet not found: {pet_id}")
        return self.state_domain.decay_state(
            self.decode_state(row["state_json"]), time.time(), pet_id
        )

    async def load_pet_state(self, pet_id: int) -> dict:
        return await asyncio.to_thread(self._load_pet_state, pet_id)

    def _load_all_pets(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id, chat_id, state_json FROM pets").fetchall()
        return [dict(row) for row in rows]

    async def load_all_pets(self) -> list[dict]:
        return await asyncio.to_thread(self._load_all_pets)

    def _resolve_pet(self, pet_id: int | None) -> tuple[dict | None, list[dict]]:
        with self.db.connect() as conn:
            if pet_id is not None:
                row = conn.execute(
                    "SELECT id, chat_id FROM pets WHERE id = ?", (pet_id,)
                ).fetchone()
                return dict(row) if row else None, []
            rows = conn.execute("SELECT id, chat_id FROM pets ORDER BY id").fetchall()
            return None, [dict(row) for row in rows]

    async def resolve_pet(self, pet_id: int | None) -> tuple[dict | None, list[dict]]:
        return await asyncio.to_thread(self._resolve_pet, pet_id)

    def _get_gm_pets(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT p.id, p.chat_id, p.born_at, p.summary_until_id, p.state_json, "
                "COUNT(DISTINCT m.id) AS message_count, "
                "COUNT(DISTINCT c.id) AS card_count "
                "FROM pets p "
                "LEFT JOIN messages m ON m.pet_id = p.id "
                "LEFT JOIN memory_cards c ON c.pet_id = p.id "
                "GROUP BY p.id ORDER BY p.id"
            ).fetchall()
        return [dict(row) for row in rows]

    async def get_gm_pets(self) -> list[dict]:
        return await asyncio.to_thread(self._get_gm_pets)

