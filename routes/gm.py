from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


def _container(req: Request):
    return req.app.state.container


def _gm_auth(req: Request) -> JSONResponse | None:
    container = _container(req)
    if not container.config.gm_token:
        return JSONResponse({"error": "gm_disabled"}, status_code=403)
    token = req.query_params.get("token") or req.headers.get("X-GM-Token")
    if not hmac.compare_digest(token or "", container.config.gm_token):
        return JSONResponse({"error": "gm_unauthorized"}, status_code=401)
    return None


def _gm_event(req: Request, kind: str) -> dict | None:
    for event in _container(req).config.scheduled_events:
        if event["kind"] == kind:
            return event
    return None


def _gm_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


async def _gm_resolve_pet(
    req: Request, chat_id: str | None = None, pet_id: int | str | None = None
) -> tuple[int, str] | JSONResponse:
    container = _container(req)
    if chat_id:
        created_id = await container.pet_repo.get_or_create_pet(chat_id)
        return created_id, chat_id

    if pet_id is not None and not isinstance(pet_id, bool):
        try:
            pet_id = int(str(pet_id).strip())
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "invalid_pet_id", "pet_id": pet_id}, status_code=400
            )

    row_dict, rows = await container.pet_repo.resolve_pet(pet_id)
    if pet_id is not None:
        if row_dict is None:
            return JSONResponse({"error": "pet_not_found", "pet_id": pet_id}, status_code=404)
        return row_dict["id"], row_dict["chat_id"]
    if len(rows) != 1:
        return JSONResponse(
            {
                "error": "target_required",
                "hint": "pass chat_id or pet_id; if no pet exists, pass chat_id to create one",
                "pets": [{"id": row["id"], "chat_id": row["chat_id"]} for row in rows],
            },
            status_code=400,
        )
    return rows[0]["id"], rows[0]["chat_id"]


async def _gm_body(req: Request) -> dict:
    if not (req.headers.get("content-type") or "").startswith("application/json"):
        return {}
    try:
        data = await req.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


@router.get("/gm/help")
async def gm_help(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    return {
        "auth": "pass ?token=... or X-GM-Token; token is GM_TOKEN, fallback FEISHU_VERIFICATION_TOKEN",
        "endpoints": {
            "GET /gm/pets": "list pets",
            "GET /gm/state": "read state; query: chat_id or pet_id",
            "POST /gm/speak": "force proactive speech; json: {chat_id|pet_id, trigger?}",
            "POST /gm/dream": "force dream; json: {chat_id|pet_id, mark?}",
            "POST /gm/diary": "force diary; json: {chat_id|pet_id, mark?}",
            "POST /gm/tick": "run one autonomous tick; json: {chat_id|pet_id?} — omit for all pets",
            "GET /gm/cards": "list memory cards; query: chat_id or pet_id, limit?",
            "GET /gm/messages": "list recent messages; query: chat_id or pet_id, limit?",
            "POST /gm/image": "send image; multipart form: file=<image> + chat_id|pet_id; query ?token=...",
            "POST /gm/text": "send text; json: {chat_id|pet_id, text}",
            "GET /web": "html dashboard; open /web?token=...",
        },
    }


@router.get("/gm/pets")
async def gm_pets(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    container = _container(req)
    rows = await container.pet_repo.get_gm_pets()
    pets = []
    for row in rows:
        state = await container.pet_repo.load_pet_state(row["id"])
        pets.append(
            {
                "id": row["id"],
                "chat_id": row["chat_id"],
                "born_at": row["born_at"],
                "message_count": row["message_count"],
                "card_count": row["card_count"],
                "summary_until_id": row["summary_until_id"],
                "state": container.state_domain.public_state(state),
            }
        )
    return {
        "pets": pets,
    }


@router.get("/gm/cards")
async def gm_cards(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    pet_id_param = req.query_params.get("pet_id")
    resolved = await _gm_resolve_pet(
        req,
        chat_id=req.query_params.get("chat_id"),
        pet_id=pet_id_param or None,
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved
    limit_param = req.query_params.get("limit")
    try:
        limit = max(1, min(200, int(limit_param))) if limit_param else 50
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid_limit", "limit": limit_param}, status_code=400)
    cards = await _container(req).memory_repo.list_cards(pet_id, limit)
    return {"pet_id": pet_id, "chat_id": chat_id, "cards": cards}


@router.get("/gm/messages")
async def gm_messages(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    pet_id_param = req.query_params.get("pet_id")
    resolved = await _gm_resolve_pet(
        req,
        chat_id=req.query_params.get("chat_id"),
        pet_id=pet_id_param or None,
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved
    limit_param = req.query_params.get("limit")
    try:
        limit = max(1, min(200, int(limit_param))) if limit_param else 50
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid_limit", "limit": limit_param}, status_code=400)
    messages = await _container(req).message_repo.recent_messages(pet_id, limit)
    return {"pet_id": pet_id, "chat_id": chat_id, "messages": messages}


@router.get("/gm/state")
async def gm_get_state(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    pet_id_param = req.query_params.get("pet_id")
    resolved = await _gm_resolve_pet(
        req,
        chat_id=req.query_params.get("chat_id"),
        pet_id=pet_id_param or None,
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved
    container = _container(req)
    state = await container.pet_repo.load_pet_state(pet_id)
    return {
        "pet_id": pet_id,
        "chat_id": chat_id,
        "state": container.state_domain.public_state(state),
    }


@router.post("/gm/speak")
async def gm_speak(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    container = _container(req)
    body = await _gm_body(req)
    pet_id_param = body.get("pet_id") or req.query_params.get("pet_id")
    resolved = await _gm_resolve_pet(
        req,
        chat_id=body.get("chat_id") or req.query_params.get("chat_id"),
        pet_id=pet_id_param or None,
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved
    trigger = (
        body.get("trigger")
        or req.query_params.get("trigger")
        or container.config.gm_default_speak_trigger
    )
    result = await container.services.autonomous.proactive_speak(pet_id, chat_id, trigger)
    if result is None:
        return JSONResponse({"error": "llm_empty_or_invalid"}, status_code=502)
    reply, state = result
    return {
        "ok": True,
        "pet_id": pet_id,
        "chat_id": chat_id,
        "reply": reply,
        "state": container.state_domain.public_state(state),
    }


async def _gm_scheduled(req: Request, kind: str):
    auth = _gm_auth(req)
    if auth:
        return auth
    event = _gm_event(req, kind)
    if event is None:
        return JSONResponse({"error": "unknown_event", "kind": kind}, status_code=404)
    container = _container(req)
    body = await _gm_body(req)
    pet_id_param = body.get("pet_id") or req.query_params.get("pet_id")
    resolved = await _gm_resolve_pet(
        req,
        chat_id=body.get("chat_id") or req.query_params.get("chat_id"),
        pet_id=pet_id_param or None,
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved
    date_key, _ = container.state_domain.local_date_hour(time.time())
    mark = _gm_bool(body.get("mark", req.query_params.get("mark")), default=False)
    result = await container.services.autonomous.scheduled_speak(
        pet_id, chat_id, event, date_key, mark_date=mark
    )
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
        "state": container.state_domain.public_state(state),
    }


@router.post("/gm/dream")
async def gm_dream(req: Request):
    return await _gm_scheduled(req, "dream")


@router.post("/gm/diary")
async def gm_diary(req: Request):
    return await _gm_scheduled(req, "diary")


@router.post("/gm/image")
async def gm_image(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    try:
        form = await req.form()
    except Exception:
        return JSONResponse({"error": "invalid_form", "hint": "use multipart/form-data with field 'file'"}, status_code=400)
    file = form.get("file") or form.get("image")
    if file is None:
        return JSONResponse({"error": "missing_file", "hint": "multipart field 'file' is required"}, status_code=400)
    filename = getattr(file, "filename", None) or "image"
    content_type = (getattr(file, "content_type", None) or "").lower()
    is_image_ct = content_type.startswith("image/")
    is_image_ext = filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif"))
    if not is_image_ct and not is_image_ext:
        return JSONResponse({"error": "not_an_image", "content_type": content_type, "filename": filename}, status_code=400)
    try:
        image_bytes: bytes = await file.read()  # type: ignore[union-attr]
    except Exception:
        return JSONResponse({"error": "read_failed"}, status_code=400)
    if not image_bytes:
        return JSONResponse({"error": "empty_file"}, status_code=400)
    if len(image_bytes) > 10 * 1024 * 1024:
        return JSONResponse({"error": "file_too_large", "size": len(image_bytes), "limit": 10 * 1024 * 1024}, status_code=400)
    pet_id_param = form.get("pet_id") or req.query_params.get("pet_id")
    chat_id_param = form.get("chat_id") or req.query_params.get("chat_id")
    if pet_id_param is not None and not isinstance(pet_id_param, str):
        pet_id_param = str(pet_id_param)
    if chat_id_param is not None and not isinstance(chat_id_param, str):
        chat_id_param = str(chat_id_param)
    resolved = await _gm_resolve_pet(req, chat_id=chat_id_param or None, pet_id=pet_id_param or None)
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved
    container = _container(req)
    image_key = await container.feishu.upload_image(image_bytes)
    if not image_key:
        return JSONResponse({"error": "feishu_upload_failed", "hint": "check im:resource:upload permission and re-publish app"}, status_code=502)
    try:
        message_id = await container.feishu.send_image(chat_id, image_key)
    except RuntimeError as exc:
        return JSONResponse({"error": "feishu_send_failed", "detail": str(exc)[:500]}, status_code=502)
    return {"ok": True, "pet_id": pet_id, "chat_id": chat_id, "image_key": image_key, "message_id": message_id}


@router.post("/gm/text")
async def gm_text(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    data = await _gm_body(req)
    text = (data.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "empty_text"}, status_code=400)
    resolved = await _gm_resolve_pet(
        req, chat_id=data.get("chat_id") or None, pet_id=data.get("pet_id") or None
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved
    container = _container(req)
    try:
        await container.feishu.send_text(chat_id, text)
    except RuntimeError as exc:
        return JSONResponse({"error": "feishu_send_failed", "detail": str(exc)[:500]}, status_code=502)
    return {"ok": True, "pet_id": pet_id, "chat_id": chat_id}


@router.post("/gm/tick")
async def gm_tick(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    container = _container(req)
    body = await _gm_body(req)
    pet_id_param = body.get("pet_id") or req.query_params.get("pet_id")
    chat_id_param = body.get("chat_id") or req.query_params.get("chat_id")
    if not pet_id_param and not chat_id_param:
        await container.services.autonomous.tick_all_pets()
        return {"ok": True, "scope": "all"}
    resolved = await _gm_resolve_pet(
        req,
        chat_id=chat_id_param,
        pet_id=pet_id_param or None,
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved
    spoke = await container.services.autonomous.tick_pet(pet_id, chat_id)
    return {"ok": True, "scope": "pet", "pet_id": pet_id, "chat_id": chat_id, "spoke": spoke}
