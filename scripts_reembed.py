"""一次性重嵌脚本：用当前 .env 的 EMBED_MODEL / provider 重算所有 card 向量。

换 embedding 模型或 provider 后跑一次。直接读 embeddings.content 重算 vec 覆盖，
不重建 memory_cards。在 ~/tamagotchi 目录下用 venv 跑：
    .venv/bin/python scripts_reembed.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

from config import load_config
from domain.memory import MemoryDomain
from integrations.llm_client import LLMClient


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


async def embed_with_retry(llm: LLMClient, text: str, tries: int = 4):
    for i in range(tries):
        vec = await llm.embed_text(text)
        if vec:
            return vec
        await asyncio.sleep(2 * (i + 1))
    return None


async def main() -> None:
    cfg = load_config(load_env(".env"))
    print(f"provider={cfg.llm_provider} embed_model={cfg.embed_model}")
    llm = LLMClient(cfg)
    md = MemoryDomain(cfg)

    conn = sqlite3.connect("state.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, content, length(vec) AS old_bytes FROM embeddings WHERE kind='card' ORDER BY id"
    ).fetchall()
    print(f"card embeddings to reembed: {len(rows)}")

    ok = fail = 0
    last_dim = None
    for r in rows:
        vec = await embed_with_retry(llm, r["content"])
        if not vec:
            fail += 1
            print(f"  FAIL id={r['id']}")
            continue
        last_dim = len(vec)
        conn.execute(
            "UPDATE embeddings SET vec=?, ts=? WHERE id=?",
            (md.vec_pack(vec), time.time(), r["id"]),
        )
        ok += 1
        if ok % 50 == 0:
            conn.commit()
            print(f"  committed {ok}")
    conn.commit()
    conn.close()
    print(f"DONE ok={ok} fail={fail} new_dim={last_dim} (old was {3072})")


if __name__ == "__main__":
    asyncio.run(main())
