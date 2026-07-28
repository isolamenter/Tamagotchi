"""一次性重嵌脚本：用当前 .env 的 EMBED_MODEL / provider 重算所有 card 向量。

换 embedding 模型或 provider 后跑一次。直接从 memory_cards 重建 embeddings，
不改写记忆卡内容。所有向量生成成功后才在单个事务里替换旧向量：
    .venv/bin/python scripts_reembed.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Protocol

from config import load_config
from domain.memory import MemoryDomain
from integrations.llm_client import LLMClient


class Embedder(Protocol):
    async def embed_text(self, text: str) -> list[float] | None: ...


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


async def embed_with_retry(llm: Embedder, text: str, tries: int = 4):
    for i in range(tries):
        vec = await llm.embed_text(text)
        if vec:
            return vec
        if i + 1 < tries:
            await asyncio.sleep(2 * (i + 1))
    return None


def load_cards(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, pet_id, when_text, who, what, vibe, hooks "
        "FROM memory_cards ORDER BY id"
    ).fetchall()
    return [
        {
            "id": row["id"],
            "pet_id": row["pet_id"],
            "when": row["when_text"],
            "who": row["who"],
            "what": row["what"],
            "vibe": row["vibe"],
            "hooks": row["hooks"],
        }
        for row in rows
    ]


async def prepare_embeddings(
    cards: list[dict],
    llm: Embedder,
    memory_domain: MemoryDomain,
    *,
    tries: int = 4,
) -> tuple[list[tuple], int]:
    prepared: list[tuple] = []
    expected_dim: int | None = None
    for index, card in enumerate(cards, start=1):
        content = memory_domain.format_card_for_embed(card)
        vec = await embed_with_retry(llm, content, tries=tries)
        if not vec:
            raise RuntimeError(f"embedding failed for memory_card id={card['id']}")
        if expected_dim is None:
            expected_dim = len(vec)
        elif len(vec) != expected_dim:
            raise RuntimeError(
                f"embedding dimension changed during run: card id={card['id']} "
                f"expected={expected_dim} actual={len(vec)}"
            )
        prepared.append(
            (
                card["pet_id"],
                "card",
                card["id"],
                content,
                memory_domain.vec_pack(vec),
            )
        )
        if index % 25 == 0 or index == len(cards):
            print(f"  prepared {index}/{len(cards)}")
    return prepared, expected_dim or 0


def replace_card_embeddings(
    conn: sqlite3.Connection, prepared: list[tuple], *, now: float | None = None
) -> None:
    timestamp = time.time() if now is None else now
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM embeddings WHERE kind = 'card'")
        conn.executemany(
            "INSERT INTO embeddings (pet_id, kind, source_id, content, vec, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(*item, timestamp) for item in prepared],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


async def main() -> None:
    cfg = load_config(load_env(".env"))
    print(f"provider={cfg.llm_provider} embed_model={cfg.embed_model}")
    llm = LLMClient(cfg)
    md = MemoryDomain(cfg)

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        cards = load_cards(conn)
        old_count = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE kind = 'card'"
        ).fetchone()[0]
        print(f"memory_cards={len(cards)} old_card_embeddings={old_count}")
        prepared, dimension = await prepare_embeddings(cards, llm, md)
        replace_card_embeddings(conn, prepared)
        new_count = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE kind = 'card'"
        ).fetchone()[0]
    finally:
        conn.close()
    print(f"DONE cards={len(cards)} embeddings={new_count} dimension={dimension}")


if __name__ == "__main__":
    asyncio.run(main())
