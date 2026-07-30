from __future__ import annotations

import math
import random
import time

from config import AppConfig


class StateDomain:
    def __init__(self, config: AppConfig):
        self.config = config

    @property
    def numeric_keys(self) -> tuple[str, ...]:
        return self.config.state_numeric_keys

    def initial_state(self) -> dict:
        return {
            **self.config.initial_state,
            "last_update_ts": time.time(),
            "last_proactive_ts": 0.0,
            "last_reply_ts": 0.0,
            "last_social_ts": 0.0,
            "last_free_card_ts": 0.0,
            "last_dream_date": "",
            "last_diary_date": "",
            "recent_vibe": "",
            "recent_vibe_date": "",
            "active_need": {},
            "need_cooldowns": {},
            "card_action_ts": {},
        }

    def normalize_state(self, stored: dict | None) -> dict:
        """补齐演进中的运行字段，同时保留未知字段以避免线上 state 丢失。"""
        raw = dict(stored) if isinstance(stored, dict) else {}
        base = self.initial_state()
        base.update(raw)
        for key in self.config.state_numeric_keys:
            try:
                base[key] = max(0.0, min(100.0, float(base.get(key, self.config.initial_state[key]))))
            except (TypeError, ValueError):
                base[key] = self.config.initial_state[key]
        for key in ("active_need", "need_cooldowns", "card_action_ts"):
            if not isinstance(base.get(key), dict):
                base[key] = {}
        for key in (
            "last_update_ts",
            "last_proactive_ts",
            "last_reply_ts",
            "last_social_ts",
            "last_free_card_ts",
        ):
            try:
                base[key] = float(base.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                base[key] = 0.0
        return base

    def local_time(self, now_ts: float) -> time.struct_time:
        return time.gmtime(now_ts + self.config.proactive_tz_offset_hours * 3600)

    def local_date_hour(self, now_ts: float) -> tuple[str, int]:
        local = self.local_time(now_ts)
        return time.strftime("%Y-%m-%d", local), local.tm_hour

    def local_hour(self, now_ts: float) -> int:
        return self.local_date_hour(now_ts)[1]

    def quiet_by_hour(self, hour: int) -> bool:
        qs, qe = self.config.quiet_hours
        return qs <= hour < qe if qs < qe else (hour >= qs or hour < qe)

    def is_weekend_rest(self, now_ts: float) -> bool:
        return self.config.quiet_weekends and self.local_time(now_ts).tm_wday >= 5

    def in_quiet_hours(self, now_ts: float) -> bool:
        local = self.local_time(now_ts)
        return self.is_weekend_rest(now_ts) or self.quiet_by_hour(local.tm_hour)

    def partition_hours(self, t_start: float, t_end: float) -> tuple[float, float]:
        total_hours = (t_end - t_start) / 3600.0
        if total_hours <= 0:
            return 0.0, 0.0

        q_hours = 0.0
        a_hours = 0.0
        current = t_start
        tz_offset_sec = self.config.proactive_tz_offset_hours * 3600
        while current < t_end:
            local = self.local_time(current)
            seconds_into_hour = (current + tz_offset_sec) % 3600
            step_sec = 3600 - seconds_into_hour if seconds_into_hour else 3600
            actual_step = min(step_sec, t_end - current) / 3600.0
            is_quiet = (
                self.config.quiet_weekends and local.tm_wday >= 5
            ) or self.quiet_by_hour(local.tm_hour)
            if is_quiet:
                q_hours += actual_step
            else:
                a_hours += actual_step
            current += actual_step * 3600.0

        return q_hours, a_hours

    def maybe_rotate_vibe(self, state: dict, now: float, pet_id: int | None = None) -> dict:
        if not self.config.recent_vibe_pool:
            return state
        date_key, _ = self.local_date_hour(now)
        if (
            state.get("recent_vibe_date") == date_key
            and state.get("recent_vibe") in self.config.recent_vibe_pool
        ):
            return state
        out = dict(state)
        if pet_id is not None:
            r = random.Random(f"{pet_id}-{date_key}")
            out["recent_vibe"] = r.choice(self.config.recent_vibe_pool)
        else:
            out["recent_vibe"] = random.choice(self.config.recent_vibe_pool)
        out["recent_vibe_date"] = date_key
        return out

    def decay_state(self, stored: dict, now: float, pet_id: int | None = None) -> dict:
        last_update_ts = float(stored.get("last_update_ts", now))
        elapsed_hours = max(0.0, (now - last_update_ts) / 3600.0)

        result = dict(stored)
        result["last_update_ts"] = now

        if elapsed_hours <= 0:
            return self.maybe_rotate_vibe(result, now, pet_id)

        q_hours, a_hours = self.partition_hours(last_update_ts, now)

        for key in self.config.state_numeric_keys:
            value = float(stored.get(key, self.config.initial_state.get(key, 50.0)))
            for regime, hours in (
                (self.config.decay_active, a_hours),
                (self.config.decay_quiet, q_hours),
            ):
                if hours <= 0:
                    continue
                cfg = regime.get(key)
                if not cfg:
                    continue
                baseline = cfg["baseline"]
                value = baseline + (value - baseline) * math.exp(-cfg["rate"] * hours)
            result[key] = max(0.0, min(100.0, value))

        return self.maybe_rotate_vibe(result, now, pet_id)

    def state_band(self, dim: str, value: float) -> str | None:
        bands = self.config.state_bands.get(dim)
        if not bands:
            return None
        eh = bands.get("extreme_high")
        h = bands.get("high")
        el = bands.get("extreme_low")
        lo = bands.get("low")
        if eh is not None and value >= float(eh):
            return f"{dim}_extreme_high"
        if h is not None and value >= float(h):
            return f"{dim}_high"
        if el is not None and value <= float(el):
            return f"{dim}_extreme_low"
        if lo is not None and value <= float(lo):
            return f"{dim}_low"
        return None

    def render_state(self, state: dict, *, include_vibe: bool = True) -> str:
        lines: list[str] = []
        for dim in self.config.state_numeric_keys:
            band_key = self.state_band(dim, float(state.get(dim, 50.0)))
            if not band_key:
                continue
            sentence = self.config.state_render_lines.get(band_key)
            if sentence:
                lines.append(sentence)
        vibe = (state.get("recent_vibe") or "").strip()
        # A concrete gameplay state already gives the model enough color.  Do
        # not stack the daily vibe on top, which otherwise turns one phrase
        # into a repeated answer theme across unrelated messages.
        if include_vibe and vibe and not lines:
            lines.append(self.config.state_render_vibe_template.format(vibe=vibe))
        if not lines:
            return ""
        return self.config.state_render_header + "\n".join(
            self.config.state_render_line_prefix + line for line in lines
        )

    def public_state(self, state: dict) -> dict:
        out: dict = {}
        for key in self.config.state_numeric_keys:
            out[key] = round(float(state.get(key, 0)), 1)
        out["recent_vibe"] = state.get("recent_vibe", "")
        out["recent_vibe_date"] = state.get("recent_vibe_date", "")
        out["last_update_ts"] = state.get("last_update_ts")
        out["last_proactive_ts"] = state.get("last_proactive_ts")
        out["last_reply_ts"] = state.get("last_reply_ts")
        out["last_social_ts"] = state.get("last_social_ts")
        out["last_free_card_ts"] = state.get("last_free_card_ts")
        out["last_dream_date"] = state.get("last_dream_date", "")
        out["last_diary_date"] = state.get("last_diary_date", "")
        active_need = state.get("active_need") if isinstance(state.get("active_need"), dict) else {}
        if active_need and not active_need.get("resolved"):
            out["active_need"] = {
                "id": active_need.get("id", ""),
                "kind": active_need.get("kind", ""),
                "title": active_need.get("title", ""),
                "description": active_need.get("description", ""),
                "created_at": active_need.get("created_at"),
                "expires_at": active_need.get("expires_at"),
                "severity": active_need.get("severity", 1),
                "round": active_need.get("round", 0),
                "status": active_need.get("status", "open"),
                "paused_at": active_need.get("paused_at"),
                "announced_card_id": active_need.get("announced_card_id", ""),
                "source": active_need.get("source", ""),
            }
        else:
            out["active_need"] = {}
        return out
