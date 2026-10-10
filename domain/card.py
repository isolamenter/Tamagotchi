from __future__ import annotations

import uuid
from typing import Any

from config import AppConfig


class CardDomain:
    def __init__(self, config: AppConfig):
        self.config = config

    def build_pet_card(
        self,
        pet_id: int,
        text: str,
        *,
        action_keys: list[str] | None = None,
        built_at: int | None = None,
        base_text: str | None = None,
        img_key: str | None = None,
        card_id: str | None = None,
        mode: str = "content",
    ) -> dict[str, Any]:
        import time

        if built_at is None:
            built_at = int(time.time())
        if base_text is None:
            base_text = text
        card_id = card_id or f"card-{uuid.uuid4().hex}"

        elements: list[dict[str, Any]] = [{"tag": "markdown", "content": text or "…"}]
        if img_key:
            elements.append(
                {
                    "tag": "img",
                    "img_key": img_key,
                    "alt": {"tag": "plain_text", "content": ""},
                }
            )

        if action_keys:
            buttons = []
            for key in action_keys:
                buttons.append(
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": key},
                        "type": "primary",
                        "value": {
                            "v": 2,
                            "pet_id": pet_id,
                            "card_id": card_id,
                            "action": key,
                            "built_at": built_at,
                            "base": base_text,
                            "img_key": img_key,
                        },
                    }
                )
            if buttons:
                elements.append({"tag": "hr"})
                elements.append({"tag": "action", "actions": buttons})

        return {"config": {"wide_screen_mode": True}, "elements": elements}

    def compose_card_text(self, base_text: str, lines: list[str]) -> str:
        parts = [base_text] if base_text else []
        for line in lines:
            parts.append(f"↳ {line}")
        return "\n".join(part for part in parts if part) or "…"
