from __future__ import annotations

import random

from config import AppConfig
from domain.state import StateDomain


class CardDomain:
    def __init__(self, config: AppConfig, state_domain: StateDomain):
        self.config = config
        self.state_domain = state_domain

    def state_bar(self, value: float) -> str:
        value = max(0.0, min(100.0, value))
        filled = int(round(value / 100.0 * self.config.card_bar_width))
        filled = max(0, min(self.config.card_bar_width, filled))
        return (
            self.config.card_bar_filled * filled
            + self.config.card_bar_empty * (self.config.card_bar_width - filled)
        )

    def render_state_bars(self, state: dict) -> str:
        lines: list[str] = []
        if self.config.card_bars_header:
            lines.append(self.config.card_bars_header)
        for dim in self.config.state_numeric_keys:
            label = self.config.card_bar_labels.get(dim)
            if not label:
                continue
            raw = float(state.get(dim, 50.0))
            shown = 100.0 - raw if dim == "hunger" else raw
            lines.append(f"{label}  {self.state_bar(shown)}  `{int(round(shown))}`")
        vibe = (state.get("recent_vibe") or "").strip()
        if vibe:
            lines.append(self.config.card_vibe_template.format(vibe=vibe))
        return "\n".join(lines)

    def pick_card_actions(self, state: dict) -> list[str]:
        needed: list[tuple[int, str]] = []
        for key, cfg in self.config.card_actions.items():
            dim = cfg.get("need_dim")
            side = cfg.get("need_side")
            if not dim or not side:
                continue
            band = self.state_domain.state_band(dim, float(state.get(dim, 50.0)))
            if not band:
                continue
            is_high = band.endswith("_high")
            is_low = band.endswith("_low")
            if not ((side == "high" and is_high) or (side == "low" and is_low)):
                continue
            severity = 2 if "extreme" in band else 1
            needed.append((severity, key))
        if needed:
            needed.sort(key=lambda t: -t[0])
            return [key for _, key in needed][: self.config.card_max_buttons]
        pool = [key for key in self.config.card_default_actions if key in self.config.card_actions]
        n = min(self.config.card_max_buttons, len(pool))
        return random.sample(pool, n) if n else []

    def build_pet_card(
        self,
        pet_id: int,
        text: str,
        state: dict,
        *,
        with_actions: bool = True,
        action_keys: list[str] | None = None,
        built_at: int | None = None,
        base_text: str | None = None,
    ) -> dict:
        import time

        if built_at is None:
            built_at = int(time.time())
        if base_text is None:
            base_text = text
        is_fixed = action_keys is not None
        elements: list[dict] = [{"tag": "markdown", "content": text or "…"}]
        bars = self.render_state_bars(state)
        if bars:
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": bars})
        if with_actions:
            keys = self.pick_card_actions(state) if action_keys is None else action_keys
            buttons = []
            for key in keys:
                btn_text = self.config.card_action_text.get(key, {}).get("button", key)
                buttons.append(
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": btn_text},
                        "type": "primary",
                        "value": {
                            "pet_id": pet_id,
                            "action": key,
                            "keys": list(keys),
                            "fixed": is_fixed,
                            "built_at": built_at,
                            "base": base_text,
                        },
                    }
                )
            if buttons:
                elements.append({"tag": "hr"})
                elements.append({"tag": "action", "actions": buttons})
        return {"config": {"wide_screen_mode": True}, "elements": elements}

    def rebuild_action_card(
        self,
        pet_id: int,
        text: str,
        state: dict,
        card_keys: list[str] | None,
        is_fixed: bool,
        built_at: int | None,
        base_text: str,
    ) -> dict:
        if is_fixed and card_keys:
            return self.build_pet_card(
                pet_id,
                text,
                state,
                action_keys=card_keys,
                built_at=built_at,
                base_text=base_text,
            )
        return self.build_pet_card(
            pet_id, text, state, built_at=built_at, base_text=base_text
        )

    def compose_card_text(self, base_text: str, lines: list[str], pending: str = "") -> str:
        parts = [base_text] if base_text else []
        for line in lines:
            parts.append(self.config.card_followup_prefix + line)
        if pending:
            parts.append(self.config.card_followup_prefix + pending)
        return "\n".join(part for part in parts if part) or "…"

    def prune_card_followup_buffer(self, buffer: dict[str, dict], now: float) -> None:
        if self.config.card_button_ttl_sec <= 0:
            return
        stale = [
            message_id
            for message_id, entry in buffer.items()
            if isinstance(entry.get("built_at"), (int, float))
            and now - entry["built_at"] >= self.config.card_button_ttl_sec
        ]
        for message_id in stale:
            buffer.pop(message_id, None)

    def prune_card_click_ts(self, click_ts: dict, now: float) -> dict:
        if self.config.card_action_cooldown_sec <= 0:
            return {}
        return {
            open_id: ts
            for open_id, ts in click_ts.items()
            if isinstance(ts, (int, float))
            and now - ts < self.config.card_action_cooldown_sec
        }

