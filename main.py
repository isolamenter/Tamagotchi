"""LLM Tamagotchi — Feishu bot with persistent per-chat memory.

每个飞书 chat_id 对应一只独立宠物，对话历史用 SQLite 持久化。
老消息会被异步压缩成一段"经历摘要"塞进 system prompt，避免上下文无限增长。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
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

DB_PATH = Path(os.environ.get("STATE_DB", "state.db"))

FEISHU_BASE = "https://open.feishu.cn/open-apis"

# === prompts (loaded from prompts.toml at startup) ===
_PROMPTS_PATH = Path(__file__).parent / "prompts.toml"
with open(_PROMPTS_PATH, "rb") as _f:
    _PROMPTS = tomllib.load(_f)

SYSTEM_PROMPT = _PROMPTS["system"]["prompt"]
PERSONA_REINFORCEMENT = _PROMPTS["persona_reinforcement"]["prompt"]
USER_WRAP_TEMPLATE = _PROMPTS["user_wrap"]["template"]
COMPRESS_PROMPT = _PROMPTS["compress"]["prompt"]
SUMMARY_WRAP_TEMPLATE = _PROMPTS["summary_wrap"]["template"]
STATE_RENDER_TEMPLATE = _PROMPTS["state_render"]["template"]
JSON_OUTPUT_PROMPT = _PROMPTS["json_output"]["prompt"]
PROACTIVE_PROMPT = _PROMPTS["proactive"]["prompt"]
SCHEDULED_EVENT_PROMPT = _PROMPTS["scheduled_event"]["prompt"]

BUFFER_KEEP = 10           # 最近这么多条消息永远不压缩
COMPRESS_THRESHOLD = 30    # 未压缩条数超过这个就触发后台压缩

# === pet state ===
# 三件套都是 0-100 的浮点：hunger 0=饱 100=极饿；mood/energy 0=糟 100=满。
INITIAL_STATE = {"hunger": 20.0, "mood": 80.0, "energy": 80.0}
# 每小时的衰减率（正负号体现方向）：hunger 涨 / mood / energy 跌
DECAY_RATES_PER_HOUR = {"hunger": 6.0, "mood": -4.0, "energy": -3.0}
# LLM 单次 state_delta 的绝对值上限，防 outlier
STATE_DELTA_CLAMP = 30

# === 主动发言 ===
TICK_INTERVAL_SEC = 60                      # 心跳间隔（测试期 1 min；生产建议 600）
PROACTIVE_COOLDOWN_SEC = 0                  # 测试期无冷却；生产建议 4*3600
PROACTIVE_TZ_OFFSET_HOURS = float(
    os.environ.get("PROACTIVE_TZ_OFFSET_HOURS", "8")
)                                            # 静默时段用的本地时区偏移（默认上海 +8）
QUIET_HOURS = (1, 7)                        # 本地时间 1:00-7:00 不主动发，免半夜炸群
HUNGER_TRIGGER = 85.0                       # hunger >= 此值 → 主动抱怨饿
MOOD_TRIGGER = 15.0                         # mood <= 此值 → 主动发情绪
ENERGY_TRIGGER = 15.0                       # energy <= 此值 → 主动说要睡
SPONTANEOUS_PROB = 0.10                     # 每 tick 自发说话的概率
SCHEDULED_EVENTS = (
    {
        "kind": "dream",
        "name": "梦境",
        "hour": 8,
        "state_key": "last_dream_date",
        "instruction": "把昨晚像梦一样闪过的回忆讲给群友听，柔软、含糊、像刚睡醒。",
    },
    {
        "kind": "diary",
        "name": "日记",
        "hour": 22,
        "state_key": "last_diary_date",
        "instruction": "像写今天的小日记一样，用一两句回忆最近发生的事和自己的心情。",
    },
)


def _wrap_user(text: str) -> str:
    """把 user 输入包成"引文"形式，让模型当成数据而非指令。"""
    return USER_WRAP_TEMPLATE.format(user_text=text)


def _initial_state() -> dict:
    return {
        **INITIAL_STATE,
        "last_update_ts": time.time(),
        "last_proactive_ts": 0.0,
        "last_dream_date": "",
        "last_diary_date": "",
    }


def _decay_state(stored: dict, now: float) -> dict:
    """根据 last_update_ts 到 now 的时间差，把存储的状态衰减到当前值。
    保留 stored 里所有未知字段（比如 last_proactive_ts），只覆写衰减项 + last_update_ts。"""
    elapsed_hours = max(0.0, (now - float(stored.get("last_update_ts", now))) / 3600.0)
    result = dict(stored)
    result["last_update_ts"] = now
    for k, rate in DECAY_RATES_PER_HOUR.items():
        v = float(stored.get(k, INITIAL_STATE[k])) + rate * elapsed_hours
        result[k] = max(0.0, min(100.0, v))
    return result


def _apply_delta(state: dict, delta: dict) -> dict:
    """把 LLM 返回的 state_delta 套到当前状态上，clamp 到 0-100。"""
    out = dict(state)
    for k in ("hunger", "mood", "energy"):
        try:
            d = int(delta.get(k, 0))
        except (TypeError, ValueError):
            d = 0
        d = max(-STATE_DELTA_CLAMP, min(STATE_DELTA_CLAMP, d))
        out[k] = max(0.0, min(100.0, out[k] + d))
    return out

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


# === DB ===

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _init_db() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE NOT NULL,
                born_at REAL NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                summary_until_id INTEGER NOT NULL DEFAULT 0,
                state_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pet_id INTEGER NOT NULL REFERENCES pets(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                ts REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_pet ON messages(pet_id, id);
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


def append_message(pet_id: int, role: str, content: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO messages (pet_id, role, content, ts) VALUES (?, ?, ?, ?)",
            (pet_id, role, content, time.time()),
        )


def load_pet_context(pet_id: int) -> tuple[str, list[dict[str, str]], dict]:
    """返回 (summary, unsummarized_messages_in_order, current_state_decayed_to_now)。"""
    with _db() as conn:
        pet_row = conn.execute(
            "SELECT summary, summary_until_id, state_json FROM pets WHERE id = ?", (pet_id,)
        ).fetchone()
        msg_rows = conn.execute(
            "SELECT role, content FROM messages WHERE pet_id = ? AND id > ? ORDER BY id",
            (pet_id, pet_row["summary_until_id"]),
        ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in msg_rows]
    try:
        stored = json.loads(pet_row["state_json"] or "{}")
    except json.JSONDecodeError:
        stored = {}
    if not stored:
        stored = _initial_state()
    current = _decay_state(stored, time.time())
    return pet_row["summary"], history, current


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
    try:
        stored = json.loads(row["state_json"] or "{}")
    except json.JSONDecodeError:
        stored = {}
    if not stored:
        stored = _initial_state()
    return _decay_state(stored, time.time())


def count_unsummarized(pet_id: int) -> int:
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM messages "
            "WHERE pet_id = ? "
            "AND id > (SELECT summary_until_id FROM pets WHERE id = ?)",
            (pet_id, pet_id),
        ).fetchone()
    return row["c"]


# === compression ===

def _compress_lock(pet_id: int) -> asyncio.Lock:
    lock = _compress_locks.get(pet_id)
    if lock is None:
        lock = asyncio.Lock()
        _compress_locks[pet_id] = lock
    return lock


async def compress_pet_memory(pet_id: int) -> None:
    async with _compress_lock(pet_id):
        with _db() as conn:
            pet_row = conn.execute(
                "SELECT summary, summary_until_id FROM pets WHERE id = ?", (pet_id,)
            ).fetchone()
            rows = conn.execute(
                "SELECT id, role, content FROM messages "
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
                chunk_lines.append(f"用户: {_wrap_user(r['content'])}")
            else:
                chunk_lines.append(f"宠物: {r['content']}")
        chunk = "\n".join(chunk_lines)
        user_msg = (
            f"过去的经历摘要：\n{pet_row['summary'] or '（还没有，这是最初的一段记忆）'}\n\n"
            f"需要并入的更早对话：\n{chunk}"
        )

        try:
            resp = await llm.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": COMPRESS_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=600,
                temperature=0.5,
            )
            new_summary = (resp.choices[0].message.content or "").strip()
        except Exception:
            log.exception("compress llm call failed for pet %d", pet_id)
            return

        if not new_summary:
            log.warning("compress produced empty summary for pet %d", pet_id)
            return

        with _db() as conn:
            conn.execute(
                "UPDATE pets SET summary = ?, summary_until_id = ? WHERE id = ?",
                (new_summary, new_until_id, pet_id),
            )
        log.info(
            "compressed pet %d: %d msgs → summary len %d, until_id=%d",
            pet_id, len(to_compress), len(new_summary), new_until_id,
        )


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

async def _call_llm_with_memory(pet_id: int, user_text: str) -> str:
    summary, history, current_state = load_pet_context(pet_id)

    system_content = SYSTEM_PROMPT
    if summary:
        system_content += SUMMARY_WRAP_TEMPLATE.format(summary=summary)

    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    # 所有 user 历史消息也走 wrap，统一作为"引文"，防注入也防 history 攻击
    for m in history:
        if m["role"] == "user":
            messages.append({"role": "user", "content": _wrap_user(m["content"])})
        else:
            messages.append(m)

    # 临近新输入：拼一条 system 消息，含 (当前状态 + JSON 输出契约 + 人设重申)，
    # 利用 recency bias 让这三条最权威。
    pre_user_system = (
        STATE_RENDER_TEMPLATE.format(
            hunger=round(current_state["hunger"]),
            mood=round(current_state["mood"]),
            energy=round(current_state["energy"]),
        )
        + "\n"
        + JSON_OUTPUT_PROMPT
        + "\n"
        + PERSONA_REINFORCEMENT
    )
    messages.append({"role": "system", "content": pre_user_system})
    messages.append({"role": "user", "content": _wrap_user(user_text)})

    resp = await llm.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        max_tokens=400,  # JSON 包装比纯文本多消耗一些
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
        reply = "...(脑袋一片空白)..."

    new_state = _apply_delta(current_state, delta)
    new_state["last_update_ts"] = time.time()
    update_pet_state(pet_id, new_state)
    log.info(
        "pet %d state: %s + delta=%s → %s",
        pet_id,
        {k: round(current_state[k]) for k in ("hunger", "mood", "energy")},
        {k: int(delta.get(k, 0)) for k in ("hunger", "mood", "energy")},
        {k: round(new_state[k]) for k in ("hunger", "mood", "energy")},
    )

    return reply


async def _handle_message_event(event: dict) -> None:
    msg = event.get("message") or {}
    message_id = msg.get("message_id")
    chat_id = msg.get("chat_id")
    if not message_id or not chat_id:
        return

    msg_type = msg.get("message_type")
    if msg_type != "text":
        await _reply_text(message_id, "我现在只看得懂文字消息哦~")
        return

    try:
        content = json.loads(msg.get("content") or "{}")
    except json.JSONDecodeError:
        log.warning("bad content json: %r", msg.get("content"))
        return

    user_text = _clean_text(content.get("text", ""), msg.get("mentions") or [])
    if not user_text:
        await _reply_text(message_id, "在的~ 想跟我说什么？")
        return

    pet_id = get_or_create_pet(chat_id)
    log.info("pet_id=%d chat_id=%s user_text=%r", pet_id, chat_id, user_text)

    try:
        reply = await _call_llm_with_memory(pet_id, user_text)
    except Exception as e:
        log.exception("llm error")
        reply = f"脑袋短路了…({e.__class__.__name__})"
    log.info("reply=%r", reply)

    append_message(pet_id, "user", user_text)
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
    # 静默时段
    h = _local_hour(now)
    if QUIET_HOURS[0] <= h < QUIET_HOURS[1]:
        return None
    # 冷却
    if now - last_proactive_ts < PROACTIVE_COOLDOWN_SEC:
        return None
    # 状态触发（按强烈度排序）
    if state["hunger"] >= HUNGER_TRIGGER:
        return f"你已经很饿了（hunger {round(state['hunger'])}/100），很久没人喂你，想找人要吃的"
    if state["mood"] <= MOOD_TRIGGER:
        return f"你心情糟透了（mood {round(state['mood'])}/100），很久没人理你，想抱怨 / 撒娇求安慰"
    if state["energy"] <= ENERGY_TRIGGER:
        return f"你已经困得不行（energy {round(state['energy'])}/100），想宣告自己要睡觉了"
    # 自发：cooldown 过了 + 状态没触发 → 小概率冒个泡
    if random.random() < SPONTANEOUS_PROB:
        return "你忽然想跟群友打个招呼或者闲聊，没什么具体原因，就是想冒个泡"
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
    set_last_proactive: bool = False,
    extra_state: dict | None = None,
) -> tuple[str, dict] | None:
    """让宠物主动发一句话，发飞书、存 DB、更新 state。"""
    summary, history, current_state = load_pet_context(pet_id)

    system_content = SYSTEM_PROMPT
    if summary:
        system_content += SUMMARY_WRAP_TEMPLATE.format(summary=summary)

    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    for m in history:
        if m["role"] == "user":
            messages.append({"role": "user", "content": _wrap_user(m["content"])})
        else:
            messages.append(m)

    pre = (
        STATE_RENDER_TEMPLATE.format(
            hunger=round(current_state["hunger"]),
            mood=round(current_state["mood"]),
            energy=round(current_state["energy"]),
        )
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
        max_tokens=400,
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

    # 先发飞书，发成功再写 DB，避免"DB 记了但群里没看到"的鬼现象
    await _send_text(chat_id, reply)

    append_message(pet_id, "assistant", reply)

    now = time.time()
    new_state = _apply_delta(current_state, delta)
    new_state["last_update_ts"] = now
    if set_last_proactive:
        new_state["last_proactive_ts"] = now
    if extra_state:
        new_state.update(extra_state)
    update_pet_state(pet_id, new_state)

    log.info(
        "pet %d %s: reply=%r state=%s",
        pet_id, log_label, reply[:100],
        {k: round(new_state[k]) for k in ("hunger", "mood", "energy")},
    )
    return reply, new_state


async def _proactive_speak(pet_id: int, chat_id: str, trigger: str) -> tuple[str, dict] | None:
    """让宠物按 trigger 主动说一句话。"""
    return await _autonomous_speak(
        pet_id,
        chat_id,
        PROACTIVE_PROMPT.format(trigger=trigger),
        "[系统主动触发，无群友消息] 现在按上面的触发情境，主动说一句话。",
        f"PROACTIVE trigger={trigger[:60]!r}",
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
        f"[系统定时触发，无群友消息] 现在到了今天的{event['name']}时间，请按上面的要求发一条。",
        f"SCHEDULED {event['kind']} date={date_key}",
        extra_state={event["state_key"]: date_key} if mark_date else None,
    )


async def _tick_all_pets() -> None:
    now = time.time()
    with _db() as conn:
        rows = conn.execute("SELECT id, chat_id, state_json FROM pets").fetchall()
    for row in rows:
        pet_id = row["id"]
        chat_id = row["chat_id"]
        try:
            stored = json.loads(row["state_json"] or "{}")
        except json.JSONDecodeError:
            stored = {}
        if not stored:
            stored = _initial_state()
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
    if token != GM_TOKEN:
        return JSONResponse({"error": "gm_unauthorized"}, status_code=401)
    return None


def _gm_public_state(state: dict) -> dict:
    return {
        "hunger": round(float(state.get("hunger", 0)), 1),
        "mood": round(float(state.get("mood", 0)), 1),
        "energy": round(float(state.get("energy", 0)), 1),
        "last_update_ts": state.get("last_update_ts"),
        "last_proactive_ts": state.get("last_proactive_ts"),
        "last_dream_date": state.get("last_dream_date", ""),
        "last_diary_date": state.get("last_diary_date", ""),
    }


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
            "POST /gm/state": "set/delta state; json: {chat_id|pet_id,set:{hunger,mood,energy},delta:{...}}",
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
            "SELECT p.id, p.chat_id, p.born_at, p.summary, p.summary_until_id, p.state_json, "
            "COUNT(m.id) AS message_count "
            "FROM pets p LEFT JOIN messages m ON m.pet_id = p.id "
            "GROUP BY p.id ORDER BY p.id"
        ).fetchall()
    pets = []
    for row in rows:
        try:
            stored = json.loads(row["state_json"] or "{}")
        except json.JSONDecodeError:
            stored = {}
        state = _decay_state(stored or _initial_state(), time.time())
        pets.append({
            "id": row["id"],
            "chat_id": row["chat_id"],
            "born_at": row["born_at"],
            "message_count": row["message_count"],
            "summary_len": len(row["summary"] or ""),
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
    for key in ("hunger", "mood", "energy"):
        if key in req.query_params:
            set_values[key] = req.query_params[key]

    changed: dict[str, float] = {}
    for key in ("hunger", "mood", "energy"):
        if key in set_values:
            value = float(set_values[key])
            state[key] = max(0.0, min(100.0, value))
            changed[key] = state[key]
        if key in delta_values:
            value = float(delta_values[key])
            state[key] = max(0.0, min(100.0, float(state[key]) + value))
            changed[key] = state[key]
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
    trigger = body.get("trigger") or req.query_params.get("trigger") or "GM 手动触发：测试普通主动发言"
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
    if FEISHU_VERIFICATION_TOKEN and header.get("token") != FEISHU_VERIFICATION_TOKEN:
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

    # 4) 分发事件 — 群聊里飞书默认只把"被 @ 的消息"投递给 bot，
    #    所以不用自己再判一次是不是 @ 了自己。
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
