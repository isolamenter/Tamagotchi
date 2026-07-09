from __future__ import annotations

import math
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
            "xp": 8,
            "goal": "be_fed",
        },
        {
            "action": "snack_hunt",
            "button": "🌃 找夜宵",
            "did": "带你去找夜宵",
            "pending": "（闻着味道一路小跑，眼睛亮了起来）",
            "delta": {"hunger": -20, "curiosity": 8, "energy": -5},
            "xp": 10,
        },
        {
            "action": "promise_food",
            "button": "🫓 画饼充饥",
            "did": "给你画了一张很香的饼",
            "pending": "（认真盯着那张饼，半信半疑地咽了咽口水）",
            "delta": {"hunger": -8, "curiosity": 4},
            "random_delta": {"mood": (-3, 6)},
            "xp": 4,
        },
    ),
    "sleepy": (
        {
            "action": "rest",
            "button": "💤 哄睡",
            "did": "哄你休息了一会儿",
            "pending": "（打了个哈欠……终于能安心眯一会儿）",
            "delta": {"energy": 30, "hunger": 5, "mood": 2},
            "xp": 8,
        },
        {
            "action": "quiet_story",
            "button": "📖 讲睡前故事",
            "did": "给你讲了一个很轻的睡前故事",
            "pending": "（把故事叼进梦里，尾音慢慢变轻）",
            "delta": {"energy": 18, "mood": 6, "curiosity": 3},
            "xp": 10,
        },
        {
            "action": "tuck_in",
            "button": "🛏️ 盖小被子",
            "did": "给你盖好了小被子",
            "pending": "（缩进被角，只露出一点安心的呼吸）",
            "delta": {"energy": 25, "affection": 3},
            "xp": 9,
        },
    ),
    "sad": (
        {
            "action": "soothe",
            "button": "🫧 哄一哄",
            "did": "轻声哄了哄你",
            "pending": "（闷闷的心情松开了一点）",
            "delta": {"mood": 25, "affection": 3},
            "xp": 8,
            "goal": "be_comforted",
        },
        {
            "action": "listen",
            "button": "👂 听它碎碎念",
            "did": "认真听你碎碎念了一会儿",
            "pending": "（被听见以后，委屈少了一小块）",
            "delta": {"mood": 16, "affection": 5, "energy": -2},
            "xp": 10,
        },
        {
            "action": "make_joke",
            "button": "🎭 逗它笑",
            "did": "努力把你逗笑",
            "pending": "（先绷着脸，最后还是噗地笑了一声）",
            "delta": {"mood": 14, "curiosity": 5, "energy": -4},
            "xp": 9,
        },
    ),
    "bored": (
        {
            "action": "play",
            "button": "🎲 逗它玩",
            "did": "陪你玩了个小游戏",
            "pending": "（一下蹦起来——有人陪我玩啦！）",
            "delta": {"curiosity": 25, "mood": 8, "energy": -8, "hunger": 5},
            "xp": 8,
            "goal": "play_once",
        },
        {
            "action": "tell_news",
            "button": "🗞️ 讲新鲜事",
            "did": "给你讲了一个新鲜事",
            "pending": "（耳朵立起来，开始追问后续）",
            "delta": {"curiosity": 18, "affection": 2},
            "xp": 10,
            "goal": "hear_news",
        },
        {
            "action": "send_explore",
            "button": "🧭 派去探险",
            "did": "派你去附近探险",
            "pending": "（叼着小地图冲出去，又带着亮晶晶的眼神回来）",
            "delta": {"curiosity": 12, "energy": -10, "mood": 5},
            "xp": 12,
        },
    ),
    "lonely": (
        {
            "action": "pet",
            "button": "🤚 摸摸头",
            "did": "摸了摸你的头",
            "pending": "（眯起眼睛……被注意到的时候最安心了）",
            "delta": {"affection": 20, "mood": 5},
            "xp": 8,
        },
        {
            "action": "sit_together",
            "button": "🪑 陪它坐会儿",
            "did": "安静陪你坐了一会儿",
            "pending": "（靠近一点点，确认这里还有自己的位置）",
            "delta": {"affection": 16, "mood": 7, "energy": 2},
            "xp": 10,
        },
        {
            "action": "call_name",
            "button": "📣 喊它过来",
            "did": "喊你过来一起待着",
            "pending": "（听见有人叫，立刻探出头来）",
            "delta": {"affection": 12, "curiosity": 4, "mood": 4},
            "xp": 7,
        },
    ),
}

DAILY_GOALS: tuple[dict[str, Any], ...] = (
    {
        "kind": "hear_news",
        "title": "想听一个新鲜事",
        "target": 1,
        "actions": ("tell_news",),
    },
    {"kind": "be_fed", "title": "想被投喂一次", "target": 1, "actions": ("feed",)},
    {
        "kind": "be_comforted",
        "title": "想被安慰一下",
        "target": 1,
        "actions": ("soothe",),
    },
    {"kind": "play_once", "title": "想玩一次", "target": 1, "actions": ("play",)},
)


@dataclass(frozen=True)
class GameplayResult:
    state: dict
    need: dict
    action: str
    did: str
    pending: str
    delta: dict[str, float]
    xp: int
    leveled_up: bool
    goal_completed: bool
    log_text: str


class GameplayDomain:
    def __init__(self, config: AppConfig):
        self.config = config

    def default_progress(self) -> dict:
        return {"xp": 0, "level": 1, "total_xp": 0}

    def normalize_state(self, state: dict) -> dict:
        out = dict(state)
        if not isinstance(out.get("active_need"), dict):
            out["active_need"] = {}
        if not isinstance(out.get("daily_goal"), dict):
            out["daily_goal"] = {}
        progress = out.get("progress")
        if not isinstance(progress, dict):
            progress = self.default_progress()
        progress = {
            **self.default_progress(),
            "xp": int(progress.get("xp", 0) or 0),
            "level": int(progress.get("level", 1) or 1),
            "total_xp": int(progress.get("total_xp", 0) or 0),
        }
        progress["level"] = self.level_for_total_xp(progress["total_xp"])
        out["progress"] = progress
        if not isinstance(out.get("state_log"), list):
            out["state_log"] = []
        if not isinstance(out.get("need_cooldowns"), dict):
            out["need_cooldowns"] = {}
        return out

    def level_for_total_xp(self, total_xp: int) -> int:
        return int(math.floor(math.sqrt(max(0, total_xp) / 50.0))) + 1

    def local_date(self, now: float) -> str:
        local = time.gmtime(now + self.config.proactive_tz_offset_hours * 3600)
        return time.strftime("%Y-%m-%d", local)

    def ensure_daily_goal(
        self, state: dict, now: float, pet_id: int | None = None
    ) -> dict:
        out = self.normalize_state(state)
        date_key = self.local_date(now)
        current = out.get("daily_goal") or {}
        if current.get("date") == date_key and current.get("kind"):
            return out

        seed = f"{pet_id or 0}:{date_key}"
        spec = random.Random(seed).choice(DAILY_GOALS)
        out["daily_goal"] = {
            "date": date_key,
            "kind": spec["kind"],
            "title": spec["title"],
            "target": int(spec["target"]),
            "progress": 0,
            "completed": False,
            "reward_xp": self.config.gameplay_daily_goal_reward_xp,
        }
        return out

    def current_need(self, state: dict, now: float | None = None) -> dict:
        need = self.normalize_state(state).get("active_need") or {}
        if not need or need.get("resolved"):
            return {}
        if now is not None and float(need.get("expires_at") or 0) <= now:
            return {}
        if need.get("kind") not in NEED_SPECS:
            return {}
        return need

    def expired_need_cleared(self, state: dict, now: float) -> dict:
        out = self.normalize_state(state)
        need = out.get("active_need") or {}
        if need and not need.get("resolved") and float(need.get("expires_at") or 0) <= now:
            out["active_need"] = {}
        return out

    def detect_need_kind(self, state: dict, now: float) -> tuple[str, int] | None:
        if not self.config.gameplay_enabled:
            return None
        state = self.expired_need_cleared(state, now)
        if self.current_need(state, now):
            return None
        cooldowns = state.get("need_cooldowns") or {}
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
        out = self.ensure_daily_goal(state, now, pet_id)
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
            return {}
        return {
            "button": str(choice.get("button", action)),
            "did": str(choice.get("did", "")),
            "pending": str(choice.get("pending", "…")),
        }

    def apply_choice(
        self, state: dict, action: str, actor_name: str, now: float
    ) -> GameplayResult:
        out = self.ensure_daily_goal(state, now)
        need = self.current_need(out, now)
        if not need:
            raise ValueError("no_active_need")
        need_kind = need["kind"]
        rule = self.choice_for_action(need_kind, action)
        if not rule:
            raise ValueError("invalid_need_action")

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

        xp = int(rule.get("xp", 0) or 0)
        progress = dict(out.get("progress") or self.default_progress())
        old_level = int(progress.get("level", 1) or 1)
        progress["xp"] = int(progress.get("xp", 0) or 0) + xp
        progress["total_xp"] = int(progress.get("total_xp", 0) or 0) + xp
        progress["level"] = self.level_for_total_xp(progress["total_xp"])
        out["progress"] = progress

        goal_completed = self.advance_daily_goal(out, action, rule)
        if goal_completed:
            reward_xp = int(out["daily_goal"].get("reward_xp", 0) or 0)
            progress["xp"] += reward_xp
            progress["total_xp"] += reward_xp
            progress["level"] = self.level_for_total_xp(progress["total_xp"])
            out["progress"] = progress

        out["active_need"] = {}
        cooldowns = dict(out.get("need_cooldowns") or {})
        cooldowns[need_kind] = now + self.config.gameplay_need_ttl_sec
        out["need_cooldowns"] = cooldowns
        out["last_update_ts"] = now

        log_text = self.resolve_log_text(actor_name, rule, need_kind)
        reward_xp = int(out["daily_goal"].get("reward_xp", 0) or 0) if goal_completed else 0
        total_xp = xp + reward_xp
        self.append_log(
            out,
            {
                "ts": int(now),
                "kind": "need_resolved",
                "actor": actor_name,
                "text": log_text,
                "delta": {k: v for k, v in delta.items() if v},
                "xp": xp,
                "need_kind": need_kind,
                "action": action,
            },
        )
        if goal_completed:
            self.append_log(
                out,
                {
                    "ts": int(now),
                    "kind": "daily_goal_completed",
                    "actor": actor_name,
                    "text": f"今日目标「{out['daily_goal'].get('title')}」完成了。",
                    "delta": {},
                    "xp": reward_xp,
                    "goal_kind": out["daily_goal"].get("kind"),
                },
            )

        return GameplayResult(
            state=out,
            need=dict(need),
            action=action,
            did=str(rule.get("did", "")),
            pending=str(rule.get("pending", "…")),
            delta=delta,
            xp=total_xp,
            leveled_up=progress["level"] > old_level,
            goal_completed=goal_completed,
            log_text=log_text,
        )

    def advance_daily_goal(
        self, state: dict, action: str, rule: dict[str, Any] | None = None
    ) -> bool:
        goal = state.get("daily_goal") or {}
        if not goal or goal.get("completed"):
            return False
        goal_kind = goal.get("kind")
        matched = False
        if rule and rule.get("goal") == goal_kind:
            matched = True
        else:
            for spec in DAILY_GOALS:
                if spec["kind"] == goal_kind and action in spec["actions"]:
                    matched = True
                    break
        if not matched:
            return False
        goal = dict(goal)
        goal["progress"] = min(
            int(goal.get("target", 1) or 1), int(goal.get("progress", 0) or 0) + 1
        )
        if goal["progress"] >= int(goal.get("target", 1) or 1):
            goal["completed"] = True
        state["daily_goal"] = goal
        return bool(goal.get("completed"))

    def append_log(self, state: dict, entry: dict) -> None:
        log = list(state.get("state_log") or [])
        log.append(entry)
        max_len = max(1, int(self.config.gameplay_state_log_max))
        state["state_log"] = log[-max_len:]

    def resolve_log_text(self, actor_name: str, rule: dict[str, Any], need_kind: str) -> str:
        did = str(rule.get("did") or "照料了它")
        title = NEED_SPECS.get(need_kind, {}).get("title", need_kind)
        return f"{actor_name} {did}，处理了「{title}」。"
