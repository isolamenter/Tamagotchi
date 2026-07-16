from __future__ import annotations

import asyncio
import json
import time

from repositories.sqlite import Database


class CardRepository:
    """Card contracts and durable claim records."""

    def __init__(self, db: Database):
        self.db = db

    def _create_instance(
        self,
        card_id: str,
        pet_id: int,
        mode: str,
        need_id: str,
        need_round: int,
        built_at: float,
        expires_at: float,
        max_settlements: int,
        base_text: str,
        action_keys: list[str],
        img_key: str | None,
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO card_instances "
                "(card_id, pet_id, mode, need_id, need_round, built_at, expires_at, "
                "max_settlements, base_text, action_keys_json, img_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    card_id,
                    pet_id,
                    mode,
                    need_id,
                    need_round,
                    built_at,
                    expires_at,
                    max_settlements,
                    base_text,
                    json.dumps(action_keys, ensure_ascii=False),
                    img_key or "",
                ),
            )

    async def create_instance(self, **kwargs) -> None:
        await asyncio.to_thread(self._create_instance, **kwargs)

    def _mark_announced(self, card_id: str, message_id: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE card_instances SET announced = 1, message_id = ? WHERE card_id = ?",
                (message_id, card_id),
            )

    async def mark_announced(self, card_id: str, message_id: str) -> None:
        await asyncio.to_thread(self._mark_announced, card_id, message_id)

    def _get_instance(self, card_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM card_instances WHERE card_id = ?", (card_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        for key in ("action_keys_json", "feedback_lines_json"):
            try:
                item[key] = json.loads(item[key] or "[]")
            except json.JSONDecodeError:
                item[key] = []
        return item

    async def get_instance(self, card_id: str) -> dict | None:
        return await asyncio.to_thread(self._get_instance, card_id)

    def _claim(self, card_id: str, actor_open_id: str, action: str, now: float) -> str:
        """Return settle/social/duplicate/missing/expired."""
        actor_open_id = actor_open_id or "anonymous"
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT announced, expires_at, max_settlements, settlement_count "
                "FROM card_instances WHERE card_id = ?",
                (card_id,),
            ).fetchone()
            if not row or not row["announced"]:
                return "missing"
            if now >= float(row["expires_at"]):
                return "expired"
            exists = conn.execute(
                "SELECT 1 FROM card_claims WHERE card_id = ? AND actor_open_id = ?",
                (card_id, actor_open_id),
            ).fetchone()
            if exists:
                return "duplicate"
            applies = int(row["settlement_count"]) < int(row["max_settlements"])
            conn.execute(
                "INSERT INTO card_claims (card_id, actor_open_id, action, settled_at, state_applied) "
                "VALUES (?, ?, ?, ?, ?)",
                (card_id, actor_open_id, action, now, 1 if applies else 0),
            )
            if applies:
                conn.execute(
                    "UPDATE card_instances SET settlement_count = settlement_count + 1 WHERE card_id = ?",
                    (card_id,),
                )
                return "settle"
            return "social"

    async def claim(
        self, card_id: str, actor_open_id: str, action: str, now: float | None = None
    ) -> str:
        return await asyncio.to_thread(self._claim, card_id, actor_open_id, action, now or time.time())

    def _append_feedback(self, card_id: str, line: str, max_lines: int) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT feedback_lines_json FROM card_instances WHERE card_id = ?", (card_id,)
            ).fetchone()
            if not row:
                return None
            try:
                lines = json.loads(row["feedback_lines_json"] or "[]")
            except json.JSONDecodeError:
                lines = []
            lines.append(line)
            if max_lines > 0:
                lines = lines[-max_lines:]
            conn.execute(
                "UPDATE card_instances SET feedback_lines_json = ?, version = version + 1 "
                "WHERE card_id = ?",
                (json.dumps(lines, ensure_ascii=False), card_id),
            )
        return self._get_instance(card_id)

    async def append_feedback(self, card_id: str, line: str, max_lines: int) -> dict | None:
        return await asyncio.to_thread(self._append_feedback, card_id, line, max_lines)
