from __future__ import annotations

import random
import time
import uuid

from config import AppConfig
from domain.gameplay import GameplayDomain
from domain.state import StateDomain


class CardDomain:
    def __init__(
        self,
        config: AppConfig,
        state_domain: StateDomain,
        gameplay_domain: GameplayDomain | None = None,
    ):
        self.config = config
        self.state_domain = state_domain
        self.gameplay_domain = gameplay_domain

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
            shown = float(state.get(dim, 50.0))
            lines.append(f"{label}  {self.state_bar(shown)}  `{int(round(shown))}`")
        vibe = (state.get("recent_vibe") or "").strip()
        if vibe:
            lines.append(self.config.card_vibe_template.format(vibe=vibe))
        return "\n".join(lines)

    def render_gameplay_status(self, state: dict, now: float) -> str:
        if not self.gameplay_domain:
            return ""
        lines: list[str] = []
        need = self.gameplay_domain.current_need(state, now)
        if need:
            urgency = "**紧急** · " if int(need.get("severity", 1) or 1) >= 2 else ""
            lines.append(f"{urgency}**{need.get('title', '需要照料')}**")
            desc = (need.get("description") or "").strip()
            if desc:
                lines.append(desc)
        return "\n".join(line for line in lines if line)

    def pick_card_actions(self, state: dict) -> list[str]:
        if self.gameplay_domain:
            need = self.gameplay_domain.current_need(state, time.time())
            if need:
                return self.gameplay_domain.choice_keys_for_need(need["kind"])[
                    : self.config.card_max_buttons
                ]
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
        img_key: str | None = None,
        card_id: str | None = None,
        mode: str | None = None,
        need_round: int = 0,
        expires_at: float | None = None,
    ) -> dict:
        import time

        if built_at is None:
            built_at = int(time.time())
        if base_text is None:
            base_text = text
        card_id = card_id or f"card-{uuid.uuid4().hex}"
        expires_at = expires_at if expires_at is not None else built_at + self.config.card_button_ttl_sec
        is_fixed = action_keys is not None
        active_need_for_mode = (
            self.gameplay_domain.current_need(state, float(built_at)) if self.gameplay_domain else {}
        )
        mode = mode or ("need" if active_need_for_mode and not is_fixed else "scheduled" if is_fixed else "free")
        elements: list[dict] = [{"tag": "markdown", "content": text or "…"}]
        if img_key:
            elements.append(
                {
                    "tag": "img",
                    "img_key": img_key,
                    "alt": {"tag": "plain_text", "content": ""},
                }
            )
        bars = self.render_state_bars(state)
        gameplay = self.render_gameplay_status(state, float(built_at))
        if gameplay and action_keys is None:
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": gameplay})
        if bars:
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": bars})
        if with_actions:
            keys = self.pick_card_actions(state) if action_keys is None else action_keys
            buttons = []
            active_need = active_need_for_mode if mode == "need" else {}
            hints: list[str] = []
            for key in keys:
                gameplay_text = (
                    self.gameplay_domain.action_text(key, active_need.get("kind"))
                    if self.gameplay_domain and active_need
                    else {}
                )
                btn_text = gameplay_text.get(
                    "button", self.config.card_action_text.get(key, {}).get("button", key)
                )
                hint = gameplay_text.get("effect_hint", "")
                if hint:
                    hints.append(f"- {btn_text}：{hint}")
                buttons.append(
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": btn_text},
                        "type": "primary",
                        "value": {
                            "v": 2,
                            "pet_id": pet_id,
                            "card_id": card_id,
                            "mode": mode,
                            "action": key,
                            "need_id": active_need.get("id", ""),
                            "need_kind": active_need.get("kind", ""),
                            "need_round": need_round,
                            "expires_at": expires_at,
                            "keys": list(keys),
                            "fixed": is_fixed,
                            "built_at": built_at,
                            "base": base_text,
                            "img_key": img_key,
                        },
                    }
                )
            if buttons:
                elements.append({"tag": "hr"})
                if hints:
                    elements.append({"tag": "markdown", "content": "\n".join(hints)})
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
        img_key: str | None = None,
    ) -> dict:
        if is_fixed and card_keys:
            return self.build_pet_card(
                pet_id,
                text,
                state,
                action_keys=card_keys,
                built_at=built_at,
                base_text=base_text,
                img_key=img_key,
            )
        return self.build_pet_card(
            pet_id, text, state, built_at=built_at, base_text=base_text, img_key=img_key
        )

    def compose_card_text(self, base_text: str, lines: list[str], pending: str = "") -> str:
        parts = [base_text] if base_text else []
        for line in lines:
            parts.append(self.config.card_followup_prefix + line)
        if pending:
            parts.append(self.config.card_followup_prefix + pending)
        return "\n".join(part for part in parts if part) or "…"
