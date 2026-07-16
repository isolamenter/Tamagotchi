from __future__ import annotations

import asyncio
import logging
import time

from config import AppConfig
from domain.memory import MemoryDomain
from repositories.sqlite import Database

log = logging.getLogger("tamagotchi")


class MemoryRepository:
    def __init__(self, db: Database, memory_domain: MemoryDomain, config: AppConfig):
        self.db = db
        self.memory_domain = memory_domain
        self.config = config

    def _store_embedding(
        self, pet_id: int, kind: str, source_id: int, content: str, vec: list[float]
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO embeddings (pet_id, kind, source_id, content, vec, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    pet_id,
                    kind,
                    source_id,
                    content,
                    self.memory_domain.vec_pack(vec),
                    time.time(),
                ),
            )

    async def store_embedding(
        self, pet_id: int, kind: str, source_id: int, content: str, vec: list[float]
    ) -> None:
        await asyncio.to_thread(
            self._store_embedding, pet_id, kind, source_id, content, vec
        )

    def _score_cards(self, pet_id: int, q_vec: list[float], k: int) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT e.source_id, e.vec, c.when_text, c.who, c.what, c.vibe "
                "FROM embeddings e JOIN memory_cards c ON c.id = e.source_id "
                "WHERE e.pet_id = ? AND e.kind = 'card' "
                "ORDER BY e.id DESC LIMIT ?",
                (pet_id, self.config.recall_scan_max),
            ).fetchall()
        if not rows:
            return []
        q_len = len(q_vec)
        dim_match = 0
        scored = []
        for row in rows:
            vec = self.memory_domain.vec_unpack(row["vec"])
            if len(vec) == q_len:
                dim_match += 1
            sim = self.memory_domain.cosine(q_vec, vec)
            scored.append((sim, row))
        if dim_match == 0:
            log.warning(
                "pet %d: all %d stored embeddings mismatch query dim (%d); "
                "EMBED_MODEL changed? historical recall is disabled until re-embedded",
                pet_id,
                len(rows),
                q_len,
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        out = []
        for sim, row in scored[:k]:
            if sim <= 0.0:
                continue
            out.append(
                {
                    "id": row["source_id"],
                    "score": sim,
                    "when": row["when_text"],
                    "who": row["who"],
                    "what": row["what"],
                    "vibe": row["vibe"],
                }
            )
        return out

    async def score_cards(self, pet_id: int, q_vec: list[float], k: int) -> list[dict]:
        return await asyncio.to_thread(self._score_cards, pet_id, q_vec, k)

    def _recent_cards(self, pet_id: int, n: int) -> list[dict]:
        if n <= 0:
            return []
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id, when_text, who, what, vibe FROM memory_cards "
                "WHERE pet_id = ? ORDER BY id DESC LIMIT ?",
                (pet_id, n),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "when": row["when_text"],
                "who": row["who"],
                "what": row["what"],
                "vibe": row["vibe"],
            }
            for row in rows
        ]

    async def recent_cards(self, pet_id: int, n: int) -> list[dict]:
        return await asyncio.to_thread(self._recent_cards, pet_id, n)

    def _list_cards(self, pet_id: int, limit: int) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id, when_text, who, what, vibe, hooks, created_at "
                "FROM memory_cards WHERE pet_id = ? ORDER BY id DESC LIMIT ?",
                (pet_id, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "when": row["when_text"],
                "who": row["who"],
                "what": row["what"],
                "vibe": row["vibe"],
                "hooks": row["hooks"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def list_cards(self, pet_id: int, limit: int) -> list[dict]:
        return await asyncio.to_thread(self._list_cards, pet_id, limit)

    def _get_compress_context(self, pet_id: int) -> tuple[int, int, float, list[dict]]:
        with self.db.connect() as conn:
            pet_row = conn.execute(
                "SELECT summary_until_id, compress_fail_count, last_compress_attempt "
                "FROM pets WHERE id = ?",
                (pet_id,),
            ).fetchone()
            if not pet_row:
                return 0, 0, 0.0, []
            summary_until_id = pet_row["summary_until_id"]
            compress_fail_count = (
                pet_row["compress_fail_count"] if "compress_fail_count" in pet_row.keys() else 0
            )
            last_compress_attempt = (
                pet_row["last_compress_attempt"]
                if "last_compress_attempt" in pet_row.keys()
                else 0.0
            )
            rows = conn.execute(
                "SELECT m.id, m.role, m.content, "
                "COALESCE(u.name, m.sender_name) AS sender_name, "
                "m.sender_open_id, m.is_observer FROM messages m "
                "LEFT JOIN user_names u ON u.open_id = m.sender_open_id "
                "WHERE m.pet_id = ? AND m.id > ? ORDER BY m.id",
                (pet_id, summary_until_id),
            ).fetchall()
        return (
            summary_until_id,
            compress_fail_count,
            last_compress_attempt,
            [dict(row) for row in rows],
        )

    async def get_compress_context(self, pet_id: int) -> tuple[int, int, float, list[dict]]:
        return await asyncio.to_thread(self._get_compress_context, pet_id)

    def _save_compressed_cards(
        self, pet_id: int, cards_raw: list[dict], new_until_id: int
    ) -> list[tuple[int, dict]]:
        inserted = []
        now = time.time()
        with self.db.connect() as conn:
            for card in cards_raw:
                if not isinstance(card, dict):
                    continue
                what = (card.get("what") or "").strip()
                if not what:
                    continue
                row = conn.execute(
                    "INSERT INTO memory_cards "
                    "(pet_id, when_text, who, what, vibe, hooks, created_at, source_until_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                    (
                        pet_id,
                        (card.get("when") or "").strip(),
                        (card.get("who") or "").strip(),
                        what,
                        (card.get("vibe") or "").strip(),
                        (card.get("hooks") or "").strip(),
                        now,
                        new_until_id,
                    ),
                ).fetchone()
                inserted.append((row["id"], card))
            # 只有真正存进卡片时才推进 summary_until_id；0 张卡时不推进，
            # 否则这批消息会从 verbatim 窗口和卡片索引里同时消失（静默丢失）。
            if inserted:
                conn.execute(
                    "UPDATE pets SET summary_until_id = ?, compress_fail_count = 0, "
                    "last_compress_attempt = ? WHERE id = ?",
                    (new_until_id, now, pet_id),
                )
        return inserted

    async def save_compressed_cards(
        self, pet_id: int, cards_raw: list[dict], new_until_id: int
    ) -> list[tuple[int, dict]]:
        return await asyncio.to_thread(
            self._save_compressed_cards, pet_id, cards_raw, new_until_id
        )

    def _handle_compress_failure(
        self, pet_id: int, current_fail_count: int, new_until_id: int
    ) -> int:
        now = time.time()
        next_fail_count = current_fail_count + 1
        with self.db.connect() as conn:
            # A failure must never advance summary_until_id: doing so removes
            # raw history without a durable memory card.
            conn.execute(
                "UPDATE pets SET compress_fail_count = ?, last_compress_attempt = ? "
                "WHERE id = ?",
                (next_fail_count, now, pet_id),
            )
        return next_fail_count

    async def handle_compress_failure(
        self, pet_id: int, current_fail_count: int, new_until_id: int
    ) -> int:
        return await asyncio.to_thread(
            self._handle_compress_failure, pet_id, current_fail_count, new_until_id
        )
