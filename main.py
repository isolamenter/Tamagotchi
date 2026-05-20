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

_LLM_CONFIG = _PET_CONFIG["llm"]
REPLY_MAX_TOKENS = int(_LLM_CONFIG["reply_max_tokens"])
SCHEDULED_MAX_TOKENS = int(_LLM_CONFIG["scheduled_max_tokens"])
COMPRESS_MAX_TOKENS = int(_LLM_CONFIG["compress_max_tokens"])

_STATE_CONFIG = _PET_CONFIG["state"]
INITIAL_STATE = {k: float(v) for k, v in _STATE_CONFIG["initial"].items()}
DECAY_RATES_PER_HOUR = {
    k: float(v) for k, v in _STATE_CONFIG["decay_per_hour"].items()
}
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
        "last_dream_date": "",
        "last_diary_date": "",
        "recent_vibe": "",
        "recent_vibe_date": "",
    }


def _maybe_rotate_vibe(state: dict, now: float) -> dict:
    """每天滚一次 recent_vibe；同一本地日期内幂等。"""
    if not RECENT_VIBE_POOL:
        return state
    date_key, _ = _local_date_hour(now)
    if state.get("recent_vibe_date") == date_key and state.get("recent_vibe"):
        return state
    out = dict(state)
    out["recent_vibe"] = random.choice(RECENT_VIBE_POOL)
    out["recent_vibe_date"] = date_key
    return out


def _decay_state(stored: dict, now: float) -> dict:
    """根据 last_update_ts 到 now 的时间差，把存储的状态衰减到当前值。
    保留 stored 里所有未知字段（如 last_proactive_ts / recent_vibe），只覆写衰减项 + last_update_ts。
    顺便每天滚一次 recent_vibe。"""
    elapsed_hours = max(0.0, (now - float(stored.get("last_update_ts", now))) / 3600.0)
    result = dict(stored)
    result["last_update_ts"] = now
    for k, rate in DECAY_RATES_PER_HOUR.items():
        v = float(stored.get(k, INITIAL_STATE.get(k, 50.0))) + rate * elapsed_hours
        result[k] = max(0.0, min(100.0, v))
    return _maybe_rotate_vibe(result, now)


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


# === clients ===
http = httpx.AsyncClient(timeout=30.0)
llm = AsyncOpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)

# tenant access token cache
_token_cache: dict[str, float | str | None] = {"token": None, "expires_at": 0.0}

# event dedup（单进程 demo，重启会丢，但够用）
_seen_events: deque[str] = deque(maxlen=1000)
_seen_set: set[str] = set()

# per-pet compress lock，避免同一只宠物并发压缩
_compress_locks: dict[int, asyncio.Lock] = {}

# bot 自己的 open_id（启动后第一次拿 token 后填充），用来判断消息有没有 @ 自己
_bot_open_id_cache: dict[str, str] = {"open_id": ""}

# open_id -> 群友姓名缓存（contact API 调一次缓存一次，失败降级到短码）
_user_name_cache: dict[str, str] = {}


# === DB ===

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _init_db() -> None:
    # 开发期约定：每次迭代上线先 rm state.db，所以这里不做 ALTER 兼容；schema 只一次性 CREATE。
    with _db() as conn:
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
            """
        )


def get_or_create_pet(chat_id: str) -> int:
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


def find_pet(chat_id: str) -> int | None:
    """返回已存在的 pet_id，不存在不创建——observer 消息进来时用。"""
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM pets WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row["id"] if row else None


def append_message(
    pet_id: int,
    role: str,
    content: str,
    sender_name: str = "",
    is_observer: bool = False,
) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO messages (pet_id, role, content, ts, sender_name, is_observer) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pet_id, role, content, time.time(), sender_name, 1 if is_observer else 0),
        )


def _decode_state(state_json: str | None) -> dict:
    """把 pets.state_json 解析成 dict；坏 JSON / 空值都兜底回 _initial_state()。
    返回未衰减的存储态，调用方一般再 _decay_state 到当前。"""
    try:
        stored = json.loads(state_json or "{}")
    except json.JSONDecodeError:
        stored = {}
    return stored or _initial_state()


def load_pet_context(pet_id: int) -> tuple[list[dict], dict]:
    """返回 (unsummarized_messages_in_order, current_state_decayed_to_now)。
    history 每条带 role/content/sender_name/is_observer，给后续 _wrap_user 用。
    长期记忆走 memory_cards + RAG，不在这里返回。"""
    with _db() as conn:
        pet_row = conn.execute(
            "SELECT summary_until_id, state_json FROM pets WHERE id = ?", (pet_id,)
        ).fetchone()
        msg_rows = conn.execute(
            "SELECT role, content, sender_name, is_observer FROM messages "
            "WHERE pet_id = ? AND id > ? ORDER BY id",
            (pet_id, pet_row["summary_until_id"]),
        ).fetchall()
    history = [
        {
            "role": r["role"],
            "content": r["content"],
            "sender_name": r["sender_name"] or "",
            "is_observer": bool(r["is_observer"]),
        }
        for r in msg_rows
    ]
    current = _decay_state(_decode_state(pet_row["state_json"]), time.time())
    return history, current


def update_pet_state(pet_id: int, state: dict) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE pets SET state_json = ? WHERE id = ?",
            (json.dumps(state), pet_id),
        )


def load_pet_state(pet_id: int) -> dict:
    with _db() as conn:
        row = conn.execute(
            "SELECT state_json FROM pets WHERE id = ?", (pet_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"pet not found: {pet_id}")
    return _decay_state(_decode_state(row["state_json"]), time.time())


def count_unsummarized(pet_id: int) -> int:
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM messages "
            "WHERE pet_id = ? "
            "AND id > (SELECT summary_until_id FROM pets WHERE id = ?)",
            (pet_id, pet_id),
        ).fetchone()
    return row["c"]


# === embeddings ===

def _vec_pack(vec: list[float]) -> bytes:
    """float32 LE 紧凑存 BLOB；1536 维 → 6KB。"""
    return struct.pack(f"<{len(vec)}f", *vec)


def _vec_unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob)//4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    s = na = nb = 0.0
    for x, y in zip(a, b):
        s += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    return s / denom if denom else 0.0


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
    _store_embedding(pet_id, kind, source_id, content, vec)


def _score_cards(pet_id: int, q_vec: list[float], k: int) -> list[dict]:
    """同步重活：查卡片库 + 逐条 unpack 向量 + cosine 打分 + 取 top-K。
    DB 读和纯 Python 余弦都是阻塞操作，由调用方丢进线程池，避免卡住事件循环。"""
    with _db() as conn:
        rows = conn.execute(
            "SELECT e.source_id, e.vec, c.when_text, c.who, c.what, c.vibe "
            "FROM embeddings e JOIN memory_cards c ON c.id = e.source_id "
            "WHERE e.pet_id = ? AND e.kind = 'card' "
            "ORDER BY e.id DESC LIMIT 2000",
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


async def compress_pet_memory(pet_id: int) -> None:
    async with _compress_lock(pet_id):
        with _db() as conn:
            pet_row = conn.execute(
                "SELECT summary_until_id FROM pets WHERE id = ?", (pet_id,)
            ).fetchone()
            rows = conn.execute(
                "SELECT id, role, content, sender_name, is_observer FROM messages "
                "WHERE pet_id = ? AND id > ? ORDER BY id",
                (pet_id, pet_row["summary_until_id"]),
            ).fetchall()

        if len(rows) <= BUFFER_KEEP + 1:
            # 已经被别的协程压过了，或者还没到压的份上
            return

        to_compress = rows[: len(rows) - BUFFER_KEEP]
        new_until_id = to_compress[-1]["id"]
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
            return

        try:
            data = json.loads(content)
            cards_raw = data.get("cards") or []
            if not isinstance(cards_raw, list):
                cards_raw = []
        except json.JSONDecodeError:
            log.warning("compress returned non-JSON for pet %d: %r", pet_id, content[:200])
            return

        inserted: list[tuple[int, dict]] = []
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
            # 把 until pointer 推进——不管这次抽了几张卡片，这批消息都算压完了
            conn.execute(
                "UPDATE pets SET summary_until_id = ? WHERE id = ?",
                (new_until_id, pet_id),
            )
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
    now = time.time()
    cached = _token_cache.get("token")
    expires_at = float(_token_cache.get("expires_at") or 0)
    if isinstance(cached, str) and expires_at > now + 60:
        return cached
    r = await http.post(
        f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"get tenant_access_token failed: {data}")
    token = data["tenant_access_token"]
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + float(data.get("expire", 7000))
    return token


async def _get_bot_open_id() -> str:
    """拿 bot 自己的 open_id；调一次缓存，失败抛异常由调用方降级。"""
    cached = _bot_open_id_cache["open_id"]
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
    _bot_open_id_cache["open_id"] = open_id
    log.info("bot open_id resolved: %s", open_id)
    return open_id


async def _resolve_user_name(open_id: str) -> str:
    """open_id -> 真实姓名；contact 权限没批 / 调用失败 -> 降级到 '群友-后4位'。"""
    if not open_id:
        return "群友"
    cached = _user_name_cache.get(open_id)
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
            # 99991672 = 没权限；不刷屏，只记一次
            log.info("resolve_user_name fallback (code=%s msg=%s)", data.get("code"), data.get("msg"))
    except Exception:
        log.exception("resolve_user_name network error for %s", open_id)
    if not name:
        name = f"群友-{open_id[-4:]}"
    _user_name_cache[open_id] = name
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
    pet_id: int, user_text: str, sender_name: str = ""
) -> tuple[str, dict]:
    history, current_state = load_pet_context(pet_id)

    # RAG：用当前 user_text 检索相关卡片 + 拼最近卡片，渲染成段塞进 system message
    recall_block = await build_recall_block(pet_id, query=user_text)
    system_content = SYSTEM_PROMPT + recall_block

    messages = _base_messages(system_content, history)

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
    update_pet_state(pet_id, new_state)
    log.info(
        "pet %d state: %s + delta=%s → %s",
        pet_id,
        {k: round(current_state.get(k, 0)) for k in STATE_NUMERIC_KEYS},
        {k: int(delta.get(k, 0)) for k in STATE_NUMERIC_KEYS},
        {k: round(new_state.get(k, 0)) for k in STATE_NUMERIC_KEYS},
    )

    return reply, new_state


async def _is_direct_to_bot(msg: dict, chat_type: str, mentions: list[dict]) -> bool:
    """判断这条消息是不是"对宠物说的"。
    - 私聊 (p2p) 永远 direct
    - 群里只有 @ 了 bot 才 direct
    - 拿不到 bot open_id（API 挂 / 权限未批）则退化成"全部 direct"，保持老行为
    """
    if chat_type == "p2p":
        return True
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

    chat_type = msg.get("chat_type") or ""
    mentions = msg.get("mentions") or []
    sender_open_id = (((event.get("sender") or {}).get("sender_id") or {}).get("open_id")) or ""
    sender_name = await _resolve_user_name(sender_open_id) if sender_open_id else "群友"

    is_direct = await _is_direct_to_bot(msg, chat_type, mentions)
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
        pet_id = find_pet(chat_id)
        if pet_id is None:
            return
        append_message(pet_id, "user", text, sender_name=sender_name, is_observer=True)
        log.info("pet %d observed [%s]: %r", pet_id, sender_name, text[:80])
        if count_unsummarized(pet_id) > COMPRESS_THRESHOLD:
            asyncio.create_task(compress_pet_memory(pet_id))
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

    pet_id = get_or_create_pet(chat_id)
    log.info("pet_id=%d chat_id=%s sender=%s user_text=%r", pet_id, chat_id, sender_name, user_text)

    try:
        reply, _ = await _call_llm_with_memory(pet_id, user_text, sender_name=sender_name)
    except Exception as e:
        log.exception("llm error")
        reply = FALLBACK_REPLIES["llm_error_template"].format(error_class=e.__class__.__name__)
    log.info("reply=%r", reply)

    append_message(pet_id, "user", user_text, sender_name=sender_name, is_observer=False)
    append_message(pet_id, "assistant", reply)
    await _reply_text(message_id, reply)

    if count_unsummarized(pet_id) > COMPRESS_THRESHOLD:
        asyncio.create_task(compress_pet_memory(pet_id))


# === 主动发言（autonomous proactive speech） ===

def _local_hour(now_ts: float) -> int:
    """无 zoneinfo 依赖的本地小时（按 PROACTIVE_TZ_OFFSET_HOURS 偏移）。"""
    return int(((now_ts + PROACTIVE_TZ_OFFSET_HOURS * 3600) / 3600) % 24)


def _local_date_hour(now_ts: float) -> tuple[str, int]:
    """返回本地日期 key 和小时，用于每日定时事件去重。"""
    local = time.gmtime(now_ts + PROACTIVE_TZ_OFFSET_HOURS * 3600)
    return time.strftime("%Y-%m-%d", local), local.tm_hour


def _should_tick_speak(state: dict, last_proactive_ts: float, now: float) -> str | None:
    """代码层廉价过滤：返回触发情境字符串就该让 LLM 说话，None 就跳过这一 tick。"""
    # 静默时段（支持跨午夜：start > end 时按夜间区间处理）
    h = _local_hour(now)
    qs, qe = QUIET_HOURS
    in_quiet = qs <= h < qe if qs < qe else (h >= qs or h < qe)
    if in_quiet:
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
        if hour == event["hour"] and state.get(event["state_key"]) != date_key:
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
) -> tuple[str, dict] | None:
    """让宠物主动发一句话，发飞书、存 DB、更新 state。"""
    history, current_state = load_pet_context(pet_id)

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
    await _send_text(chat_id, reply)

    append_message(pet_id, "assistant", reply)
    update_pet_state(pet_id, new_state)

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
    )


async def _scheduled_speak(
    pet_id: int,
    chat_id: str,
    event: dict,
    date_key: str,
    *,
    mark_date: bool = True,
) -> tuple[str, dict] | None:
    """固定时刻的日记 / 梦境。成功发出后才写每日去重字段。"""
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
    )


async def _tick_all_pets() -> None:
    now = time.time()
    with _db() as conn:
        rows = conn.execute("SELECT id, chat_id, state_json FROM pets").fetchall()
    for row in rows:
        pet_id = row["id"]
        chat_id = row["chat_id"]
        stored = _decode_state(row["state_json"])
        current = _decay_state(stored, now)
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


def _gm_resolve_pet(chat_id: str | None = None, pet_id: int | None = None) -> tuple[int, str] | JSONResponse:
    if chat_id:
        return get_or_create_pet(chat_id), chat_id
    with _db() as conn:
        if pet_id is not None:
            row = conn.execute("SELECT id, chat_id FROM pets WHERE id = ?", (pet_id,)).fetchone()
        else:
            rows = conn.execute("SELECT id, chat_id FROM pets ORDER BY id").fetchall()
            if len(rows) != 1:
                return JSONResponse(
                    {
                        "error": "target_required",
                        "hint": "pass chat_id or pet_id; if no pet exists, pass chat_id to create one",
                        "pets": [{"id": r["id"], "chat_id": r["chat_id"]} for r in rows],
                    },
                    status_code=400,
                )
            row = rows[0]
    if row is None:
        return JSONResponse({"error": "pet_not_found", "pet_id": pet_id}, status_code=404)
    return row["id"], row["chat_id"]


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


@app.get("/gm/pets")
async def gm_pets(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
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
    pets = []
    for row in rows:
        state = _decay_state(_decode_state(row["state_json"]), time.time())
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
    resolved = _gm_resolve_pet(
        chat_id=req.query_params.get("chat_id"),
        pet_id=int(pet_id_param) if pet_id_param else None,
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved
    state = load_pet_state(pet_id)
    return {"pet_id": pet_id, "chat_id": chat_id, "state": _gm_public_state(state)}


@app.post("/gm/state")
async def gm_set_state(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    body = await _gm_body(req)
    pet_id_param = body.get("pet_id") or req.query_params.get("pet_id")
    resolved = _gm_resolve_pet(
        chat_id=body.get("chat_id") or req.query_params.get("chat_id"),
        pet_id=int(pet_id_param) if pet_id_param else None,
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved

    state = load_pet_state(pet_id)
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
    update_pet_state(pet_id, state)
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
    resolved = _gm_resolve_pet(
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
    resolved = _gm_resolve_pet(
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

    # 3) 事件去重
    event_id = header.get("event_id")
    if event_id:
        if event_id in _seen_set:
            return {"code": 0}
        if len(_seen_events) == _seen_events.maxlen:
            _seen_set.discard(_seen_events[0])
        _seen_events.append(event_id)
        _seen_set.add(event_id)

    # 4) 分发事件 — 申请了"接收群消息"权限后，群里所有消息都会进来，
    #    _handle_message_event 内部按 @ 判断是 direct 还是 observer。
    if header.get("event_type") == "im.message.receive_v1":
        background.add_task(_handle_message_event, body.get("event") or {})

    return {"code": 0}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
    )
