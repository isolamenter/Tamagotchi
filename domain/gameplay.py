from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass
from typing import Any

from config import AppConfig


NEED_ORDER = ("hungry", "sleepy", "sad", "bored", "lonely")

NEED_SPECS: dict[str, dict[str, str]] = {
    "hungry": {
        "dim": "hunger",
        "side": "high",
        "title": "饿肚子",
        "description": "它闻起来像快把梦里的面包都啃完了。",
    },
    "sleepy": {
        "dim": "energy",
        "side": "low",
        "title": "困到摇晃",
        "description": "它的眼皮快合上了，还努力扒着群聊边缘。",
    },
    "sad": {
        "dim": "mood",
        "side": "low",
        "title": "心情打结",
        "description": "它把小情绪团成一团，等人轻轻拆开。",
    },
    "bored": {
        "dim": "curiosity",
        "side": "low",
        "title": "无聊冒泡",
        "description": "它盯着空气发呆，想要一点新鲜响动。",
    },
    "lonely": {
        "dim": "affection",
        "side": "low",
        "title": "想被注意",
        "description": "它在门口探头探脑，像在确认大家还记得它。",
    },
}

CHOICE_RULES: dict[str, tuple[dict[str, Any], ...]] = {
    "hungry": (
        {
            "action": "feed",
            "button": "🍖 投喂",
            "did": "给你喂了点吃的",
            "pending": "（小口小口……饿意终于退下去一点）",
            "delta": {"hunger": -35, "mood": 4, "affection": 1},
        },
        {
            "action": "snack_hunt",
            "button": "🌃 找夜宵",
            "did": "带你去找夜宵",
            "pending": "（闻着味道一路小跑，眼睛亮了起来）",
            "delta": {"hunger": -20, "curiosity": 8, "energy": -5},
        },
        {
            "action": "promise_food",
            "button": "🫓 画饼充饥",
            "did": "给你画了一张很香的饼",
            "pending": "（认真盯着那张饼，半信半疑地咽了咽口水）",
            "delta": {"hunger": -8, "curiosity": 4},
            "random_delta": {"mood": (-3, 6)},
        },
    ),
    "sleepy": (
        {
            "action": "rest",
            "button": "💤 哄睡",
            "did": "哄你休息了一会儿",
            "pending": "（打了个哈欠……终于能安心眯一会儿）",
            "delta": {"energy": 30, "hunger": 5, "mood": 2},
        },
        {
            "action": "quiet_story",
            "button": "📖 讲睡前故事",
            "did": "给你讲了一个很轻的睡前故事",
            "pending": "（把故事叼进梦里，尾音慢慢变轻）",
            "delta": {"energy": 18, "mood": 6, "curiosity": 3},
        },
        {
            "action": "tuck_in",
            "button": "🛏️ 盖小被子",
            "did": "给你盖好了小被子",
            "pending": "（缩进被角，只露出一点安心的呼吸）",
            "delta": {"energy": 25, "affection": 3},
        },
    ),
    "sad": (
        {
            "action": "soothe",
            "button": "🫧 哄一哄",
            "did": "轻声哄了哄你",
            "pending": "（闷闷的心情松开了一点）",
            "delta": {"mood": 25, "affection": 3},
        },
        {
            "action": "listen",
            "button": "👂 听它碎碎念",
            "did": "认真听你碎碎念了一会儿",
            "pending": "（被听见以后，委屈少了一小块）",
            "delta": {"mood": 16, "affection": 5, "energy": -2},
        },
        {
            "action": "make_joke",
            "button": "🎭 逗它笑",
            "did": "努力把你逗笑",
            "pending": "（先绷着脸，最后还是噗地笑了一声）",
            "delta": {"mood": 14, "curiosity": 5, "energy": -4},
        },
    ),
    "bored": (
        {
            "action": "play",
            "button": "🎲 逗它玩",
            "did": "陪你玩了个小游戏",
            "pending": "（一下蹦起来——有人陪我玩啦！）",
            "delta": {"curiosity": 25, "mood": 8, "energy": -8, "hunger": 5},
        },
        {
            "action": "tell_news",
            "button": "🗞️ 讲新鲜事",
            "did": "给你讲了一个新鲜事",
            "pending": "（耳朵立起来，开始追问后续）",
            "delta": {"curiosity": 18, "affection": 2},
        },
        {
            "action": "send_explore",
            "button": "🧭 派去探险",
            "did": "派你去附近探险",
            "pending": "（叼着小地图冲出去，又带着亮晶晶的眼神回来）",
            "delta": {"curiosity": 12, "energy": -10, "mood": 5},
        },
    ),
    "lonely": (
        {
            "action": "pet",
            "button": "🤚 摸摸头",
            "did": "摸了摸你的头",
            "pending": "（眯起眼睛……被注意到的时候最安心了）",
            "delta": {"affection": 20, "mood": 5},
        },
        {
            "action": "sit_together",
            "button": "🪑 陪它坐会儿",
            "did": "安静陪你坐了一会儿",
            "pending": "（靠近一点点，确认这里还有自己的位置）",
            "delta": {"affection": 16, "mood": 7, "energy": 2},
        },
        {
            "action": "call_name",
            "button": "📣 喊它过来",
            "did": "喊你过来一起待着",
            "pending": "（听见有人叫，立刻探出头来）",
            "delta": {"affection": 12, "curiosity": 4, "mood": 4},
        },
    ),
}

@dataclass(frozen=True)
class GameplayResult:
    state: dict
    need: dict
    action: str
    did: str
    pending: str
    delta: dict[str, float]


class GameplayDomain:
    def __init__(self, config: AppConfig):
        self.config = config

    def current_need(self, state: dict, now: float | None = None) -> dict:
        need = state["active_need"]
        if not need or need.get("resolved"):
            return {}
        if now is not None and float(need.get("expires_at") or 0) <= now:
            return {}
        if need.get("kind") not in NEED_SPECS:
            return {}
        return need

    def expired_need_cleared(self, state: dict, now: float) -> dict:
        out = dict(state)
        need = out["active_need"]
        if need and not need.get("resolved") and float(need.get("expires_at") or 0) <= now:
            out["active_need"] = {}
        return out

    def detect_need_kind(self, state: dict, now: float) -> tuple[str, int] | None:
        if not self.config.gameplay_enabled:
            return None
        state = self.expired_need_cleared(state, now)
        if self.current_need(state, now):
            return None
        cooldowns = state["need_cooldowns"]
        candidates: list[tuple[int, int, str]] = []
        for idx, kind in enumerate(NEED_ORDER):
            raw_until = cooldowns.get(kind, 0)
            try:
                if float(raw_until) > now:
                    continue
            except (TypeError, ValueError):
                pass
            spec = NEED_SPECS[kind]
            dim = spec["dim"]
            value = float(state.get(dim, self.config.initial_state.get(dim, 50.0)))
            threshold = self.config.gameplay_need_thresholds.get(kind)
            if threshold is None:
                continue
            if spec["side"] == "high":
                matched = value >= threshold
                severity = 2 if value >= min(100.0, threshold + 10) else 1
            else:
                matched = value <= threshold
                severity = 2 if value <= max(0.0, threshold - 15) else 1
            if matched:
                candidates.append((-severity, idx, kind))
        if not candidates:
            return None
        candidates.sort()
        kind = candidates[0][2]
        return kind, -candidates[0][0]

    def build_need(self, kind: str, severity: int, now: float) -> dict:
        spec = NEED_SPECS[kind]
        return {
            "id": f"need-{time.strftime('%Y%m%d', time.gmtime(now))}-{uuid.uuid4().hex[:8]}",
            "kind": kind,
            "title": spec["title"],
            "description": spec["description"],
            "created_at": int(now),
            "expires_at": int(now + self.config.gameplay_need_ttl_sec),
            "severity": severity,
            "resolved": False,
            "source": "state_threshold",
        }

    def maybe_create_need(
        self, state: dict, now: float, pet_id: int | None = None
    ) -> tuple[dict, dict | None]:
        out = dict(state)
        out = self.expired_need_cleared(out, now)
        detected = self.detect_need_kind(out, now)
        if detected is None:
            return out, None
        kind, severity = detected
        need = self.build_need(kind, severity, now)
        out["active_need"] = need
        return out, need

    def choice_rules_for_need(self, need_kind: str) -> list[dict[str, Any]]:
        return [dict(item) for item in CHOICE_RULES.get(need_kind, ())]

    def choice_keys_for_need(self, need_kind: str) -> list[str]:
        return [item["action"] for item in self.choice_rules_for_need(need_kind)]

    def choice_for_action(
        self, need_kind: str | None, action: str
    ) -> dict[str, Any] | None:
        if need_kind:
            for item in CHOICE_RULES.get(need_kind, ()):
                if item["action"] == action:
                    return dict(item)
            return None
        for rules in CHOICE_RULES.values():
            for item in rules:
                if item["action"] == action:
                    return dict(item)
        return None

    def action_text(self, action: str, need_kind: str | None = None) -> dict[str, str]:
        choice = self.choice_for_action(need_kind, action)
        if not choice:
            choice = self.free_card_rule(action)
        if not choice:
            return {}
        return {
            "button": str(choice.get("button", action)),
            "did": str(choice.get("did", "")),
            "pending": str(choice.get("pending", "…")),
        }

    def free_card_rule(self, action: str) -> dict[str, Any] | None:
        config_rule = self.config.card_actions.get(action)
        if not config_rule:
            return None
        text_rule = self.config.card_action_text.get(action, {})
        return {
            **dict(config_rule),
            "action": action,
            "button": text_rule.get("button", action),
            "did": text_rule.get("did", "和你互动了一下"),
            "pending": text_rule.get("pending", "…"),
        }

    def apply_choice(
        self, state: dict, action: str, actor_name: str, now: float
    ) -> GameplayResult:
        return self._apply_card_action(
            state, action, actor_name, now, require_active_need=True
        )

    def apply_card_action(
        self,
        state: dict,
        action: str,
        actor_name: str,
        now: float,
        *,
        prefer_free: bool = False,
    ) -> GameplayResult:
        """统一结算所有会改变 state 的卡片按钮。"""
        return self._apply_card_action(
            state,
            action,
            actor_name,
            now,
            require_active_need=False,
            prefer_free=prefer_free,
        )

    def _apply_card_action(
        self,
        state: dict,
        action: str,
        actor_name: str,
        now: float,
        *,
        require_active_need: bool,
        prefer_free: bool = False,
    ) -> GameplayResult:
        out = dict(state)
        need = self.current_need(out, now)
        if not need and require_active_need:
            raise ValueError("no_active_need")

        settle_need = bool(need and not prefer_free)
        if settle_need:
            need_kind = need["kind"]
            rule = self.choice_for_action(need_kind, action)
            if not rule:
                raise ValueError("invalid_need_action")
        else:
            need_kind = ""
            rule = self.free_card_rule(action)
            if not rule:
                raise ValueError("invalid_card_action")

        delta = {k: float(v) for k, v in dict(rule.get("delta") or {}).items()}
        for key, bounds in dict(rule.get("random_delta") or {}).items():
            low, high = bounds
            delta[key] = float(random.randint(int(low), int(high)))

        for key in self.config.state_numeric_keys:
            if key not in delta:
                continue
            out[key] = max(
                0.0,
                min(
                    100.0,
                    float(out.get(key, self.config.initial_state.get(key, 50.0)))
                    + float(delta[key]),
                ),
            )

        if settle_need:
            out["active_need"] = {}
            cooldowns = dict(out["need_cooldowns"])
            cooldowns[need_kind] = now + self.config.gameplay_need_ttl_sec
            out["need_cooldowns"] = cooldowns
        out["last_update_ts"] = now

        return GameplayResult(
            state=out,
            need=dict(need if settle_need else {}),
            action=action,
            did=str(rule.get("did", "")),
            pending=str(rule.get("pending", "…")),
            delta=delta,
        )
