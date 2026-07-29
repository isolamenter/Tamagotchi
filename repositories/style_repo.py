from __future__ import annotations

import asyncio
import struct
import time

from repositories.sqlite import Database


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


class StyleEmbeddingRepository:
    """SQLite cache for the static style corpus, separate from event memory."""

    def __init__(self, db: Database):
        self.db = db

    def _load(self, provider: str, model: str) -> dict[str, dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT example_id, content_hash, dimension, vec "
                "FROM style_embeddings WHERE provider = ? AND model = ?",
                (provider, model),
            ).fetchall()
        return {
            row["example_id"]: {
                "content_hash": row["content_hash"],
                "dimension": int(row["dimension"]),
                "vector": _unpack_vector(row["vec"]),
            }
            for row in rows
        }

    async def load(self, provider: str, model: str) -> dict[str, dict]:
        return await asyncio.to_thread(self._load, provider, model)

    def _replace_all(
        self, provider: str, model: str, records: list[dict]
    ) -> None:
        now = time.time()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM style_embeddings")
            conn.executemany(
                "INSERT INTO style_embeddings "
                "(example_id, provider, model, content_hash, dimension, vec, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        record["example_id"],
                        provider,
                        model,
                        record["content_hash"],
                        len(record["vector"]),
                        _pack_vector(record["vector"]),
                        now,
                    )
                    for record in records
                ],
            )

    async def replace_all(
        self, provider: str, model: str, records: list[dict]
    ) -> None:
        await asyncio.to_thread(self._replace_all, provider, model, records)
