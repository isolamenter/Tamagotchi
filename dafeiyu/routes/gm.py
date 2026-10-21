from __future__ import annotations

import hmac
import logging
import time
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, ConfigDict, Field

from dafeiyu import __version__

log = logging.getLogger("dafeiyu")


def require_gm(req: Request, x_gm_token: Annotated[str, Header()] = "") -> None:
    expected = req.app.state.container.config.gm_token
    if not expected:
        raise HTTPException(403, "管理功能未启用，请配置独立的 GM_TOKEN")
    if not hmac.compare_digest(x_gm_token.encode(), expected.encode()):
        raise HTTPException(401, "管理口令不正确")


router = APIRouter(prefix="/gm", dependencies=[Depends(require_gm)])


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: int = Field(gt=0, strict=True)


class SpeakRequest(Target):
    trigger: str = Field(default="", max_length=2000)


class ScheduledRequest(Target):
    mark: bool = False


class TextRequest(Target):
    text: str = Field(min_length=1, max_length=10000)


class NameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=80)


async def target(req: Request, conversation_id: int) -> tuple[int, str]:
    row, _ = await req.app.state.container.conversation_repo.resolve_conversation(conversation_id)
    if row is None:
        raise HTTPException(404, "会话不存在，请刷新会话列表")
    return row["id"], row["chat_id"]


@router.get("/overview")
async def overview(req: Request):
    c = req.app.state.container
    return {
        "version": __version__,
        "name": c.config.character_name,
        "icon": c.config.character_icon,
        "style_ready": c.services.style.ready,
        "style_failed": c.services.style.initialization_error is not None,
        "memory_index": await c.memory_repo.embedding_status(),
        "scheduled_events": [
            {"kind": e["kind"], "name": e["name"], "hour": e["hour"]}
            for e in c.config.scheduled_events
        ],
        "quiet_hours": c.config.quiet_hours,
        "timezone_offset_hours": c.config.proactive_tz_offset_hours,
        "image_generation_enabled": bool(c.config.image_model),
    }


@router.get("/help")
async def help_info():
    return {
        "version": __version__,
        "auth": "X-GM-Token header",
        "target": "conversation_id",
        "read": ["overview", "conversations", "state", "messages", "memories"],
        "actions": ["text", "image", "speak", "dream", "diary", "tick", "tick-all"],
    }


@router.get("/conversations")
async def conversations(req: Request):
    return {"conversations": await req.app.state.container.conversation_repo.get_gm_conversations()}


@router.patch("/conversations/{conversation_id}")
async def rename_conversation(req: Request, conversation_id: int, body: NameRequest):
    await target(req, conversation_id)
    await req.app.state.container.conversation_repo.rename(conversation_id, body.title.strip())
    return {"ok": True, "message": "会话名称已保存"}


@router.get("/state")
async def state(req: Request, conversation_id: int = Query(gt=0)):
    await target(req, conversation_id)
    return {
        "conversation_id": conversation_id,
        "state": await req.app.state.container.conversation_repo.load_conversation_state(
            conversation_id
        ),
    }


@router.get("/messages")
async def messages(
    req: Request,
    conversation_id: int = Query(gt=0),
    limit: int = Query(default=60, ge=1, le=200),
    before_id: int | None = Query(default=None, gt=0),
):
    await target(req, conversation_id)
    rows = await req.app.state.container.message_repo.recent_messages(
        conversation_id, limit, before_id=before_id
    )
    return {"messages": rows, "has_more": len(rows) == limit}


@router.get("/memories")
async def memories(
    req: Request,
    conversation_id: int = Query(gt=0),
    limit: int = Query(default=50, ge=1, le=200),
    before_id: int | None = Query(default=None, gt=0),
):
    await target(req, conversation_id)
    rows = await req.app.state.container.memory_repo.list_cards(
        conversation_id, limit, before_id=before_id
    )
    return {"memories": rows, "has_more": len(rows) == limit}


@router.post("/text")
async def send_text(req: Request, body: TextRequest):
    c = req.app.state.container
    conversation_id, chat_id = await target(req, body.conversation_id)
    if not body.text.strip():
        raise HTTPException(422, "请输入要发送的文字")
    async with c.runtime.chat_lock(conversation_id):
        await c.services.delivery.send(conversation_id, chat_id, body.text.strip(), source="manual")
    return {"ok": True, "message": "文字已发送"}


@router.post("/image")
async def send_image(
    req: Request,
    conversation_id: Annotated[int, Form(gt=0)],
    file: Annotated[UploadFile, File()],
):
    c = req.app.state.container
    conversation_id, chat_id = await target(req, conversation_id)
    if file.content_type not in {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
    }:
        raise HTTPException(422, "请选择 PNG、JPEG、GIF、WebP 或 BMP 图片")
    content = await file.read(10 * 1024 * 1024 + 1)
    if not content or len(content) > 10 * 1024 * 1024:
        raise HTTPException(422, "图片不能为空且不能超过 10 MB")
    async with c.runtime.chat_lock(conversation_id):
        await c.services.delivery.send_image(conversation_id, chat_id, content)
    return {"ok": True, "message": "图片已发送"}


@router.post("/speak")
async def speak(req: Request, body: SpeakRequest):
    c = req.app.state.container
    conversation_id, chat_id = await target(req, body.conversation_id)
    reply, _ = await c.services.autonomous.proactive_speak(
        conversation_id,
        chat_id,
        body.trigger.strip() or c.config.gm_default_speak_trigger,
    )
    return {"ok": True, "message": "主动发言已发送", "reply": reply}


async def scheduled(req: Request, body: ScheduledRequest, kind: str):
    c = req.app.state.container
    conversation_id, chat_id = await target(req, body.conversation_id)
    event = next((e for e in c.config.scheduled_events if e["kind"] == kind), None)
    if event is None:
        raise HTTPException(404, "没有配置这项定时内容")
    date, _ = c.state_domain.local_date_hour(time.time())
    reply, _ = await c.services.autonomous.scheduled_speak(
        conversation_id, chat_id, event, date, mark_date=body.mark
    )
    return {
        "ok": True,
        "message": event["name"] + "已发送",
        "reply": reply,
        "marked_date": date if body.mark else None,
    }


@router.post("/dream")
async def dream(req: Request, body: ScheduledRequest):
    return await scheduled(req, body, "dream")


@router.post("/diary")
async def diary(req: Request, body: ScheduledRequest):
    return await scheduled(req, body, "diary")


@router.post("/tick")
async def tick(req: Request, body: Target):
    conversation_id, chat_id = await target(req, body.conversation_id)
    spoke = await req.app.state.container.services.autonomous.tick_conversation(
        conversation_id, chat_id
    )
    return {
        "ok": True,
        "spoke": spoke,
        "message": "已产生一条发言" if spoke else "检查完成，当前无需发言",
    }


@router.post("/tick-all")
async def tick_all(req: Request):
    await req.app.state.container.services.autonomous.tick_all_conversations()
    return {"ok": True, "message": "所有会话检查完成"}
