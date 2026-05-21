from __future__ import annotations

import hmac
import random
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
    req: Request, chat_id: str | None = None, pet_id: int | None = None
) -> tuple[int, str] | JSONResponse:
    container = _container(req)
    if chat_id:
        created_id = await container.pet_repo.get_or_create_pet(chat_id)
        return created_id, chat_id

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
            "POST /gm/state": "set/delta state; json: {chat_id|pet_id, set:{hunger,mood,energy,curiosity,affection}, delta:{...}, recent_vibe?: '...' | 'random'}",
            "POST /gm/speak": "force proactive speech; json: {chat_id|pet_id,trigger?}",
            "POST /gm/dream": "force dream; json: {chat_id|pet_id,mark?}",
            "POST /gm/diary": "force diary; json: {chat_id|pet_id,mark?}",
            "POST /gm/tick": "run one autonomous tick for all pets",
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
        state = container.state_domain.decay_state(
            container.pet_repo.decode_state(row["state_json"]), time.time(), row["id"]
        )
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
    return {"pets": pets}


@router.get("/gm/state")
async def gm_get_state(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    pet_id_param = req.query_params.get("pet_id")
    resolved = await _gm_resolve_pet(
        req,
        chat_id=req.query_params.get("chat_id"),
        pet_id=int(pet_id_param) if pet_id_param else None,
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


@router.post("/gm/state")
async def gm_set_state(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    container = _container(req)
    body = await _gm_body(req)
    pet_id_param = body.get("pet_id") or req.query_params.get("pet_id")
    resolved = await _gm_resolve_pet(
        req,
        chat_id=body.get("chat_id") or req.query_params.get("chat_id"),
        pet_id=int(pet_id_param) if pet_id_param else None,
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved

    state = await container.pet_repo.load_pet_state(pet_id)
    set_values = body.get("set") if isinstance(body.get("set"), dict) else {}
    delta_values = body.get("delta") if isinstance(body.get("delta"), dict) else {}
    for key in container.config.state_numeric_keys:
        if key in req.query_params:
            set_values[key] = req.query_params[key]

    changed: dict = {}
    for key in container.config.state_numeric_keys:
        if key in set_values:
            value = float(set_values[key])
            state[key] = max(0.0, min(100.0, value))
            changed[key] = state[key]
        if key in delta_values:
            value = float(delta_values[key])
            state[key] = max(0.0, min(100.0, float(state[key]) + value))
            changed[key] = state[key]

    rv_payload = set_values.get("recent_vibe")
    if rv_payload is None:
        rv_payload = body.get("recent_vibe")
    if rv_payload is not None:
        rv = str(rv_payload).strip()
        if rv.lower() == "random" and container.config.recent_vibe_pool:
            rv = random.choice(container.config.recent_vibe_pool)
        state["recent_vibe"] = rv
        state["recent_vibe_date"] = ""
        changed["recent_vibe"] = rv
    state["last_update_ts"] = time.time()
    await container.pet_repo.update_pet_state(pet_id, state)
    return {
        "ok": True,
        "pet_id": pet_id,
        "chat_id": chat_id,
        "changed": changed,
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
        pet_id=int(pet_id_param) if pet_id_param else None,
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
        pet_id=int(pet_id_param) if pet_id_param else None,
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


@router.post("/gm/tick")
async def gm_tick(req: Request):
    auth = _gm_auth(req)
    if auth:
        return auth
    await _container(req).services.autonomous.tick_all_pets()
    return {"ok": True}

