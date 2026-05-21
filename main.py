"""LLM Tamagotchi — Feishu bot with persistent per-chat memory.

每个飞书 chat_id 对应一只独立宠物，对话历史用 SQLite 持久化。
老消息会被异步压缩成一段"经历摘要"塞进 system prompt，避免上下文无限增长。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import random
import re
import sqlite3
import struct
import time
import tomllib
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tamagotchi")

# === config (read from env) ===
FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
FEISHU_VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
FEISHU_ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")
GM_TOKEN = os.environ.get("GM_TOKEN") or FEISHU_VERIFICATION_TOKEN

OPENAI_BASE_URL = os.environ["OPENAI_BASE_URL"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL_NAME = os.environ.get("MODEL_NAME", "gpt-4o-mini")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-3-small")

DB_PATH = Path(os.environ.get("STATE_DB", "state.db"))

FEISHU_BASE = "https://open.feishu.cn/open-apis"

# === prompts (loaded from TOML at startup) ===
_PROMPTS_PATH = Path(__file__).parent / "prompts.toml"
_PET_STYLE_PATH = Path(__file__).parent / "pet_style.toml"
_PET_CONFIG_PATH = Path(__file__).parent / "pet_config.toml"
with open(_PROMPTS_PATH, "rb") as _f:
    _PROMPTS = tomllib.load(_f)
with open(_PET_STYLE_PATH, "rb") as _f:
    _PET_STYLE = tomllib.load(_f)
with open(_PET_CONFIG_PATH, "rb") as _f:
    _PET_CONFIG = tomllib.load(_f)

PET_STYLE_PROMPT = _PET_STYLE["style"]["prompt"]
PET_STYLE_REINFORCEMENT = _PET_STYLE["style"]["reinforcement"]
SYSTEM_PROMPT = _PROMPTS["system"]["template"].format(style_prompt=PET_STYLE_PROMPT)
PERSONA_REINFORCEMENT = _PROMPTS["persona_reinforcement"]["template"].format(
    style_reinforcement=PET_STYLE_REINFORCEMENT
)
USER_WRAP_TEMPLATE = _PROMPTS["user_wrap"]["template"]
USER_WRAP_DIRECT_TEMPLATE = _PROMPTS["user_wrap"]["direct_template"]
USER_WRAP_OBSERVER_TEMPLATE = _PROMPTS["user_wrap"]["observer_template"]
COMPRESS_PROMPT = _PROMPTS["compress"]["prompt"]
COMPRESS_USER_LINE_TEMPLATE = _PROMPTS["compress"]["user_line_template"]
COMPRESS_ASSISTANT_LINE_TEMPLATE = _PROMPTS["compress"]["assistant_line_template"]
COMPRESS_USER_MESSAGE_TEMPLATE = _PROMPTS["compress"]["user_message_template"]
RECALL_HEADER = _PROMPTS["recall"]["header"]
RECALL_CARD_TEMPLATE = _PROMPTS["recall"]["card_template"]
_STATE_RENDER = _PROMPTS["state_render"]
STATE_RENDER_HEADER = _STATE_RENDER["header"]
STATE_RENDER_LINE_PREFIX = _STATE_RENDER["line_prefix"]
STATE_RENDER_VIBE_TEMPLATE = _STATE_RENDER["vibe_template"]
STATE_RENDER_LINES = _STATE_RENDER["lines"]
JSON_OUTPUT_PROMPT = _PROMPTS["json_output"]["prompt"]
PROACTIVE_PROMPT = _PROMPTS["proactive"]["prompt"]
PROACTIVE_TRIGGER_TEMPLATES = _PROMPTS["proactive_triggers"]
PROACTIVE_USER_STUB_TEMPLATE = _PROMPTS["autonomous_user_stub"]["proactive"]
SCHEDULED_EVENT_PROMPT = _PROMPTS["scheduled_event"]["prompt"]
SCHEDULED_USER_STUB_TEMPLATE = _PROMPTS["autonomous_user_stub"]["scheduled"]
FALLBACK_REPLIES = _PROMPTS["fallback_reply"]
GM_DEFAULT_SPEAK_TRIGGER = _PROMPTS["gm"]["default_speak_trigger"]

_MEMORY_CONFIG = _PET_CONFIG["memory"]
BUFFER_KEEP = int(_MEMORY_CONFIG["buffer_keep"])
COMPRESS_THRESHOLD = int(_MEMORY_CONFIG["compress_threshold"])

_REPLY_CONFIG = _PET_CONFIG["reply"]
REPLY_MIN_INTERVAL_SEC = int(_REPLY_CONFIG["min_interval_sec"])

_OBSERVER_CONFIG = _PET_CONFIG["observer"]
OBSERVER_FLUSH_MAX_COUNT = int(_OBSERVER_CONFIG["flush_max_count"])

_LLM_CONFIG = _PET_CONFIG["llm"]
REPLY_MAX_TOKENS = int(_LLM_CONFIG["reply_max_tokens"])
SCHEDULED_MAX_TOKENS = int(_LLM_CONFIG["scheduled_max_tokens"])
COMPRESS_MAX_TOKENS = int(_LLM_CONFIG["compress_max_tokens"])
CARD_REPLY_MAX_TOKENS = int(_LLM_CONFIG.get("card_reply_max_tokens", 150))

_STATE_CONFIG = _PET_CONFIG["state"]
INITIAL_STATE = {k: float(v) for k, v in _STATE_CONFIG["initial"].items()}
DECAY_RATES_PER_HOUR = {
    k: float(v) for k, v in _STATE_CONFIG["decay_per_hour"].items()
}
_DEFAULT_DECAY_QUIET = {
    "hunger": 2.0,
    "mood": -0.5,
    "energy": 6.0,
    "curiosity": 0.0,
    "affection": 0.0,
}
DECAY_RATES_PER_HOUR_QUIET = {}
for k in INITIAL_STATE.keys():
    _val = _STATE_CONFIG.get("decay_per_hour_quiet", {}).get(k)
    if _val is not None:
        DECAY_RATES_PER_HOUR_QUIET[k] = float(_val)
    else:
        DECAY_RATES_PER_HOUR_QUIET[k] = float(
            _DEFAULT_DECAY_QUIET.get(k, DECAY_RATES_PER_HOUR.get(k, 0.0))
        )
STATE_DELTA_CLAMP = int(_STATE_CONFIG["delta_clamp"])
# state.bands.<dim> = {extreme_high?, high?, low?, extreme_low?}；缺的档不渲染
STATE_BANDS = {k: dict(v) for k, v in _STATE_CONFIG.get("bands", {}).items()}
# 数值维度的顺序，影响 _apply_delta / _decay_state / 渲染先后
STATE_NUMERIC_KEYS = tuple(INITIAL_STATE.keys())
RECENT_VIBE_POOL = list(_PET_STYLE.get("recent_vibes", {}).get("pool", []))

_AUTONOMOUS_CONFIG = _PET_CONFIG["autonomous"]
TICK_INTERVAL_SEC = int(_AUTONOMOUS_CONFIG["tick_interval_sec"])
PROACTIVE_COOLDOWN_SEC = int(_AUTONOMOUS_CONFIG["cooldown_sec"])
PROACTIVE_TZ_OFFSET_HOURS = float(_AUTONOMOUS_CONFIG["timezone_offset_hours"])
QUIET_HOURS = (
    int(_AUTONOMOUS_CONFIG["quiet_start_hour"]),
    int(_AUTONOMOUS_CONFIG["quiet_end_hour"]),
)
# 日记锁定在休息开始那一刻，梦境锁定在休息结束那一刻；
# 二者跟随 quiet_start/quiet_end，prompts.toml 里的 hour 仅作其它 kind 的兜底。
_SCHEDULED_HOUR_LOCK = {"diary": QUIET_HOURS[0], "dream": QUIET_HOURS[1]}
SCHEDULED_EVENTS = tuple(
    {**event, "hour": _SCHEDULED_HOUR_LOCK.get(event["kind"], event["hour"])}
    for event in _PROMPTS["scheduled_events"]
)
_TRIGGER_THRESHOLDS = _AUTONOMOUS_CONFIG["trigger_thresholds"]
HUNGER_TRIGGER = float(_TRIGGER_THRESHOLDS["hunger"])
MOOD_TRIGGER = float(_TRIGGER_THRESHOLDS["mood"])
ENERGY_TRIGGER = float(_TRIGGER_THRESHOLDS["energy"])
SPONTANEOUS_PROB = float(_AUTONOMOUS_CONFIG["spontaneous_prob"])

# 主动发言交互卡片配置（运行数值在 pet_config [card]，展示文案在 prompts [card]）。
_CARD_CONFIG = _PET_CONFIG.get("card", {})
CARD_ENABLED = bool(_CARD_CONFIG.get("enabled", False))
CARD_BAR_WIDTH = int(_CARD_CONFIG.get("bar_width", 10))
CARD_ACTION_COOLDOWN_SEC = int(_CARD_CONFIG.get("action_cooldown_sec", 60))
CARD_MAX_BUTTONS = int(_CARD_CONFIG.get("max_buttons", 3))
CARD_DEFAULT_ACTIONS = list(_CARD_CONFIG.get("default_actions", []))
# action_key -> {need_dim, need_side, delta}
CARD_ACTIONS = {k: dict(v) for k, v in _CARD_CONFIG.get("actions", {}).items()}

_CARD_PROMPTS = _PROMPTS.get("card", {})
CARD_BAR_FILLED = _CARD_PROMPTS.get("bar_filled", "▰")
CARD_BAR_EMPTY = _CARD_PROMPTS.get("bar_empty", "▱")
CARD_BARS_HEADER = _CARD_PROMPTS.get("bars_header", "")
CARD_TOAST_DONE = _CARD_PROMPTS.get("toast_done", "✓")
CARD_TOAST_COOLDOWN = _CARD_PROMPTS.get("toast_cooldown", "稍等一下~")
_CARD_BARS = dict(_CARD_PROMPTS.get("bars", {}))
CARD_VIBE_TEMPLATE = _CARD_BARS.pop("vibe_template", "✨ {vibe}")
CARD_BAR_LABELS = _CARD_BARS  # dim -> label（含 emoji）
# action_key -> {button, pending, did}
CARD_ACTION_TEXT = {k: dict(v) for k, v in _CARD_PROMPTS.get("actions", {}).items()}
CARD_ACTION_REPLY_PROMPT = _CARD_PROMPTS.get("action_reply", {}).get("prompt", "")


def _wrap_user(text: str, sender_name: str = "", is_observer: bool = False) -> str:
    """把 user 输入包成"引文"形式，让模型当成数据而非指令。
    - is_observer=True：群里别人之间的对话，宠物只是听到
    - sender_name 非空 & is_observer=False：群友直接 @ 宠物或私聊
    - 两者都空：旧数据兜底
    """
    if is_observer:
        return USER_WRAP_OBSERVER_TEMPLATE.format(
            sender_name=sender_name or "群友",
            user_text=text,
        )
    if sender_name:
        return USER_WRAP_DIRECT_TEMPLATE.format(
            sender_name=sender_name,
            user_text=text,
        )
    return USER_WRAP_TEMPLATE.format(user_text=text)


def _initial_state() -> dict:
    return {
        **INITIAL_STATE,
        "last_update_ts": time.time(),
        "last_proactive_ts": 0.0,
        "last_reply_ts": 0.0,
        "last_dream_date": "",
        "last_diary_date": "",
        "recent_vibe": "",
        "recent_vibe_date": "",
    }


def _local_date_hour(now_ts: float) -> tuple[str, int]:
    """返回本地日期 key 和小时，用于每日定时事件去重。"""
    local = time.gmtime(now_ts + PROACTIVE_TZ_OFFSET_HOURS * 3600)
    return time.strftime("%Y-%m-%d", local), local.tm_hour


def _local_hour(now_ts: float) -> int:
    """无 zoneinfo 依赖的本地小时（按 PROACTIVE_TZ_OFFSET_HOURS 偏移）。"""
    return _local_date_hour(now_ts)[1]


def _in_quiet_hours(now_ts: float) -> bool:
    """now 是否落在休息时段（支持跨午夜：start > end 时按夜间区间处理）。"""
    h = _local_hour(now_ts)
    qs, qe = QUIET_HOURS
    return qs <= h < qe if qs < qe else (h >= qs or h < qe)


def _partition_hours(t_start: float, t_end: float) -> tuple[float, float]:
    """把 [t_start, t_end] 之间的小时数划分为 (quiet_hours, active_hours)。
    采用加速整天折算 + 逐小时步进计算余数。
    """
    total_hours = (t_end - t_start) / 3600.0
    if total_hours <= 0:
        return 0.0, 0.0

    qs, qe = QUIET_HOURS
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
        lh = _local_hour(current)
        is_quiet = qs <= lh < qe if qs < qe else (lh >= qs or lh < qe)
        actual_step = min(step, (t_end - current) / 3600.0)
        if is_quiet:
            q_hours += actual_step
        else:
            a_hours += actual_step
        current += actual_step * 3600

    return q_hours, a_hours


def _maybe_rotate_vibe(state: dict, now: float, pet_id: int | None = None) -> dict:
    """每天滚一次 recent_vibe；同一本地日期内幂等。"""
    if not RECENT_VIBE_POOL:
        return state
    date_key, _ = _local_date_hour(now)
    if state.get("recent_vibe_date") == date_key and state.get("recent_vibe"):
        return state
    out = dict(state)
    if pet_id is not None:
        r = random.Random(f"{pet_id}-{date_key}")
        out["recent_vibe"] = r.choice(RECENT_VIBE_POOL)
    else:
        out["recent_vibe"] = random.choice(RECENT_VIBE_POOL)
    out["recent_vibe_date"] = date_key
    return out


def _decay_state(stored: dict, now: float, pet_id: int | None = None) -> dict:
    """根据 last_update_ts 到 now 的时间差，把存储的状态衰减到当前值。
    保留 stored 里所有未知字段（如 last_proactive_ts / recent_vibe），只覆写衰减项 + last_update_ts。
    根据静默时段分流计算 active_hours 和 quiet_hours 不同的衰减率。
    顺便每天滚一次 recent_vibe。"""
    last_update_ts = float(stored.get("last_update_ts", now))
    elapsed_hours = max(0.0, (now - last_update_ts) / 3600.0)
    
    result = dict(stored)
    result["last_update_ts"] = now
    
    if elapsed_hours <= 0:
        return _maybe_rotate_vibe(result, now, pet_id)
        
    q_hours, a_hours = _partition_hours(last_update_ts, now)
    
    for k in STATE_NUMERIC_KEYS:
        v = float(stored.get(k, INITIAL_STATE.get(k, 50.0)))
        rate_active = DECAY_RATES_PER_HOUR.get(k, 0.0)
        rate_quiet = DECAY_RATES_PER_HOUR_QUIET.get(k, rate_active)
        
        v += rate_active * a_hours + rate_quiet * q_hours
        result[k] = max(0.0, min(100.0, v))
        
    return _maybe_rotate_vibe(result, now, pet_id)


def _apply_delta(state: dict, delta: dict) -> dict:
    """把 LLM 返回的 state_delta 套到当前状态上，clamp 到 0-100。
    覆盖所有 INITIAL_STATE 里声明的数值维度，未声明的字段被忽略。"""
    out = dict(state)
    for k in STATE_NUMERIC_KEYS:
        try:
            d = int(delta.get(k, 0))
        except (TypeError, ValueError):
            d = 0
        d = max(-STATE_DELTA_CLAMP, min(STATE_DELTA_CLAMP, d))
        out[k] = max(0.0, min(100.0, float(out.get(k, INITIAL_STATE.get(k, 50.0))) + d))
    return out


def _state_band(dim: str, value: float) -> str | None:
    """根据 STATE_BANDS 把 dim 当前值映射到档位 key，命中 lines 表的某条；中段返回 None。
    档位 key 顺序：extreme_high > high > low > extreme_low，每个维度可只配其中一部分。"""
    bands = STATE_BANDS.get(dim)
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


def _render_state(state: dict) -> str:
    """生成"感受块"塞进 system message：中段不渲染；全维度都中段且无 vibe 时返回空串。"""
    lines: list[str] = []
    for dim in STATE_NUMERIC_KEYS:
        band_key = _state_band(dim, float(state.get(dim, 50.0)))
        if not band_key:
            continue
        sentence = STATE_RENDER_LINES.get(band_key)
        if sentence:
            lines.append(sentence)
    vibe = (state.get("recent_vibe") or "").strip()
    if vibe:
        lines.append(STATE_RENDER_VIBE_TEMPLATE.format(vibe=vibe))
    if not lines:
        return ""
    return STATE_RENDER_HEADER + "\n".join(STATE_RENDER_LINE_PREFIX + l for l in lines)


# === 主动发言交互卡片 ===


def _state_bar(value: float) -> str:
    """把 0-100 的值画成方块进度条。"""
    v = max(0.0, min(100.0, value))
    filled = int(round(v / 100.0 * CARD_BAR_WIDTH))
    filled = max(0, min(CARD_BAR_WIDTH, filled))
    return CARD_BAR_FILLED * filled + CARD_BAR_EMPTY * (CARD_BAR_WIDTH - filled)


def _render_state_bars(state: dict) -> str:
    """渲染卡片底部的状态进度条段（markdown）。hunger 反转成「饱腹度」让满=好。"""
    lines: list[str] = []
    if CARD_BARS_HEADER:
        lines.append(CARD_BARS_HEADER)
    for dim in STATE_NUMERIC_KEYS:
        label = CARD_BAR_LABELS.get(dim)
        if not label:
            continue
        raw = float(state.get(dim, 50.0))
        shown = 100.0 - raw if dim == "hunger" else raw
        lines.append(f"{label}  {_state_bar(shown)}  `{int(round(shown))}`")
    vibe = (state.get("recent_vibe") or "").strip()
    if vibe:
        lines.append(CARD_VIBE_TEMPLATE.format(vibe=vibe))
    return "\n".join(lines)


def _pick_card_actions(state: dict) -> list[str]:
    """按当前 state 挑该浮现的互动按钮：哪个维度有需求就出哪个按钮。
    全维度都在中段（没需求）时，从 default_actions 里随机兜底，保证总有东西可点。"""
    needed: list[tuple[int, str]] = []  # (severity, key)
    for key, cfg in CARD_ACTIONS.items():
        dim = cfg.get("need_dim")
        side = cfg.get("need_side")
        if not dim or not side:
            continue
        band = _state_band(dim, float(state.get(dim, 50.0)))
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
        return [k for _, k in needed][:CARD_MAX_BUTTONS]
    pool = [k for k in CARD_DEFAULT_ACTIONS if k in CARD_ACTIONS]
    n = min(CARD_MAX_BUTTONS, len(pool))
    return random.sample(pool, n) if n else []


def _build_pet_card(
    pet_id: int,
    text: str,
    state: dict,
    *,
    with_actions: bool = True,
    action_keys: list[str] | None = None,
) -> dict:
    """组装飞书 interactive 卡片：宠物的话 + 状态进度条 + 互动按钮。
    with_actions=False 时不带按钮——按钮被点过一次后，卡片就变成结果快照，
    避免按钮集随状态轮换导致可以无限点。
    action_keys 不为 None 时用这组固定按钮（日记 / 梦境的「晚安」「早上好」），
    为 None 时按 state 动态挑（主动发言卡片）。"""
    elements: list[dict] = [{"tag": "markdown", "content": text or "…"}]
    bars = _render_state_bars(state)
    if bars:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": bars})
    if with_actions:
        keys = _pick_card_actions(state) if action_keys is None else action_keys
        buttons = []
        for key in keys:
            btn_text = CARD_ACTION_TEXT.get(key, {}).get("button", key)
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn_text},
                "type": "primary",
                "value": {"pet_id": pet_id, "action": key},
            })
        if buttons:
            elements.append({"tag": "hr"})
            elements.append({"tag": "action", "actions": buttons})
    return {"config": {"wide_screen_mode": True}, "elements": elements}


def _apply_card_delta(state: dict, delta: dict) -> dict:
    """套用按钮的确定性 delta：只 clamp 到 0-100，不走 LLM 的 ±delta_clamp 限制。"""
    out = dict(state)
    for k in STATE_NUMERIC_KEYS:
        try:
            d = float(delta.get(k, 0))
        except (TypeError, ValueError):
            d = 0.0
        out[k] = max(0.0, min(100.0, float(out.get(k, INITIAL_STATE.get(k, 50.0))) + d))
    return out


# === clients ===
http = httpx.AsyncClient(timeout=30.0)
llm = AsyncOpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)

# tenant access token lock (keeps token acquisition serialized)
_token_lock = asyncio.Lock()

# per-pet compress lock (prevents concurrent compression for the same pet)
_compress_locks: dict[int, asyncio.Lock] = {}

# 旁听消息不逐条落库：先攒在进程内，autonomous tick 或下一条 direct 消息到来时批量写入。
# {pet_id: [{"content", "sender_name", "ts"}, ...]}
_observer_buffer: dict[int, list[dict]] = {}

# 群 @ 回复节流的进程内闸门：{pet_id: 上次放行回复的起始时间戳}。
# DB 的 last_reply_ts 要等 LLM 回完才写，一波 @ 在回复生成期间都读到同一个旧值会全部放行；
# 这个 dict 在通过节流的同步代码里立即占位（check+set 间无 await，asyncio 下原子），堵住并发漏放。
_reply_gate: dict[int, float] = {}


# === DB cache & dedup helpers ===

def _get_sys_cache(key: str) -> str | None:
    now = time.time()
    with _db() as conn:
        row = conn.execute(
            "SELECT val FROM sys_cache WHERE key = ? AND (expires_at IS NULL OR expires_at > ?)",
            (key, now)
        ).fetchone()
    return row["val"] if row else None


async def get_sys_cache(key: str) -> str | None:
    return await asyncio.to_thread(_get_sys_cache, key)


def _set_sys_cache(key: str, val: str, expires_in_sec: float | None = None) -> None:
    expires_at = time.time() + expires_in_sec if expires_in_sec is not None else None
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sys_cache (key, val, expires_at) VALUES (?, ?, ?)",
            (key, val, expires_at)
        )


async def set_sys_cache(key: str, val: str, expires_in_sec: float | None = None) -> None:
    return await asyncio.to_thread(_set_sys_cache, key, val, expires_in_sec)


def _get_cached_user_name(open_id: str) -> str | None:
    threshold = time.time() - 86400
    with _db() as conn:
        row = conn.execute(
            "SELECT name FROM user_names WHERE open_id = ? AND updated_at > ?",
            (open_id, threshold)
        ).fetchone()
    return row["name"] if row else None


async def get_cached_user_name(open_id: str) -> str | None:
    return await asyncio.to_thread(_get_cached_user_name, open_id)


def _set_cached_user_name(open_id: str, name: str) -> None:
    now = time.time()
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_names (open_id, name, updated_at) VALUES (?, ?, ?)",
            (open_id, name, now)
        )


async def set_cached_user_name(open_id: str, name: str) -> None:
    return await asyncio.to_thread(_set_cached_user_name, open_id, name)


def _check_and_register_event(event_id: str) -> bool:
    now = time.time()
    with _db() as conn:
        try:
            conn.execute(
                "INSERT INTO event_dedup (event_id, created_at) VALUES (?, ?)",
                (event_id, now)
            )
            return False
        except sqlite3.IntegrityError:
            return True


async def check_and_register_event(event_id: str) -> bool:
    return await asyncio.to_thread(_check_and_register_event, event_id)


def _clean_old_events(max_age_sec: float = 86400) -> None:
    threshold = time.time() - max_age_sec
    with _db() as conn:
        conn.execute("DELETE FROM event_dedup WHERE created_at < ?", (threshold,))


async def clean_old_events(max_age_sec: float = 86400) -> None:
    await asyncio.to_thread(_clean_old_events, max_age_sec)


# === DB ===

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _init_db() -> None:
    with _db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE NOT NULL,
                born_at REAL NOT NULL,
                summary_until_id INTEGER NOT NULL DEFAULT 0,
                state_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pet_id INTEGER NOT NULL REFERENCES pets(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                ts REAL NOT NULL,
                sender_name TEXT NOT NULL DEFAULT '',
                is_observer INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_messages_pet ON messages(pet_id, id);

            CREATE TABLE IF NOT EXISTS memory_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pet_id INTEGER NOT NULL REFERENCES pets(id),
                when_text TEXT NOT NULL DEFAULT '',
                who TEXT NOT NULL DEFAULT '',
                what TEXT NOT NULL,
                vibe TEXT NOT NULL DEFAULT '',
                hooks TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                source_until_id INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_cards_pet ON memory_cards(pet_id, id);

            CREATE TABLE IF NOT EXISTS embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pet_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                vec BLOB NOT NULL,
                ts REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_embed_pet ON embeddings(pet_id, kind);
            CREATE INDEX IF NOT EXISTS idx_embed_source ON embeddings(kind, source_id);

            CREATE TABLE IF NOT EXISTS event_dedup (
                event_id TEXT PRIMARY KEY,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sys_cache (
                key TEXT UNIQUE NOT NULL,
                val TEXT,
                expires_at REAL
            );

            CREATE TABLE IF NOT EXISTS user_names (
                open_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        try:
            conn.execute("ALTER TABLE pets ADD COLUMN compress_fail_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE pets ADD COLUMN last_compress_attempt REAL DEFAULT 0.0")
        except sqlite3.OperationalError:
            pass


def _get_or_create_pet(chat_id: str) -> int:
    now = time.time()
    initial_state_json = json.dumps(_initial_state())
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO pets (chat_id, born_at, state_json) VALUES (?, ?, ?)",
            (chat_id, now, initial_state_json),
        )
        row = conn.execute(
            "SELECT id FROM pets WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row["id"]


async def get_or_create_pet(chat_id: str) -> int:
    return await asyncio.to_thread(_get_or_create_pet, chat_id)


def _find_pet(chat_id: str) -> int | None:
    """返回已存在的 pet_id，不存在不创建——observer 消息进来时用。"""
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM pets WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row["id"] if row else None


async def find_pet(chat_id: str) -> int | None:
    return await asyncio.to_thread(_find_pet, chat_id)


def _append_message(
    pet_id: int,
    role: str,
    content: str,
    sender_name: str = "",
    is_observer: bool = False,
) -> int:
    """插入一条消息，返回新行的 id（调用方需要时用来在 history 里定位本条）。"""
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO messages (pet_id, role, content, ts, sender_name, is_observer) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pet_id, role, content, time.time(), sender_name, 1 if is_observer else 0),
        )
        return int(cur.lastrowid)


async def append_message(
    pet_id: int,
    role: str,
    content: str,
    sender_name: str = "",
    is_observer: bool = False,
) -> int:
    return await asyncio.to_thread(_append_message, pet_id, role, content, sender_name, is_observer)


def _append_observer_batch(pet_id: int, items: list[dict]) -> None:
    """把一批缓冲的旁听消息按各自原始 ts 批量写入 messages 表。"""
    with _db() as conn:
        conn.executemany(
            "INSERT INTO messages (pet_id, role, content, ts, sender_name, is_observer) "
            "VALUES (?, 'user', ?, ?, ?, 1)",
            [(pet_id, it["content"], it["ts"], it["sender_name"]) for it in items],
        )


async def flush_observer_buffer(pet_id: int) -> int:
    """把某宠物缓冲的旁听消息批量落库，返回写入条数。
    落库失败则把消息退回缓冲（保持时序），不抛异常。落库成功后按需触发压缩。"""
    items = _observer_buffer.pop(pet_id, None)
    if not items:
        return 0
    try:
        await asyncio.to_thread(_append_observer_batch, pet_id, items)
    except Exception:
        log.exception("flush observer batch failed for pet %d; re-buffering %d msgs", pet_id, len(items))
        _observer_buffer.setdefault(pet_id, [])[:0] = items
        return 0
    log.info("pet %d flushed %d buffered observer msgs", pet_id, len(items))
    if (await count_unsummarized(pet_id)) > COMPRESS_THRESHOLD:
        asyncio.create_task(compress_pet_memory(pet_id))
    return len(items)


async def flush_all_observer_buffers() -> None:
    """flush 所有宠物的旁听缓冲——autonomous tick 和进程关闭时调用。"""
    for pet_id in list(_observer_buffer.keys()):
        try:
            await flush_observer_buffer(pet_id)
        except Exception:
            log.exception("flush observer buffer failed for pet %d", pet_id)


def _decode_state(state_json: str | None) -> dict:
    """把 pets.state_json 解析成 dict；坏 JSON / 空值都兜底回 _initial_state()。
    返回未衰减的存储态，调用方一般再 _decay_state 到当前。"""
    try:
        stored = json.loads(state_json or "{}")
    except json.JSONDecodeError:
        stored = {}
    return stored or _initial_state()


def _load_pet_context(pet_id: int) -> tuple[list[dict], dict]:
    """返回 (unsummarized_messages_in_order, current_state_decayed_to_now)。
    history 每条带 role/content/sender_name/is_observer，给后续 _wrap_user 用。
    长期记忆走 memory_cards + RAG，不在这里返回。"""
    with _db() as conn:
        pet_row = conn.execute(
            "SELECT summary_until_id, state_json FROM pets WHERE id = ?", (pet_id,)
        ).fetchone()
        msg_rows = conn.execute(
            "SELECT id, role, content, sender_name, is_observer FROM messages "
            "WHERE pet_id = ? AND id > ? ORDER BY id",
            (pet_id, pet_row["summary_until_id"]),
        ).fetchall()
    history = [
        {
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "sender_name": r["sender_name"] or "",
            "is_observer": bool(r["is_observer"]),
        }
        for r in msg_rows
    ]
    current = _decay_state(_decode_state(pet_row["state_json"]), time.time(), pet_id)
    return history, current


async def load_pet_context(pet_id: int) -> tuple[list[dict], dict]:
    return await asyncio.to_thread(_load_pet_context, pet_id)


def _update_pet_state(pet_id: int, state: dict) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE pets SET state_json = ? WHERE id = ?",
            (json.dumps(state), pet_id),
        )


async def update_pet_state(pet_id: int, state: dict) -> None:
    return await asyncio.to_thread(_update_pet_state, pet_id, state)


def _load_pet_state(pet_id: int) -> dict:
    with _db() as conn:
        row = conn.execute(
            "SELECT state_json FROM pets WHERE id = ?", (pet_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"pet not found: {pet_id}")
    return _decay_state(_decode_state(row["state_json"]), time.time(), pet_id)


async def load_pet_state(pet_id: int) -> dict:
    return await asyncio.to_thread(_load_pet_state, pet_id)


def _count_unsummarized(pet_id: int) -> int:
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM messages "
            "WHERE pet_id = ? "
            "AND id > (SELECT summary_until_id FROM pets WHERE id = ?)",
            (pet_id, pet_id),
        ).fetchone()
    return row["c"]


async def count_unsummarized(pet_id: int) -> int:
    return await asyncio.to_thread(_count_unsummarized, pet_id)


# === embeddings ===

def _vec_pack(vec: list[float]) -> bytes:
    """float32 LE 紧凑存 BLOB；1536 维 → 6KB。"""
    return struct.pack(f"<{len(vec)}f", *vec)


def _vec_unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob)//4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


async def _embed_text(text: str) -> list[float] | None:
    """单条 embed；失败返 None 给调用方降级（不抛）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        resp = await llm.embeddings.create(model=EMBED_MODEL, input=text)
        return list(resp.data[0].embedding)
    except Exception:
        log.exception("embed failed for text len=%d", len(text))
        return None


def _store_embedding(pet_id: int, kind: str, source_id: int, content: str, vec: list[float]) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO embeddings (pet_id, kind, source_id, content, vec, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (pet_id, kind, source_id, content, _vec_pack(vec), time.time()),
        )


async def _embed_and_store(pet_id: int, kind: str, source_id: int, content: str) -> None:
    """异步：embed 然后写 DB；调用方一般 asyncio.create_task。"""
    vec = await _embed_text(content)
    if vec is None:
        return
    await asyncio.to_thread(_store_embedding, pet_id, kind, source_id, content, vec)


def _score_cards(pet_id: int, q_vec: list[float], k: int) -> list[dict]:
    """同步重活：查卡片库 + 逐条 unpack 向量 + cosine 打分 + 取 top-K。
    DB 读和纯 Python 余弦都是阻塞操作，由调用方丢进线程池，避免卡住事件循环。"""
    with _db() as conn:
        rows = conn.execute(
            "SELECT e.source_id, e.vec, c.when_text, c.who, c.what, c.vibe "
            "FROM embeddings e JOIN memory_cards c ON c.id = e.source_id "
            "WHERE e.pet_id = ? AND e.kind = 'card' "
            "ORDER BY e.id DESC LIMIT 500",
            (pet_id,),
        ).fetchall()
    if not rows:
        return []
    scored = []
    for r in rows:
        sim = _cosine(q_vec, _vec_unpack(r["vec"]))
        scored.append((sim, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for sim, r in scored[:k]:
        if sim <= 0.0:
            continue
        out.append({
            "id": r["source_id"],
            "score": sim,
            "when": r["when_text"],
            "who": r["who"],
            "what": r["what"],
            "vibe": r["vibe"],
        })
    return out


async def recall_relevant_cards(pet_id: int, query: str, k: int = 6) -> list[dict]:
    """用 query 在该宠物的卡片库里检索 top-K，按相似度降序。失败返回空。"""
    if not query.strip():
        return []
    q_vec = await _embed_text(query)
    if q_vec is None:
        return []
    return await asyncio.to_thread(_score_cards, pet_id, q_vec, k)


def _recent_cards(pet_id: int, n: int) -> list[dict]:
    if n <= 0:
        return []
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, when_text, who, what, vibe FROM memory_cards "
            "WHERE pet_id = ? ORDER BY id DESC LIMIT ?",
            (pet_id, n),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "when": r["when_text"],
            "who": r["who"],
            "what": r["what"],
            "vibe": r["vibe"],
        }
        for r in rows
    ]


def _render_recall_block(cards: list[dict]) -> str:
    if not cards:
        return ""
    # 按 id 升序展示，让 LLM 看到时间线感（最近的在最后）
    sorted_cards = sorted(cards, key=lambda c: c.get("id", 0))
    lines = [
        RECALL_CARD_TEMPLATE.format(
            when=(c.get("when") or "某时"),
            who=(c.get("who") or "群里"),
            what=(c.get("what") or "").strip(),
            vibe=(c.get("vibe") or "").strip(),
        )
        for c in sorted_cards
    ]
    return RECALL_HEADER + "\n".join(lines)


async def build_recall_block(
    pet_id: int, query: str = "", k_relevant: int = 6, k_recent: int = 3
) -> str:
    """合并 top-K 相关 + top-N 最近，渲染成 system 注入段。失败安全：空 query 时只走 recency。"""
    cards_by_id: dict[int, dict] = {}
    if query.strip():
        relevant = await recall_relevant_cards(pet_id, query, k=k_relevant)
        for c in relevant:
            cards_by_id[c["id"]] = c
    for c in _recent_cards(pet_id, k_recent):
        cards_by_id.setdefault(c["id"], c)
    return _render_recall_block(list(cards_by_id.values()))


# === compression ===

def _compress_lock(pet_id: int) -> asyncio.Lock:
    lock = _compress_locks.get(pet_id)
    if lock is None:
        lock = asyncio.Lock()
        _compress_locks[pet_id] = lock
    return lock


def _format_card_for_embed(card: dict) -> str:
    """卡片喂给 embed 的纯文本——把字段拼成一行。"""
    parts = []
    for k in ("when", "who", "what", "vibe", "hooks"):
        v = (card.get(k) or "").strip()
        if v:
            parts.append(v)
    return " | ".join(parts)


def _get_compress_context(pet_id: int) -> tuple[int, int, float, list[dict]]:
    with _db() as conn:
        pet_row = conn.execute(
            "SELECT summary_until_id, compress_fail_count, last_compress_attempt FROM pets WHERE id = ?", (pet_id,)
        ).fetchone()
        if not pet_row:
            return 0, 0, 0.0, []
        summary_until_id = pet_row["summary_until_id"]
        compress_fail_count = pet_row["compress_fail_count"] if "compress_fail_count" in pet_row.keys() else 0
        last_compress_attempt = pet_row["last_compress_attempt"] if "last_compress_attempt" in pet_row.keys() else 0.0
        
        rows = conn.execute(
            "SELECT id, role, content, sender_name, is_observer FROM messages "
            "WHERE pet_id = ? AND id > ? ORDER BY id",
            (pet_id, summary_until_id),
        ).fetchall()
    return (summary_until_id, compress_fail_count, last_compress_attempt, [dict(r) for r in rows])


def _save_compressed_cards(pet_id: int, cards_raw: list[dict], new_until_id: int) -> list[tuple[int, dict]]:
    inserted = []
    now = time.time()
    with _db() as conn:
        for card in cards_raw:
            if not isinstance(card, dict):
                continue
            what = (card.get("what") or "").strip()
            if not what:
                continue
            row = conn.execute(
                "INSERT INTO memory_cards (pet_id, when_text, who, what, vibe, hooks, created_at, source_until_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (
                    pet_id,
                    (card.get("when") or "").strip(),
                    (card.get("who") or "").strip(),
                    what,
                    (card.get("vibe") or "").strip(),
                    (card.get("hooks") or "").strip(),
                    now,
                    new_until_id,
                ),
            ).fetchone()
            inserted.append((row["id"], card))
        conn.execute(
            "UPDATE pets SET summary_until_id = ?, compress_fail_count = 0, last_compress_attempt = ? WHERE id = ?",
            (new_until_id, now, pet_id),
        )
    return inserted


def _handle_compress_failure(pet_id: int, current_fail_count: int, new_until_id: int) -> None:
    now = time.time()
    next_fail_count = current_fail_count + 1
    with _db() as conn:
        if next_fail_count >= 5:
            conn.execute(
                "UPDATE pets SET summary_until_id = ?, compress_fail_count = 0, last_compress_attempt = ? WHERE id = ?",
                (new_until_id, now, pet_id)
            )
            log.error(
                "compression failed 5 times for pet %d. Forcefully skipping chunk to until_id=%d to prevent locking the buffer.",
                pet_id, new_until_id
            )
        else:
            conn.execute(
                "UPDATE pets SET compress_fail_count = ?, last_compress_attempt = ? WHERE id = ?",
                (next_fail_count, now, pet_id)
            )
            log.warning(
                "memory compression failed for pet %d. fail_count is now %d.",
                pet_id, next_fail_count
            )


async def compress_pet_memory(pet_id: int) -> None:
    async with _compress_lock(pet_id):
        summary_until_id, compress_fail_count, last_compress_attempt, rows = await asyncio.to_thread(_get_compress_context, pet_id)
        if not rows:
            return

        if len(rows) <= BUFFER_KEEP + 1:
            # 已经被别的协程压过了，或者还没到压的份上
            return

        to_compress = rows[: len(rows) - BUFFER_KEEP]
        new_until_id = to_compress[-1]["id"]

        now = time.time()
        if compress_fail_count >= 3:
            if now - last_compress_attempt < 3600:
                log.info(
                    "skipping memory compression for pet %d due to cool-down backoff (fail_count=%d, last_attempt=%f)",
                    pet_id, compress_fail_count, last_compress_attempt
                )
                return

        # 拼对话块时，user 消息也包成 <<< >>>，明确告诉压缩 LLM 它们是数据不是指令
        chunk_lines: list[str] = []
        for r in to_compress:
            if r["role"] == "user":
                wrapped = _wrap_user(
                    r["content"],
                    sender_name=r["sender_name"] or "",
                    is_observer=bool(r["is_observer"]),
                )
                chunk_lines.append(
                    COMPRESS_USER_LINE_TEMPLATE.format(content=wrapped)
                )
            else:
                chunk_lines.append(
                    COMPRESS_ASSISTANT_LINE_TEMPLATE.format(content=r["content"])
                )
        chunk = "\n".join(chunk_lines)
        user_msg = COMPRESS_USER_MESSAGE_TEMPLATE.format(chunk=chunk)

        try:
            resp = await llm.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": COMPRESS_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=COMPRESS_MAX_TOKENS,
                temperature=0.4,
                response_format={"type": "json_object"},
            )
            content = (resp.choices[0].message.content or "").strip()
        except Exception:
            log.exception("compress llm call failed for pet %d", pet_id)
            await asyncio.to_thread(_handle_compress_failure, pet_id, compress_fail_count, new_until_id)
            return

        try:
            data = json.loads(content)
            cards_raw = data.get("cards") or []
            if not isinstance(cards_raw, list):
                raise ValueError("cards is not a list")
        except Exception as e:
            log.warning("compress parse/json error for pet %d: %s. raw content: %r", pet_id, str(e), content[:200])
            await asyncio.to_thread(_handle_compress_failure, pet_id, compress_fail_count, new_until_id)
            return

        try:
            inserted = await asyncio.to_thread(_save_compressed_cards, pet_id, cards_raw, new_until_id)
        except Exception:
            log.exception("failed to save compressed cards for pet %d", pet_id)
            await asyncio.to_thread(_handle_compress_failure, pet_id, compress_fail_count, new_until_id)
            return

        log.info(
            "compressed pet %d: %d msgs → %d cards, until_id=%d",
            pet_id, len(to_compress), len(inserted), new_until_id,
        )

        # 异步 embed 每张卡片，失败不影响主流程
        for card_id, card in inserted:
            text = _format_card_for_embed(card)
            asyncio.create_task(_embed_and_store(pet_id, "card", card_id, text))


# === feishu helpers ===

def _decrypt_feishu(encrypt_str: str, encrypt_key: str) -> str:
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    data = base64.b64decode(encrypt_str)
    iv, ct = data[:16], data[16:]
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


async def _get_tenant_access_token() -> str:
    async with _token_lock:
        cached = await get_sys_cache("tenant_access_token")
        if cached:
            return cached
        r = await http.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        )
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"get tenant_access_token failed: {data}")
        token = data["tenant_access_token"]
        expires_in = float(data.get("expire", 7000))
        # Keep buffer of 60 seconds
        await set_sys_cache("tenant_access_token", token, expires_in - 60)
        return token


async def _get_bot_open_id() -> str:
    """拿 bot 自己的 open_id；调一次缓存，失败抛异常由调用方降级。"""
    cached = await get_sys_cache("bot_open_id")
    if cached:
        return cached
    token = await _get_tenant_access_token()
    r = await http.get(
        f"{FEISHU_BASE}/bot/v3/info",
        headers={"Authorization": f"Bearer {token}"},
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"get bot info failed: {data}")
    open_id = ((data.get("bot") or {}).get("open_id") or "").strip()
    if not open_id:
        raise RuntimeError(f"bot info missing open_id: {data}")
    await set_sys_cache("bot_open_id", open_id)
    log.info("bot open_id resolved: %s", open_id)
    return open_id


async def _resolve_user_name(open_id: str) -> str:
    """open_id -> 真实姓名；从 DB 缓存或飞书 API 获取，失败/没权限则使用降级姓名缓存。"""
    if not open_id:
        return "群友"
    cached = await get_cached_user_name(open_id)
    if cached:
        return cached
    name = ""
    try:
        token = await _get_tenant_access_token()
        r = await http.get(
            f"{FEISHU_BASE}/contact/v3/users/{open_id}",
            params={"user_id_type": "open_id"},
            headers={"Authorization": f"Bearer {token}"},
        )
        data = r.json()
        if data.get("code") == 0:
            name = (((data.get("data") or {}).get("user") or {}).get("name") or "").strip()
        else:
            log.info("resolve_user_name fallback (code=%s msg=%s)", data.get("code"), data.get("msg"))
    except Exception:
        log.exception("resolve_user_name network error for %s", open_id)
    if not name:
        name = f"群友-{open_id[-4:]}"
    await set_cached_user_name(open_id, name)
    return name


async def _reply_text(message_id: str, text: str) -> None:
    token = await _get_tenant_access_token()
    r = await http.post(
        f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    )
    data = r.json()
    if data.get("code") != 0:
        log.error("reply failed: %s", data)


async def _send_text(chat_id: str, text: str) -> None:
    """主动给某个 chat 发新消息（不基于已有 message_id）。"""
    token = await _get_tenant_access_token()
    r = await http.post(
        f"{FEISHU_BASE}/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    )
    data = r.json()
    if data.get("code") != 0:
        log.error("send failed: %s", data)
        raise RuntimeError(f"feishu send failed: {data}")


async def _send_card(chat_id: str, card: dict) -> None:
    """主动给某个 chat 发一张 interactive 卡片。"""
    token = await _get_tenant_access_token()
    r = await http.post(
        f"{FEISHU_BASE}/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
    )
    data = r.json()
    if data.get("code") != 0:
        log.error("send card failed: %s", data)
        raise RuntimeError(f"feishu send card failed: {data}")


async def _update_card_message(message_id: str, card: dict) -> None:
    """原地更新一张已发出的 interactive 卡片（按钮点击后的异步回填用）。"""
    token = await _get_tenant_access_token()
    r = await http.patch(
        f"{FEISHU_BASE}/im/v1/messages/{message_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={"content": json.dumps(card, ensure_ascii=False)},
    )
    data = r.json()
    if data.get("code") != 0:
        log.error("update card failed: %s", data)


def _clean_text(raw: str, mentions: list[dict]) -> str:
    # 飞书 text 内容里 @ 占位符形如 "@_user_1"，对应 mentions[i].key
    for m in mentions or []:
        key = m.get("key")
        if key:
            raw = raw.replace(key, "")
    return re.sub(r"\s+", " ", raw).strip()


# === main flow ===


def _base_messages(system_content: str, history: list[dict]) -> list[dict]:
    """system message + wrap 过的历史消息；回复路径和主动发言路径共用。
    user 历史按 direct / observer 走不同 wrap 模板，assistant 原样带过。"""
    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    for m in history:
        if m["role"] == "user":
            messages.append({
                "role": "user",
                "content": _wrap_user(
                    m["content"],
                    sender_name=m.get("sender_name", ""),
                    is_observer=m.get("is_observer", False),
                ),
            })
        else:
            messages.append({"role": m["role"], "content": m["content"]})
    return messages

async def _call_llm_with_memory(
    pet_id: int, user_text: str, sender_name: str = "", current_msg_id: int | None = None
) -> tuple[str, dict]:
    history, current_state = await load_pet_context(pet_id)

    # RAG：用当前 user_text 检索相关卡片 + 拼最近卡片，渲染成段塞进 system message
    recall_block = await build_recall_block(pet_id, query=user_text)
    system_content = SYSTEM_PROMPT + recall_block

    # 本条 user 输入在调用前已入库（保证 messages.id 反映真实到达顺序），
    # 这里按 id 把它从 history 摘掉，避免它既在历史里又作为末条 user 出现而重复。
    hist = [m for m in history if m.get("id") != current_msg_id]
    messages = _base_messages(system_content, hist)

    # 临近新输入：拼一条 system 消息，含 (当前状态感受 + JSON 输出契约 + 人设重申)，
    # 利用 recency bias 让这三条最权威。state 中段维度自动不渲染。
    pre_user_system = (
        _render_state(current_state)
        + "\n"
        + JSON_OUTPUT_PROMPT
        + "\n"
        + PERSONA_REINFORCEMENT
    )
    messages.append({"role": "system", "content": pre_user_system})
    messages.append({
        "role": "user",
        "content": _wrap_user(user_text, sender_name=sender_name),
    })

    resp = await llm.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        max_tokens=REPLY_MAX_TOKENS,  # JSON 包装比纯文本多消耗一些
        temperature=0.9,
        response_format={"type": "json_object"},
    )
    content = (resp.choices[0].message.content or "").strip()

    # 解析 LLM 的 JSON：失败就把 content 当 reply 兜底，state 不变
    try:
        data = json.loads(content)
        reply = (data.get("reply") or "").strip()
        delta = data.get("state_delta") or {}
        if not isinstance(delta, dict):
            delta = {}
    except json.JSONDecodeError:
        log.warning("LLM returned non-JSON: %r", content[:200])
        reply = content
        delta = {}

    if not reply:
        reply = FALLBACK_REPLIES["empty_llm"]

    new_state = _apply_delta(current_state, delta)
    new_state["last_update_ts"] = time.time()
    await update_pet_state(pet_id, new_state)
    log.info(
        "pet %d state: %s + delta=%s → %s",
        pet_id,
        {k: round(current_state.get(k, 0)) for k in STATE_NUMERIC_KEYS},
        {k: int(delta.get(k, 0)) for k in STATE_NUMERIC_KEYS},
        {k: round(new_state.get(k, 0)) for k in STATE_NUMERIC_KEYS},
    )

    return reply, new_state


async def _mark_replied(pet_id: int) -> None:
    """记录本次正式回复的时间戳，用于群 @ 回复节流（[reply].min_interval_sec）。"""
    state = await load_pet_state(pet_id)
    state["last_reply_ts"] = time.time()
    await update_pet_state(pet_id, state)


async def _is_direct_to_bot(mentions: list[dict]) -> bool:
    """判断这条群消息是不是"对宠物说的"——只有 @ 了 bot 才 direct。
    拿不到 bot open_id（API 挂 / 权限未批）则退化成"全部 direct"，保持老行为。
    """
    try:
        bot_open_id = await _get_bot_open_id()
    except Exception:
        log.exception("get bot open_id failed; falling back to treating all messages as direct")
        return True
    return any((m.get("id") or {}).get("open_id") == bot_open_id for m in mentions)


async def _handle_message_event(event: dict) -> None:
    msg = event.get("message") or {}
    message_id = msg.get("message_id")
    chat_id = msg.get("chat_id")
    if not message_id or not chat_id:
        return

    mentions = msg.get("mentions") or []
    sender_open_id = (((event.get("sender") or {}).get("sender_id") or {}).get("open_id")) or ""
    sender_name = await _resolve_user_name(sender_open_id) if sender_open_id else "群友"

    is_direct = await _is_direct_to_bot(mentions)
    msg_type = msg.get("message_type")

    # observer 模式：群里别人在聊，不针对宠物
    if not is_direct:
        if msg_type != "text":
            return  # 图片 / 文件 / 表情包暂不作为 observer 记录
        try:
            content = json.loads(msg.get("content") or "{}")
        except json.JSONDecodeError:
            return
        text = _clean_text(content.get("text", ""), mentions)
        if not text:
            return
        # 没建过宠物的群，不因为观察消息就自动孵化
        pet_id = await find_pet(chat_id)
        if pet_id is None:
            return
        # 不逐条落库：先缓冲在进程内，攒到 tick 或下一条 direct 消息再批量处理
        buf = _observer_buffer.setdefault(pet_id, [])
        buf.append({"content": text, "sender_name": sender_name, "ts": time.time()})
        log.info(
            "pet %d buffered observer [%s]: %r (buffer=%d)",
            pet_id, sender_name, text[:80], len(buf),
        )
        # 安全上限：缓冲过多立刻 flush，避免进程内存无界增长
        if len(buf) >= OBSERVER_FLUSH_MAX_COUNT:
            await flush_observer_buffer(pet_id)
        return

    # direct 模式：对宠物说话，走完整 LLM
    if msg_type != "text":
        await _reply_text(message_id, FALLBACK_REPLIES["non_text"])
        return

    try:
        content = json.loads(msg.get("content") or "{}")
    except json.JSONDecodeError:
        log.warning("bad content json: %r", msg.get("content"))
        return

    user_text = _clean_text(content.get("text", ""), mentions)
    if not user_text:
        await _reply_text(message_id, FALLBACK_REPLIES["empty_text"])
        return

    pet_id = await get_or_create_pet(chat_id)
    log.info("pet_id=%d chat_id=%s sender=%s user_text=%r", pet_id, chat_id, sender_name, user_text)

    # 先把缓冲的旁听消息落库，保证 messages.id 顺序≈真实时间顺序
    await flush_observer_buffer(pet_id)

    # 休息时段：群 @ / 私聊都不调 LLM，只回一句固定的睡觉文案；
    # 消息照常写 messages（is_observer=0），宠物醒来后能看到。
    if _in_quiet_hours(time.time()):
        await append_message(
            pet_id, "user", user_text, sender_name=sender_name, is_observer=False
        )
        await _reply_text(message_id, FALLBACK_REPLIES["quiet_hours"])
        log.info("pet %d @ during quiet hours, sent sleeping reply", pet_id)
        if (await count_unsummarized(pet_id)) > COMPRESS_THRESHOLD:
            asyncio.create_task(compress_pet_memory(pet_id))
        return

    # 回复节流：群里被 @ 时，距上次正式回复不足 min_interval_sec 则只记不回。
    if REPLY_MIN_INTERVAL_SEC > 0:
        now = time.time()
        db_ts = float((await load_pet_state(pet_id)).get("last_reply_ts", 0.0))
        # DB last_reply_ts 在 LLM 回完才写，取 max(_reply_gate) 才能挡住同一波在途的并发 @。
        last_reply_ts = max(db_ts, _reply_gate.get(pet_id, 0.0))
        elapsed = now - last_reply_ts
        if elapsed < REPLY_MIN_INTERVAL_SEC:
            await append_message(
                pet_id, "user", user_text, sender_name=sender_name, is_observer=False
            )
            log.info(
                "pet %d @ within reply cooldown (%.0fs left), recorded without reply",
                pet_id, REPLY_MIN_INTERVAL_SEC - elapsed,
            )
            if (await count_unsummarized(pet_id)) > COMPRESS_THRESHOLD:
                asyncio.create_task(compress_pet_memory(pet_id))
            return
        # 通过节流——立即占位。上面 _reply_gate.get 到这里无 await，并发 @ 无法插进来漏放。
        _reply_gate[pet_id] = now

    # 本条输入先入库，让它的 id 反映真实到达顺序（节流消息同样即时入库，不会被回复
    # 期间的 LLM 延迟挤到后面）；_call_llm_with_memory 按 current_msg_id 把它从历史里
    # 摘出来当末条 user，避免重复。
    current_msg_id = await append_message(
        pet_id, "user", user_text, sender_name=sender_name, is_observer=False
    )
    try:
        reply, _ = await _call_llm_with_memory(
            pet_id, user_text, sender_name=sender_name, current_msg_id=current_msg_id
        )
    except Exception as e:
        log.exception("llm error")
        reply = FALLBACK_REPLIES["llm_error_template"].format(error_class=e.__class__.__name__)
    log.info("reply=%r", reply)

    await append_message(pet_id, "assistant", reply)
    await _reply_text(message_id, reply)
    await _mark_replied(pet_id)

    if (await count_unsummarized(pet_id)) > COMPRESS_THRESHOLD:
        asyncio.create_task(compress_pet_memory(pet_id))


# === 主动发言（autonomous proactive speech） ===

# (_local_hour and _local_date_hour are defined near the top of the file)


def _should_tick_speak(state: dict, last_proactive_ts: float, now: float) -> str | None:
    """代码层廉价过滤：返回触发情境字符串就该让 LLM 说话，None 就跳过这一 tick。"""
    # 静默时段
    if _in_quiet_hours(now):
        return None
    # 冷却
    if now - last_proactive_ts < PROACTIVE_COOLDOWN_SEC:
        return None
    # 状态触发（按强烈度排序）
    if state["hunger"] >= HUNGER_TRIGGER:
        return PROACTIVE_TRIGGER_TEMPLATES["hunger"].format(hunger=round(state["hunger"]))
    if state["mood"] <= MOOD_TRIGGER:
        return PROACTIVE_TRIGGER_TEMPLATES["mood"].format(mood=round(state["mood"]))
    if state["energy"] <= ENERGY_TRIGGER:
        return PROACTIVE_TRIGGER_TEMPLATES["energy"].format(energy=round(state["energy"]))
    # 自发：cooldown 过了 + 状态没触发 → 小概率冒个泡
    if random.random() < SPONTANEOUS_PROB:
        return PROACTIVE_TRIGGER_TEMPLATES["spontaneous"]
    return None


def _scheduled_event_due(state: dict, now: float) -> tuple[dict, str] | None:
    """固定时刻的日记 / 梦境触发。独立于 state 和普通主动发言冷却。"""
    date_key, hour = _local_date_hour(now)
    for event in SCHEDULED_EVENTS:
        if hour >= event["hour"] and state.get(event["state_key"]) != date_key:
            return event, date_key
    return None


async def _autonomous_speak(
    pet_id: int,
    chat_id: str,
    prompt: str,
    user_stub: str,
    log_label: str,
    *,
    max_tokens: int,
    set_last_proactive: bool = False,
    extra_state: dict | None = None,
    as_card: bool = False,
    card_actions: list[str] | None = None,
) -> tuple[str, dict] | None:
    """让宠物主动发一句话，发飞书、存 DB、更新 state。
    as_card=True 时发交互卡片（状态条 + 互动按钮），否则发纯文本。
    card_actions 不为 None 时卡片用这组固定按钮，否则按 state 动态挑。"""
    history, current_state = await load_pet_context(pet_id)

    # 主动发言没有 user_text 做检索 query，只走最近卡片提供时序氛围。
    recall_block = await build_recall_block(pet_id, query="", k_recent=5)
    system_content = SYSTEM_PROMPT + recall_block

    messages = _base_messages(system_content, history)

    pre = (
        _render_state(current_state)
        + "\n"
        + JSON_OUTPUT_PROMPT
        + "\n"
        + PERSONA_REINFORCEMENT
        + "\n"
        + prompt
    )
    messages.append({"role": "system", "content": pre})
    # 合成一条 user 占位（明确标注是系统触发），避免某些模型对"全是 system"产生奇怪输出
    messages.append({"role": "user", "content": user_stub})

    resp = await llm.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.95,
        response_format={"type": "json_object"},
    )
    content = (resp.choices[0].message.content or "").strip()

    try:
        data = json.loads(content)
        reply = (data.get("reply") or "").strip()
        delta = data.get("state_delta") or {}
        if not isinstance(delta, dict):
            delta = {}
    except json.JSONDecodeError:
        log.warning("autonomous LLM returned non-JSON, skipping: %r", content[:200])
        return None

    if not reply:
        log.warning("autonomous LLM returned empty reply, skipping")
        return None

    now = time.time()
    new_state = _apply_delta(current_state, delta)
    new_state["last_update_ts"] = now
    if set_last_proactive:
        new_state["last_proactive_ts"] = now
    if extra_state:
        new_state.update(extra_state)

    # 先发飞书，发成功再写 DB，避免"DB 记了但群里没看到"的鬼现象。
    if as_card and CARD_ENABLED:
        await _send_card(
            chat_id, _build_pet_card(pet_id, reply, new_state, action_keys=card_actions)
        )
    else:
        await _send_text(chat_id, reply)

    await append_message(pet_id, "assistant", reply)
    await update_pet_state(pet_id, new_state)

    log.info(
        "pet %d %s: reply=%r state=%s",
        pet_id, log_label, reply[:100],
        {k: round(new_state.get(k, 0)) for k in STATE_NUMERIC_KEYS},
    )
    return reply, new_state


async def _proactive_speak(pet_id: int, chat_id: str, trigger: str) -> tuple[str, dict] | None:
    """让宠物按 trigger 主动说一句话。"""
    return await _autonomous_speak(
        pet_id,
        chat_id,
        PROACTIVE_PROMPT.format(trigger=trigger),
        PROACTIVE_USER_STUB_TEMPLATE,
        f"PROACTIVE trigger={trigger[:60]!r}",
        max_tokens=REPLY_MAX_TOKENS,
        set_last_proactive=True,
        as_card=True,
    )


async def _scheduled_speak(
    pet_id: int,
    chat_id: str,
    event: dict,
    date_key: str,
    *,
    mark_date: bool = True,
) -> tuple[str, dict] | None:
    """固定时刻的日记 / 梦境。成功发出后才写每日去重字段。
    发交互卡片，按钮是该事件固定的「晚安」/「早上好」（prompts.toml 里的 card_action）。"""
    action = event.get("card_action")
    return await _autonomous_speak(
        pet_id,
        chat_id,
        SCHEDULED_EVENT_PROMPT.format(
            event_name=event["name"],
            scheduled_hour=event["hour"],
            instruction=event["instruction"],
        ),
        SCHEDULED_USER_STUB_TEMPLATE.format(event_name=event["name"]),
        f"SCHEDULED {event['kind']} date={date_key}",
        max_tokens=SCHEDULED_MAX_TOKENS,
        extra_state={event["state_key"]: date_key} if mark_date else None,
        as_card=True,
        card_actions=[action] if action else [],
    )


def _load_all_pets() -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT id, chat_id, state_json FROM pets").fetchall()
    return [dict(r) for r in rows]


async def _tick_all_pets() -> None:
    try:
        await clean_old_events()
    except Exception:
        log.exception("clean_old_events failed")

    # 把上个 tick 周期攒下的旁听消息批量落库
    await flush_all_observer_buffers()

    now = time.time()
    rows = await asyncio.to_thread(_load_all_pets)
    for row in rows:
        pet_id = row["id"]
        chat_id = row["chat_id"]
        stored = _decode_state(row["state_json"])
        current = _decay_state(stored, now, pet_id)
        scheduled = _scheduled_event_due(current, now)
        if scheduled is not None:
            event, date_key = scheduled
            try:
                await _scheduled_speak(pet_id, chat_id, event, date_key)
            except Exception:
                log.exception("scheduled speak failed for pet %d", pet_id)
            continue
        last_proactive_ts = float(stored.get("last_proactive_ts", 0))
        trigger = _should_tick_speak(current, last_proactive_ts, now)
        if trigger is None:
            continue
        try:
            await _proactive_speak(pet_id, chat_id, trigger)
        except Exception:
            log.exception("proactive speak failed for pet %d", pet_id)


async def autonomous_loop() -> None:
    """常驻心跳：每 TICK_INTERVAL_SEC 秒扫一次所有宠物，决定是否主动发言。"""
    log.info(
        "autonomous_loop start tick=%ds cooldown=%ds quiet=%s tz_offset=%s",
        TICK_INTERVAL_SEC, PROACTIVE_COOLDOWN_SEC, QUIET_HOURS, PROACTIVE_TZ_OFFSET_HOURS,
    )
    while True:
        try:
            await asyncio.sleep(TICK_INTERVAL_SEC)
            await _tick_all_pets()
        except asyncio.CancelledError:
            log.info("autonomous_loop cancelled")
            raise
        except Exception:
            log.exception("autonomous tick crashed, loop continues")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(autonomous_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # 进程退出前把还在缓冲里的旁听消息落库，避免 systemd restart 丢上下文
        await flush_all_observer_buffers()
        await http.aclose()


app = FastAPI(lifespan=lifespan)
_init_db()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# === GM web API ===

def _gm_auth(req: Request) -> JSONResponse | None:
    if not GM_TOKEN:
        return JSONResponse({"error": "gm_disabled"}, status_code=403)
    token = req.query_params.get("token") or req.headers.get("X-GM-Token")
    if not hmac.compare_digest(token or "", GM_TOKEN):
        return JSONResponse({"error": "gm_unauthorized"}, status_code=401)
    return None


def _gm_public_state(state: dict) -> dict:
    out: dict = {}
    for k in STATE_NUMERIC_KEYS:
        out[k] = round(float(state.get(k, 0)), 1)
    out["recent_vibe"] = state.get("recent_vibe", "")
    out["recent_vibe_date"] = state.get("recent_vibe_date", "")
    out["last_update_ts"] = state.get("last_update_ts")
    out["last_proactive_ts"] = state.get("last_proactive_ts")
    out["last_reply_ts"] = state.get("last_reply_ts")
    out["last_dream_date"] = state.get("last_dream_date", "")
    out["last_diary_date"] = state.get("last_diary_date", "")
    return out


def _gm_event(kind: str) -> dict | None:
    for event in SCHEDULED_EVENTS:
        if event["kind"] == kind:
            return event
    return None


def _gm_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _db_resolve_pet(pet_id: int | None) -> tuple[dict | None, list[dict]]:
    with _db() as conn:
        if pet_id is not None:
            row = conn.execute("SELECT id, chat_id FROM pets WHERE id = ?", (pet_id,)).fetchone()
            return dict(row) if row else None, []
        else:
            rows = conn.execute("SELECT id, chat_id FROM pets ORDER BY id").fetchall()
            return None, [dict(r) for r in rows]


async def _gm_resolve_pet(chat_id: str | None = None, pet_id: int | None = None) -> tuple[int, str] | JSONResponse:
    if chat_id:
        created_id = await get_or_create_pet(chat_id)
        return created_id, chat_id

    row_dict, rows = await asyncio.to_thread(_db_resolve_pet, pet_id)
    if pet_id is not None:
        if row_dict is None:
            return JSONResponse({"error": "pet_not_found", "pet_id": pet_id}, status_code=404)
        return row_dict["id"], row_dict["chat_id"]
    else:
        if len(rows) != 1:
            return JSONResponse(
                {
                    "error": "target_required",
                    "hint": "pass chat_id or pet_id; if no pet exists, pass chat_id to create one",
                    "pets": [{"id": r["id"], "chat_id": r["chat_id"]} for r in rows],
                },
                status_code=400,
            )
        return rows[0]["id"], rows[0]["chat_id"]


async def _gm_body(req: Request) -> dict:
    if not (req.headers.get("content-type") or "").startswith("application/json"):
        return {}
    try:
        data = await req.json()
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


@app.get("/gm/help")
async def gm_help(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    return {
        "auth": "pass ?token=... or X-GM-Token; token is GM_TOKEN, fallback FEISHU_VERIFICATION_TOKEN",
        "endpoints": {
            "GET /gm/pets": "list pets",
            "GET /gm/state": "read state; query: chat_id or pet_id",
            "POST /gm/state": "set/delta state; json: {chat_id|pet_id, set:{hunger,mood,energy,curiosity,affection}, delta:{...}, recent_vibe?: '...' | 'random'}",
            "POST /gm/speak": "force proactive speech; json: {chat_id|pet_id,trigger?}",
            "POST /gm/dream": "force dream; json: {chat_id|pet_id,mark?}",
            "POST /gm/diary": "force diary; json: {chat_id|pet_id,mark?}",
            "POST /gm/tick": "run one autonomous tick for all pets",
        },
    }


def _get_gm_pets() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT p.id, p.chat_id, p.born_at, p.summary_until_id, p.state_json, "
            "COUNT(DISTINCT m.id) AS message_count, "
            "COUNT(DISTINCT c.id) AS card_count "
            "FROM pets p "
            "LEFT JOIN messages m ON m.pet_id = p.id "
            "LEFT JOIN memory_cards c ON c.pet_id = p.id "
            "GROUP BY p.id ORDER BY p.id"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/gm/pets")
async def gm_pets(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    rows = await asyncio.to_thread(_get_gm_pets)
    pets = []
    for row in rows:
        state = _decay_state(_decode_state(row["state_json"]), time.time(), row["id"])
        pets.append({
            "id": row["id"],
            "chat_id": row["chat_id"],
            "born_at": row["born_at"],
            "message_count": row["message_count"],
            "card_count": row["card_count"],
            "summary_until_id": row["summary_until_id"],
            "state": _gm_public_state(state),
        })
    return {"pets": pets}


@app.get("/gm/state")
async def gm_get_state(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    pet_id_param = req.query_params.get("pet_id")
    resolved = await _gm_resolve_pet(
        chat_id=req.query_params.get("chat_id"),
        pet_id=int(pet_id_param) if pet_id_param else None,
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved
    state = await load_pet_state(pet_id)
    return {"pet_id": pet_id, "chat_id": chat_id, "state": _gm_public_state(state)}


@app.post("/gm/state")
async def gm_set_state(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    body = await _gm_body(req)
    pet_id_param = body.get("pet_id") or req.query_params.get("pet_id")
    resolved = await _gm_resolve_pet(
        chat_id=body.get("chat_id") or req.query_params.get("chat_id"),
        pet_id=int(pet_id_param) if pet_id_param else None,
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved

    state = await load_pet_state(pet_id)
    set_values = body.get("set") if isinstance(body.get("set"), dict) else {}
    delta_values = body.get("delta") if isinstance(body.get("delta"), dict) else {}
    for key in STATE_NUMERIC_KEYS:
        if key in req.query_params:
            set_values[key] = req.query_params[key]

    changed: dict = {}
    for key in STATE_NUMERIC_KEYS:
        if key in set_values:
            value = float(set_values[key])
            state[key] = max(0.0, min(100.0, value))
            changed[key] = state[key]
        if key in delta_values:
            value = float(delta_values[key])
            state[key] = max(0.0, min(100.0, float(state[key]) + value))
            changed[key] = state[key]
    # 允许 GM 手动改 recent_vibe（清空 / 指定字符串 / "random" 触发立即重抽）
    rv_payload = set_values.get("recent_vibe")
    if rv_payload is None:
        rv_payload = body.get("recent_vibe")
    if rv_payload is not None:
        rv = str(rv_payload).strip()
        if rv.lower() == "random" and RECENT_VIBE_POOL:
            rv = random.choice(RECENT_VIBE_POOL)
        state["recent_vibe"] = rv
        # 清空 vibe_date 让下次互动重新滚或保持当天
        state["recent_vibe_date"] = ""
        changed["recent_vibe"] = rv
    state["last_update_ts"] = time.time()
    await update_pet_state(pet_id, state)
    return {
        "ok": True,
        "pet_id": pet_id,
        "chat_id": chat_id,
        "changed": changed,
        "state": _gm_public_state(state),
    }


@app.post("/gm/speak")
async def gm_speak(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    body = await _gm_body(req)
    pet_id_param = body.get("pet_id") or req.query_params.get("pet_id")
    resolved = await _gm_resolve_pet(
        chat_id=body.get("chat_id") or req.query_params.get("chat_id"),
        pet_id=int(pet_id_param) if pet_id_param else None,
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved
    trigger = body.get("trigger") or req.query_params.get("trigger") or GM_DEFAULT_SPEAK_TRIGGER
    result = await _proactive_speak(pet_id, chat_id, trigger)
    if result is None:
        return JSONResponse({"error": "llm_empty_or_invalid"}, status_code=502)
    reply, state = result
    return {"ok": True, "pet_id": pet_id, "chat_id": chat_id, "reply": reply, "state": _gm_public_state(state)}


async def _gm_scheduled(req: Request, kind: str):
    auth = _gm_auth(req)
    if auth:
        return auth
    event = _gm_event(kind)
    if event is None:
        return JSONResponse({"error": "unknown_event", "kind": kind}, status_code=404)
    body = await _gm_body(req)
    pet_id_param = body.get("pet_id") or req.query_params.get("pet_id")
    resolved = await _gm_resolve_pet(
        chat_id=body.get("chat_id") or req.query_params.get("chat_id"),
        pet_id=int(pet_id_param) if pet_id_param else None,
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved
    date_key, _ = _local_date_hour(time.time())
    mark = _gm_bool(body.get("mark", req.query_params.get("mark")), default=False)
    result = await _scheduled_speak(pet_id, chat_id, event, date_key, mark_date=mark)
    if result is None:
        return JSONResponse({"error": "llm_empty_or_invalid"}, status_code=502)
    reply, state = result
    return {
        "ok": True,
        "pet_id": pet_id,
        "chat_id": chat_id,
        "event": kind,
        "marked_date": date_key if mark else None,
        "reply": reply,
        "state": _gm_public_state(state),
    }


@app.post("/gm/dream")
async def gm_dream(req: Request):
    return await _gm_scheduled(req, "dream")


@app.post("/gm/diary")
async def gm_diary(req: Request):
    return await _gm_scheduled(req, "diary")


@app.post("/gm/tick")
async def gm_tick(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    await _tick_all_pets()
    return {"ok": True}


async def _card_action_followup(
    pet_id: int, action_key: str, message_id: str, clicker_open_id: str
) -> None:
    """按钮点击的后台收尾：让 LLM 生成一句有人格的反馈，回填卡片 + 写进记忆。"""
    try:
        sender_name = (
            await _resolve_user_name(clicker_open_id) if clicker_open_id else "群友"
        )
        text = CARD_ACTION_TEXT.get(action_key, {})
        did = text.get("did", "和你互动了一下")
        pending = text.get("pending", "…")

        # 把这次照料动作记进对话历史，让 LLM 能看到上下文
        await append_message(
            pet_id, "user", did, sender_name=sender_name, is_observer=False
        )

        history, state = await load_pet_context(pet_id)
        messages = _base_messages(SYSTEM_PROMPT, history)
        messages.append({
            "role": "system",
            "content": _render_state(state) + "\n" + PERSONA_REINFORCEMENT,
        })
        messages.append({
            "role": "user",
            "content": CARD_ACTION_REPLY_PROMPT.format(sender_name=sender_name, did=did),
        })

        reaction = ""
        try:
            resp = await llm.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                max_tokens=CARD_REPLY_MAX_TOKENS,
                temperature=0.9,
            )
            reaction = (resp.choices[0].message.content or "").strip()
        except Exception:
            log.exception("card action LLM reply failed; keeping pending text")
        if not reaction:
            reaction = pending

        await append_message(pet_id, "assistant", reaction)
        new_state = await load_pet_state(pet_id)
        await _update_card_message(
            message_id, _build_pet_card(pet_id, reaction, new_state, with_actions=False)
        )

        if (await count_unsummarized(pet_id)) > COMPRESS_THRESHOLD:
            asyncio.create_task(compress_pet_memory(pet_id))
    except Exception:
        log.exception("card action followup failed")


async def _handle_card_action(event: dict, event_id: str | None) -> dict:
    """处理卡片按钮点击（card.action.trigger）：套确定性 delta、原地刷新卡片。
    必须在 ~3s 内同步返回更新后的卡片；有人格的反馈台词由后台异步回填。"""
    try:
        action = event.get("action") or {}
        value = action.get("value") or {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}
        try:
            pet_id = int(value.get("pet_id"))
        except (TypeError, ValueError):
            pet_id = None
        action_key = value.get("action")
        context = event.get("context") or {}
        message_id = context.get("open_message_id")
        operator = event.get("operator") or {}
        clicker_open_id = operator.get("open_id") or operator.get("union_id") or ""

        if pet_id is None or action_key not in CARD_ACTIONS:
            return {"toast": {"type": "error", "content": "这个按钮点不动了…"}}

        try:
            state = await load_pet_state(pet_id)
        except ValueError:
            return {"toast": {"type": "error", "content": "找不到我了…"}}

        # 幂等：飞书重试同一 event_id 不重复套 delta（按钮 delta 是确定性的，重复会翻倍）
        if event_id and await check_and_register_event(event_id):
            return {}

        # 动作冷却：冷却中只回 toast，不改 state、不动卡片
        now = time.time()
        action_ts = dict(state.get("card_action_ts") or {})
        last_ts = float(action_ts.get(action_key, 0.0))
        if CARD_ACTION_COOLDOWN_SEC > 0 and now - last_ts < CARD_ACTION_COOLDOWN_SEC:
            return {"toast": {"type": "info", "content": CARD_TOAST_COOLDOWN}}

        # 套确定性 delta（不经过 LLM），写回 state
        delta = CARD_ACTIONS[action_key].get("delta") or {}
        new_state = _apply_card_delta(state, delta)
        action_ts[action_key] = now
        new_state["card_action_ts"] = action_ts
        new_state["last_update_ts"] = now
        await update_pet_state(pet_id, new_state)

        pending = CARD_ACTION_TEXT.get(action_key, {}).get("pending", "…")
        # 点过之后不再带按钮：这张卡片变成结果快照，避免按钮随状态轮换被无限点。
        card = _build_pet_card(pet_id, pending, new_state, with_actions=False)

        # 后台异步生成有人格的反馈台词，回填卡片
        if message_id:
            asyncio.create_task(
                _card_action_followup(pet_id, action_key, message_id, clicker_open_id)
            )

        log.info(
            "pet %d card action %s by %s -> %s",
            pet_id, action_key, clicker_open_id or "?",
            {k: round(new_state.get(k, 0)) for k in STATE_NUMERIC_KEYS},
        )
        return {
            "toast": {"type": "success", "content": CARD_TOAST_DONE},
            "card": {"type": "raw", "data": card},
        }
    except Exception:
        log.exception("card action failed")
        return {"toast": {"type": "error", "content": "呜…出了点小问题"}}


@app.post("/feishu/webhook")
async def feishu_webhook(req: Request, background: BackgroundTasks):
    raw = await req.json()

    if "encrypt" in raw:
        if not FEISHU_ENCRYPT_KEY:
            log.error("received encrypted payload but FEISHU_ENCRYPT_KEY not set")
            return JSONResponse({"code": 400}, status_code=400)
        try:
            body = json.loads(_decrypt_feishu(raw["encrypt"], FEISHU_ENCRYPT_KEY))
        except Exception:
            log.exception("decrypt failed")
            return JSONResponse({"code": 400}, status_code=400)
    else:
        body = raw

    # 1) URL 验证（在飞书后台配置回调 URL 时触发）
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}

    header = body.get("header") or {}

    # 2) Verification Token 校验（可选但建议开）
    if FEISHU_VERIFICATION_TOKEN and not hmac.compare_digest(
        header.get("token") or "", FEISHU_VERIFICATION_TOKEN
    ):
        log.warning("verification token mismatch")
        return JSONResponse({"code": 401}, status_code=401)

    event_id = header.get("event_id")
    event_type = header.get("event_type")

    # 3) 卡片按钮点击：必须同步返回更新后的卡片，不能丢进 BackgroundTasks。
    #    幂等去重在 _handle_card_action 内部按 event_id 处理。
    if event_type == "card.action.trigger":
        if not CARD_ENABLED:
            return {}
        return await _handle_card_action(body.get("event") or {}, event_id)

    # 4) 其它事件去重
    if event_id:
        if await check_and_register_event(event_id):
            return {"code": 0}

    # 5) 分发事件 — 申请了"接收群消息"权限后，群里所有消息都会进来，
    #    _handle_message_event 内部按 @ 判断是 direct 还是 observer。
    if event_type == "im.message.receive_v1":
        background.add_task(_handle_message_event, body.get("event") or {})

    return {"code": 0}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
    )
