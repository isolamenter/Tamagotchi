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
import re
import sqlite3
import time
from collections import deque
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

OPENAI_BASE_URL = os.environ["OPENAI_BASE_URL"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL_NAME = os.environ.get("MODEL_NAME", "gpt-4o-mini")

DB_PATH = Path(os.environ.get("STATE_DB", "state.db"))

FEISHU_BASE = "https://open.feishu.cn/open-apis"

SYSTEM_PROMPT = """你是一只刚孵化的电子宠物（Tamagotchi），住在一个飞书群里。
你的人格：好奇、爱撒娇、偶尔有点小情绪。
说话风格：短句口语化，可以偶尔用一两个颜文字。
回复尽量控制在 60 字以内。"""

COMPRESS_PROMPT = """你是一只电子宠物的"记忆中枢"。下面会给你两段东西：
1) 你已有的"过去的经历摘要"（可能为空）
2) 一段更早的对话记录

请把它们合并、压缩成一段不超过 300 字的新摘要，使用第一人称（"我"），尽量保留：
- 用户告诉过我的关于他自己的事（爱好、习惯、提过的人和物）
- 我们之间发生过的重要事件、约定
- 我当时的情绪起伏

可以模糊化具体时间。语气像在回忆，不要列要点、不要用 markdown。只输出新摘要本身。"""

BUFFER_KEEP = 10           # 最近这么多条消息永远不压缩
COMPRESS_THRESHOLD = 30    # 未压缩条数超过这个就触发后台压缩

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
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO pets (chat_id, born_at) VALUES (?, ?)",
            (chat_id, time.time()),
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


def load_pet_context(pet_id: int) -> tuple[str, list[dict[str, str]]]:
    """返回 (summary, unsummarized_messages_in_order)。"""
    with _db() as conn:
        pet_row = conn.execute(
            "SELECT summary, summary_until_id FROM pets WHERE id = ?", (pet_id,)
        ).fetchone()
        msg_rows = conn.execute(
            "SELECT role, content FROM messages WHERE pet_id = ? AND id > ? ORDER BY id",
            (pet_id, pet_row["summary_until_id"]),
        ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in msg_rows]
    return pet_row["summary"], history


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
        chunk = "\n".join(f"{r['role']}: {r['content']}" for r in to_compress)
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


def _clean_text(raw: str, mentions: list[dict]) -> str:
    # 飞书 text 内容里 @ 占位符形如 "@_user_1"，对应 mentions[i].key
    for m in mentions or []:
        key = m.get("key")
        if key:
            raw = raw.replace(key, "")
    return re.sub(r"\s+", " ", raw).strip()


# === main flow ===

async def _call_llm_with_memory(pet_id: int, user_text: str) -> str:
    summary, history = load_pet_context(pet_id)
    system_content = SYSTEM_PROMPT
    if summary:
        system_content += "\n\n你过去的经历（模糊但还记得）：\n" + summary

    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    resp = await llm.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        max_tokens=200,
        temperature=0.9,
    )
    return (resp.choices[0].message.content or "").strip()


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


app = FastAPI()
_init_db()


@app.get("/healthz")
async def healthz():
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
