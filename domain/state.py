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
            "last_dream_date": "",
            "last_diary_date": "",
            "recent_vibe": "",
            "recent_vibe_date": "",
        }

    def local_date_hour(self, now_ts: float) -> tuple[str, int]:
        local = time.gmtime(now_ts + self.config.proactive_tz_offset_hours * 3600)
        return time.strftime("%Y-%m-%d", local), local.tm_hour

    def local_hour(self, now_ts: float) -> int:
        return self.local_date_hour(now_ts)[1]

    def in_quiet_hours(self, now_ts: float) -> bool:
        h = self.local_hour(now_ts)
        qs, qe = self.config.quiet_hours
        return qs <= h < qe if qs < qe else (h >= qs or h < qe)

    def partition_hours(self, t_start: float, t_end: float) -> tuple[float, float]:
        total_hours = (t_end - t_start) / 3600.0
        if total_hours <= 0:
            return 0.0, 0.0

        qs, qe = self.config.quiet_hours
        if qs < qe:
            quiet_hours_per_day = float(qe - qs)
        else:
            quiet_hours_per_day = float((24 - qs) + qe)
        active_hours_per_day = 24.0 - quiet_hours_per_day

        days = int(total_hours // 24)
        q_hours = days * quiet_hours_per_day
        a_hours = days * active_hours_per_day

        rem_start = t_start + days * 24 * 3600
        step = 1.0
        current = rem_start
        while current < t_end:
            lh = self.local_hour(current)
            is_quiet = qs <= lh < qe if qs < qe else (lh >= qs or lh < qe)
            actual_step = min(step, (t_end - current) / 3600.0)
            if is_quiet:
                q_hours += actual_step
            else:
                a_hours += actual_step
            current += actual_step * 3600

        return q_hours, a_hours

    def maybe_rotate_vibe(self, state: dict, now: float, pet_id: int | None = None) -> dict:
        if not self.config.recent_vibe_pool:
            return state
        date_key, _ = self.local_date_hour(now)
        if state.get("recent_vibe_date") == date_key and state.get("recent_vibe"):
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

    def apply_delta(self, state: dict, delta: dict) -> dict:
        out = dict(state)
        for key in self.config.state_numeric_keys:
            try:
                d = int(delta.get(key, 0))
            except (TypeError, ValueError):
                d = 0
            clamp = self.config.state_delta_clamp.get(key, self.config.default_delta_clamp)
            d = max(-clamp, min(clamp, d))
            out[key] = max(
                0.0,
                min(100.0, float(out.get(key, self.config.initial_state.get(key, 50.0))) + d),
            )
        return out

    def apply_card_delta(self, state: dict, delta: dict) -> dict:
        out = dict(state)
        for key in self.config.state_numeric_keys:
            try:
                d = float(delta.get(key, 0))
            except (TypeError, ValueError):
                d = 0.0
            out[key] = max(
                0.0,
                min(100.0, float(out.get(key, self.config.initial_state.get(key, 50.0))) + d),
            )
        return out

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

    def render_state(self, state: dict) -> str:
        lines: list[str] = []
        for dim in self.config.state_numeric_keys:
            band_key = self.state_band(dim, float(state.get(dim, 50.0)))
            if not band_key:
                continue
            sentence = self.config.state_render_lines.get(band_key)
            if sentence:
                lines.append(sentence)
        vibe = (state.get("recent_vibe") or "").strip()
        if vibe:
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
        out["last_dream_date"] = state.get("last_dream_date", "")
        out["last_diary_date"] = state.get("last_diary_date", "")
        return out

