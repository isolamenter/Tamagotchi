from __future__ import annotations

import json
import math
import struct

from dafeiyu.config import AppConfig


class MemoryDomain:
    def __init__(self, config: AppConfig):
        self.config = config

    def vec_pack(self, vec: list[float]) -> bytes:
        if not vec or not all(math.isfinite(value) for value in vec):
            raise ValueError("Cannot store an empty or non-finite vector")
        return struct.pack(f"<{len(vec)}f", *vec)

    def vec_unpack(self, blob: bytes) -> list[float]:
        return list(struct.unpack(f"<{len(blob) // 4}f", blob))

    def cosine(self, a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        denominator = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
        return sum(x * y for x, y in zip(a, b, strict=True)) / denominator if denominator else 0.0

    def render_recall_block(self, cards: list[dict]) -> str:
        if not cards:
            return ""
        sorted_cards = sorted(cards, key=lambda card: (-card.get("score", 0), -card.get("id", 0)))
        lines = []
        for card in sorted_cards:
            kind_str = card.get("kind") or "事实"
            if not card.get("subject_ids") and kind_str != "imagination":
                kind_str = "未归属历史线索（非当前个人事实）"
            line = self.config.recall_card_template.format(
                kind=kind_str,
                when=card.get("when") or "某时",
                who=card.get("who") or "群里",
                what=(card.get("what") or "").strip(),
                vibe=(card.get("vibe") or "").strip(),
            )
            subjects = card.get("subject_ids", [])
            meta = {
                "subject_ids": subjects,
                "reporter_ids": card.get("reporter_ids", []),
                "confidence": card.get("confidence", "uncertain"),
                "original_kind": card.get("kind", ""),
                "profile_key": card.get("profile_key", ""),
                "expires_at": card.get("expires_at"),
                "sources": [
                    {"id": e.get("source_message_id", ""), "ts": e.get("ts")}
                    for e in card.get("evidence", [])
                ],
            }
            line += " 【归属元数据；无主体ID的旧记忆不得按同名认领】" + json.dumps(
                meta, ensure_ascii=False
            )
            lines.append(line)
        return self.config.recall_header + "\n".join(lines)

    def format_card_for_embed(self, card: dict) -> str:
        parts = []
        for key in ("kind", "when", "who", "what", "vibe", "hooks"):
            value = (card.get(key) or "").strip()
            if value:
                parts.append(value)
        return " | ".join(parts)
