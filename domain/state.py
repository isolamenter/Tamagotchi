from __future__ import annotations

import time

from config import AppConfig


class StateDomain:
    def __init__(self, config: AppConfig):
        self.config = config

    def initial_state(self) -> dict:
        return {
            "last_update_ts": time.time(),
            "last_reply_ts": 0.0,
            "last_proactive_ts": 0.0,
            "last_dream_date": "",
            "last_diary_date": "",
            "last_proactive_topic": "",
            "last_proactive_answered": True,
        }

    def normalize_state(self, stored: dict | None) -> dict:
        """补齐运行字段；移除旧的 active_need，保留其它未知字段防丢失。"""
        raw = dict(stored) if isinstance(stored, dict) else {}
        base = self.initial_state()
        base.update(raw)
        # 结清历史残留的五维照料需求
        base.pop("active_need", None)
        base.pop("need_cooldowns", None)
        base.pop("card_action_ts", None)
        for key in ("last_update_ts", "last_proactive_ts", "last_reply_ts"):
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

    def public_state(self, state: dict) -> dict:
        return {
            "last_update_ts": state.get("last_update_ts"),
            "last_proactive_ts": state.get("last_proactive_ts"),
            "last_reply_ts": state.get("last_reply_ts"),
            "last_dream_date": state.get("last_dream_date", ""),
            "last_diary_date": state.get("last_diary_date", ""),
            "last_proactive_topic": state.get("last_proactive_topic", ""),
            "last_proactive_answered": state.get("last_proactive_answered", True),
        }
