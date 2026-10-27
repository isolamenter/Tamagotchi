from __future__ import annotations

import asyncio
import json
import logging
import time

from dafeiyu.config import AppConfig
from dafeiyu.domain.memory import MemoryDomain
from dafeiyu.repositories.sqlite import Database

log = logging.getLogger("dafeiyu")


class MemoryRepository:
    def __init__(self, db: Database, memory_domain: MemoryDomain, config: AppConfig):
        self.db = db
        self.memory_domain = memory_domain
        self.config = config

    @staticmethod
    def _attribution(row):
        return {
            **{k: json.loads(row[k]) for k in ("subject_ids", "reporter_ids", "evidence")},
            **{
                k: row[k]
                for k in (
                    "origin",
                    "profile_key",
                    "profile_category",
                    "confidence",
                    "expires_at",
                    "superseded_by",
                )
            },
        }

    async def subject_cards(
        self, cid: int, subjects: set[str], limit: int = 8, profiles_only=False
    ) -> list[dict]:
        if not subjects:
            return []

        def read():
            with self.db.connect() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT m.* FROM memories m, json_each(m.subject_ids) s "
                    "WHERE m.conversation_id=? AND s.value IN ("
                    + ",".join("?" for _ in subjects)
                    + ") "
                    "AND m.superseded_by IS NULL AND (m.expires_at IS NULL OR m.expires_at>unixepoch()) "
                    + (" AND m.profile_key!=''" if profiles_only else "")
                    + " ORDER BY m.id DESC LIMIT ?",
                    (cid, *sorted(subjects), limit),
                ).fetchall()
            return [{**dict(r), **self._attribution(r), "when": r["when_text"]} for r in rows]

        return await asyncio.to_thread(read)

    def _store_embedding(self, memory_id: int, content: str, vec: list[float]) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO memory_embeddings(memory_id,provider,model,dimension,content,vec,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET "
                "provider=excluded.provider,model=excluded.model,dimension=excluded.dimension,"
                "content=excluded.content,vec=excluded.vec,updated_at=excluded.updated_at",
                (
                    memory_id,
                    self.config.llm_provider,
                    self.config.embed_model,
                    len(vec),
                    content,
                    self.memory_domain.vec_pack(vec),
                    time.time(),
                ),
            )

    async def store_embedding(self, memory_id: int, content: str, vec: list[float]) -> None:
        await asyncio.to_thread(self._store_embedding, memory_id, content, vec)

    async def embedding_status(self) -> dict:
        def read():
            with self.db.connect() as conn:
                total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                ready = conn.execute(
                    "SELECT COUNT(*) FROM memory_embeddings WHERE provider=? AND model=?",
                    (self.config.llm_provider, self.config.embed_model),
                ).fetchone()[0]
            return {"total": total, "ready": ready, "pending": total - ready}

        return await asyncio.to_thread(read)

    def _score_cards(
        self,
        conversation_id: int,
        q_vec: list[float],
        k: int,
        subject_ids=None,
        only_subjects=False,
    ) -> list[dict]:
        subjects = sorted(subject_ids or ())
        subject_filter = ""
        params = []
        if subject_ids is not None or only_subjects:
            match = (
                "EXISTS(SELECT 1 FROM json_each(c.subject_ids) s WHERE s.value IN ("
                + ",".join("?" for _ in subjects)
                + "))"
                if subjects
                else "0"
            )
            subject_filter = " AND " + (
                match if only_subjects else "(c.subject_ids='[]' OR " + match + ")"
            )
            params = subjects
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT e.memory_id, e.vec, c.* "
                "FROM memory_embeddings e JOIN memories c ON c.id = e.memory_id "
                "WHERE c.conversation_id = ? AND c.superseded_by IS NULL AND (c.expires_at IS NULL OR c.expires_at > unixepoch()) AND e.provider = ? AND e.model = ? AND e.dimension = ? "
                + subject_filter
                + " ORDER BY e.memory_id DESC LIMIT ?",
                (
                    conversation_id,
                    self.config.llm_provider,
                    self.config.embed_model,
                    len(q_vec),
                    *params,
                    self.config.recall_scan_max,
                ),
            ).fetchall()
        if not rows:
            return []
        scored = []
        for row in rows:
            vec = self.memory_domain.vec_unpack(row["vec"])
            sim = self.memory_domain.cosine(q_vec, vec)
            scored.append((sim, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        out = []
        for sim, row in scored[:k]:
            if sim <= 0.0:
                continue
            out.append(
                {
                    **self._attribution(row),
                    "id": row["memory_id"],
                    "score": sim,
                    "kind": row["kind"],
                    "when": row["when_text"],
                    "who": row["who"],
                    "what": row["what"],
                    "vibe": row["vibe"],
                }
            )
        return out

    async def score_cards(
        self,
        conversation_id: int,
        q_vec: list[float],
        k: int,
        subject_ids=None,
        only_subjects=False,
    ) -> list[dict]:
        return await asyncio.to_thread(
            self._score_cards, conversation_id, q_vec, k, subject_ids, only_subjects
        )

    def _recent_cards(self, conversation_id: int, n: int) -> list[dict]:
        if n <= 0:
            return []
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memories "
                "WHERE conversation_id = ? AND superseded_by IS NULL AND (expires_at IS NULL OR expires_at > unixepoch()) ORDER BY id DESC LIMIT ?",
                (conversation_id, n),
            ).fetchall()
        return [
            {
                **self._attribution(row),
                "id": row["id"],
                "kind": row["kind"] if "kind" in row.keys() else "reality",
                "when": row["when_text"],
                "who": row["who"],
                "what": row["what"],
                "vibe": row["vibe"],
            }
            for row in rows
        ]

    async def recent_cards(self, conversation_id: int, n: int) -> list[dict]:
        return await asyncio.to_thread(self._recent_cards, conversation_id, n)

    def _list_cards(
        self, conversation_id: int, limit: int, before_id: int | None = None
    ) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * "
                "FROM memories WHERE conversation_id = ? AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
                (conversation_id, before_id, before_id, limit),
            ).fetchall()
        return [
            {
                **self._attribution(row),
                "id": row["id"],
                "kind": row["kind"] if "kind" in row.keys() else "reality",
                "when": row["when_text"],
                "who": row["who"],
                "what": row["what"],
                "vibe": row["vibe"],
                "hooks": row["hooks"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def list_cards(
        self, conversation_id: int, limit: int, before_id: int | None = None
    ) -> list[dict]:
        return await asyncio.to_thread(self._list_cards, conversation_id, limit, before_id)

    def _get_compress_context(self, conversation_id: int) -> tuple[int, int, float, list[dict]]:
        with self.db.connect() as conn:
            conversation_row = conn.execute(
                "SELECT summary_until_id, compress_fail_count, last_compress_attempt "
                "FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not conversation_row:
                return 0, 0, 0.0, []
            summary_until_id = conversation_row["summary_until_id"]
            compress_fail_count = conversation_row["compress_fail_count"]
            last_compress_attempt = conversation_row["last_compress_attempt"]
            rows = conn.execute(
                "SELECT m.* FROM messages m "
                "WHERE m.conversation_id = ? AND m.id > ? ORDER BY m.id LIMIT ?",
                (
                    conversation_id,
                    summary_until_id,
                    self.config.buffer_keep + self.config.consolidation["max_messages"],
                ),
            ).fetchall()
        return (
            summary_until_id,
            compress_fail_count,
            last_compress_attempt,
            [dict(row) for row in rows],
        )

    async def get_compress_context(
        self, conversation_id: int
    ) -> tuple[int, int, float, list[dict]]:
        return await asyncio.to_thread(self._get_compress_context, conversation_id)

    def _save_compressed_cards(
        self,
        conversation_id: int,
        cards_raw: list[dict],
        new_until_id: int,
        *,
        allow_empty: bool = False,
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
                kind = (card.get("kind") or "reality").strip()
                if kind not in {"reality", "hearsay", "imagination"}:
                    raise ValueError("invalid memory kind")
                row = conn.execute(
                    "INSERT INTO memories "
                    "(conversation_id, kind, when_text, who, what, vibe, hooks, created_at, source_until_id, subject_ids, reporter_ids, evidence, origin) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'compression') RETURNING id",
                    (
                        conversation_id,
                        kind,
                        (card.get("when") or "").strip(),
                        (card.get("who") or "").strip(),
                        what,
                        (card.get("vibe") or "").strip(),
                        (card.get("hooks") or "").strip(),
                        now,
                        new_until_id,
                        json.dumps(card.get("subject_ids", [])),
                        json.dumps(card.get("reporter_ids", [])),
                        json.dumps(card.get("evidence", []), ensure_ascii=False),
                    ),
                ).fetchone()
                inserted.append((row["id"], card))
            # The service may explicitly acknowledge a valid "nothing to remember"
            # response. Never infer that from a nonempty batch filtered to zero cards.
            # This only moves the prompt checkpoint; raw messages remain in SQLite.
            if inserted or (allow_empty and not cards_raw):
                conn.execute(
                    "UPDATE conversations SET summary_until_id = ?, compress_fail_count = 0, "
                    "last_compress_attempt = ? WHERE id = ?",
                    (new_until_id, now, conversation_id),
                )
        return inserted

    async def save_compressed_cards(
        self,
        conversation_id: int,
        cards_raw: list[dict],
        new_until_id: int,
        *,
        allow_empty: bool = False,
    ) -> list[tuple[int, dict]]:
        return await asyncio.to_thread(
            self._save_compressed_cards,
            conversation_id,
            cards_raw,
            new_until_id,
            allow_empty=allow_empty,
        )

    def _handle_compress_failure(
        self, conversation_id: int, current_fail_count: int, new_until_id: int
    ) -> int:
        now = time.time()
        next_fail_count = current_fail_count + 1
        with self.db.connect() as conn:
            # A failure must never advance summary_until_id: doing so removes
            # raw history without a durable memory card.
            conn.execute(
                "UPDATE conversations SET compress_fail_count = ?, last_compress_attempt = ? "
                "WHERE id = ?",
                (next_fail_count, now, conversation_id),
            )
        return next_fail_count

    async def handle_compress_failure(
        self, conversation_id: int, current_fail_count: int, new_until_id: int
    ) -> int:
        return await asyncio.to_thread(
            self._handle_compress_failure,
            conversation_id,
            current_fail_count,
            new_until_id,
        )
