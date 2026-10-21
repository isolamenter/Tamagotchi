"""Rebuild schema-2 memory vectors atomically, with model identity and snapshot checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Protocol

from dafeiyu.config import load_config
from dafeiyu.domain.memory import MemoryDomain
from dafeiyu.integrations.llm_client import LLMClient
from dafeiyu.repositories.sqlite import Database


class Embedder(Protocol):
    async def embed_texts(
        self, texts: list[str], *, purpose: str = ""
    ) -> list[list[float]] | None: ...


def load_cards(conn: sqlite3.Connection) -> list[dict]:
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "when": r["when_text"],
            "who": r["who"],
            "what": r["what"],
            "vibe": r["vibe"],
            "hooks": r["hooks"],
        }
        for r in conn.execute("SELECT * FROM memories ORDER BY id")
    ]


async def prepare_embeddings(
    cards: list[dict],
    llm: Embedder,
    memory_domain: MemoryDomain,
    *,
    tries: int = 4,
    batch_size: int = 50,
) -> tuple[list[tuple], int]:
    if batch_size <= 0 or tries <= 0:
        raise ValueError("Batch size and retry count must be positive")
    prepared, dimension = [], None
    for start in range(0, len(cards), batch_size):
        batch = cards[start : start + batch_size]
        contents = [memory_domain.format_card_for_embed(card) for card in batch]
        vectors = None
        for attempt in range(tries):
            vectors = await llm.embed_texts(contents, purpose="retrieval_document")
            if vectors is not None and len(vectors) == len(batch):
                break
            if attempt + 1 < tries:
                await asyncio.sleep(2 * (attempt + 1))
        if vectors is None or len(vectors) != len(batch):
            raise RuntimeError("Memory embedding failed; existing vectors were not replaced")
        for card, content, vector in zip(batch, contents, vectors, strict=True):
            if not vector or not all(math.isfinite(value) for value in vector) or not any(vector):
                raise RuntimeError("Embedding response contained an invalid vector")
            if dimension is None:
                dimension = len(vector)
            if len(vector) != dimension:
                raise RuntimeError("Embedding dimensions changed during the run")
            prepared.append((card["id"], dimension, content, memory_domain.vec_pack(vector)))
    return prepared, dimension or 0


def replace_memory_embeddings(
    conn: sqlite3.Connection,
    prepared: list[tuple],
    snapshot: list[dict],
    *,
    provider: str,
    model: str,
) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        if load_cards(conn) != snapshot:
            raise RuntimeError("Memories changed during embedding; no vectors were replaced")
        if [row[0] for row in prepared] != [row["id"] for row in snapshot]:
            raise RuntimeError("Prepared vectors do not cover the complete memory snapshot")
        conn.execute("DELETE FROM memory_embeddings")
        conn.executemany(
            "INSERT INTO memory_embeddings(memory_id,dimension,content,vec,provider,model,updated_at) VALUES(?,?,?,?,?,?,?)",
            [(*row, provider, model, time.time()) for row in prepared],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("Database does not exist")
    environment = dict(os.environ)
    if args.env_file:
        from dotenv import dotenv_values

        environment.update({k: v for k, v in dotenv_values(args.env_file).items() if v is not None})
    environment["STATE_DB"] = str(args.database)
    config = load_config(environment)
    database = Database(args.database)
    database.init_db()
    llm = LLMClient(config)
    try:
        with database.connect() as conn:
            snapshot = load_cards(conn)
        prepared, dimension = await prepare_embeddings(
            snapshot,
            llm,
            MemoryDomain(config),
            batch_size=config.style_retrieval["embedding_batch_size"],
        )
        with database.connect() as conn:
            replace_memory_embeddings(
                conn,
                prepared,
                snapshot,
                provider=config.llm_provider,
                model=config.embed_model,
            )
        print(
            json.dumps(
                {
                    "memories": len(snapshot),
                    "vectors": len(prepared),
                    "dimension": dimension,
                    "verified": True,
                }
            )
        )
    finally:
        await llm.close()


if __name__ == "__main__":
    asyncio.run(main())
