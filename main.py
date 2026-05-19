"""LLM Tamagotchi — minimal Feishu bot.

收到群里 @ 机器人的消息后调用 LLM 并回复。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from collections import deque

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

FEISHU_BASE = "https://open.feishu.cn/open-apis"

SYSTEM_PROMPT = """你是一只刚孵化的电子宠物（Tamagotchi），住在一个飞书群里。
你的人格：好奇、爱撒娇、偶尔有点小情绪。
说话风格：短句口语化，可以偶尔用一两个颜文字。
回复尽量控制在 60 字以内。"""

# === clients ===
http = httpx.AsyncClient(timeout=30.0)
llm = AsyncOpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)

# tenant access token cache
_token_cache: dict[str, float | str | None] = {"token": None, "expires_at": 0.0}

# event dedup（单进程 demo，重启会丢，但够用）
_seen_events: deque[str] = deque(maxlen=1000)
_seen_set: set[str] = set()


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


async def _call_llm(user_text: str) -> str:
    resp = await llm.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        max_tokens=200,
        temperature=0.9,
    )
    return (resp.choices[0].message.content or "").strip()


def _clean_text(raw: str, mentions: list[dict]) -> str:
    # 飞书 text 内容里 @ 占位符形如 "@_user_1"，对应 mentions[i].key
    for m in mentions or []:
        key = m.get("key")
        if key:
            raw = raw.replace(key, "")
    return re.sub(r"\s+", " ", raw).strip()


async def _handle_message_event(event: dict) -> None:
    msg = event.get("message") or {}
    message_id = msg.get("message_id")
    if not message_id:
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

    log.info("user_text=%r", user_text)
    try:
        reply = await _call_llm(user_text)
    except Exception as e:
        log.exception("llm error")
        reply = f"脑袋短路了…({e.__class__.__name__})"
    log.info("reply=%r", reply)
    await _reply_text(message_id, reply)


app = FastAPI()


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
