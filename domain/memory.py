from __future__ import annotations

import struct

from config import AppConfig


class MemoryDomain:
    def __init__(self, config: AppConfig):
        self.config = config

    def vec_pack(self, vec: list[float]) -> bytes:
        return struct.pack(f"<{len(vec)}f", *vec)

    def vec_unpack(self, blob: bytes) -> list[float]:
        return list(struct.unpack(f"<{len(blob) // 4}f", blob))

    def cosine(self, a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        return sum(x * y for x, y in zip(a, b))

    def render_recall_block(self, cards: list[dict]) -> str:
        if not cards:
            return ""
        sorted_cards = sorted(cards, key=lambda card: card.get("id", 0))
        lines = []
        for card in sorted_cards:
            kind_str = card.get("kind") or "事实"
            # Format according to recall_card_template: "- [{kind}] {when}｜{who}｜{what}（{vibe}）"
            # If kind isn't in template, safely fall back
            try:
                line = self.config.recall_card_template.format(
                    kind=kind_str,
                    when=(card.get("when") or "某时"),
                    who=(card.get("who") or "群里"),
                    what=(card.get("what") or "").strip(),
                    vibe=(card.get("vibe") or "").strip(),
                )
            except KeyError:
                line = f"- [{kind_str}] {card.get('when', '某时')}｜{card.get('who', '群里')}｜{(card.get('what') or '').strip()}（{(card.get('vibe') or '').strip()}）"
            lines.append(line)
        return self.config.recall_header + "\n".join(lines)

    def format_card_for_embed(self, card: dict) -> str:
        parts = []
        for key in ("kind", "when", "who", "what", "vibe", "hooks"):
            value = (card.get(key) or "").strip()
            if value:
                parts.append(value)
        return " | ".join(parts)
