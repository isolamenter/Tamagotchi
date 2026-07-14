from __future__ import annotations

import hmac
import random
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from domain.gameplay import NEED_SPECS

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
            "POST /gm/state": "set/delta state; json: {chat_id|pet_id, set:{satiety,mood,energy,curiosity,affection}, delta:{...}, recent_vibe?: '...' | 'random'}",
            "POST /gm/speak": "force proactive speech; json: {chat_id|pet_id,trigger?}",
            "POST /gm/dream": "force dream; json: {chat_id|pet_id,mark?}",
            "POST /gm/diary": "force diary; json: {chat_id|pet_id,mark?}",
            "POST /gm/tick": "run one autonomous tick; json: {chat_id|pet_id?} — omit for all pets",
            "GET /gm/gameplay": "read active need; query: chat_id or pet_id",
            "POST /gm/need": "create/clear active need; json: {chat_id|pet_id, kind?, clear?}",
            "POST /gm/resolve_need": "resolve active need; json: {chat_id|pet_id, action, actor?}",
            "GET /gm/cards": "list memory cards; query: chat_id or pet_id, limit?",
            "GET /gm/messages": "list recent messages; query: chat_id or pet_id, limit?",
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
        state = await container.services.gameplay.ensure_pet_gameplay(row["id"])
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
        "numeric_keys": list(container.config.state_numeric_keys),
        "bar_labels": dict(container.config.card_bar_labels),
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
    state = await container.services.gameplay.ensure_pet_gameplay(pet_id)
    return {
        "pet_id": pet_id,
        "chat_id": chat_id,
        "state": container.state_domain.public_state(state),
    }


@router.get("/gm/gameplay")
async def gm_gameplay(req: Request):
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
    state = await container.services.gameplay.ensure_pet_gameplay(pet_id)
    public = container.state_domain.public_state(state)
    return {
        "pet_id": pet_id,
        "chat_id": chat_id,
        "active_need": public["active_need"],
    }


@router.post("/gm/need")
async def gm_need(req: Request):
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
    now = time.time()
    clear = _gm_bool(body.get("clear", req.query_params.get("clear")), default=False)
    kind = (body.get("kind") or req.query_params.get("kind") or "").strip()
    if kind and kind not in NEED_SPECS:
        return JSONResponse({"error": "unknown_need_kind", "kind": kind}, status_code=400)

    created = None

    def _mutator(state: dict) -> dict:
        nonlocal created
        if clear:
            state["active_need"] = {}
            return state
        if kind:
            created = container.gameplay_domain.build_need(
                kind, int(body.get("severity", 1) or 1), now
            )
            state["active_need"] = created
            return state
        state, created = container.gameplay_domain.maybe_create_need(state, now, pet_id)
        return state

    state = await container.pet_repo.mutate_state(pet_id, _mutator)
    return {
        "ok": True,
        "pet_id": pet_id,
        "chat_id": chat_id,
        "created": created,
        "state": container.state_domain.public_state(state),
    }


@router.post("/gm/resolve_need")
async def gm_resolve_need(req: Request):
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
    action = (body.get("action") or req.query_params.get("action") or "").strip()
    actor = (body.get("actor") or req.query_params.get("actor") or "GM").strip()
    now = time.time()

    async with container.runtime.state_lock(pet_id):
        state = await container.pet_repo.load_pet_state(pet_id)
        need = container.gameplay_domain.current_need(state, now)
        if not need:
            return JSONResponse({"error": "no_active_need"}, status_code=400)
        if not action:
            choices = container.gameplay_domain.choice_keys_for_need(need["kind"])
            action = choices[0] if choices else ""
        try:
            result = container.services.gameplay.resolve_need_choice_state(
                state, action, actor, now
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc), "action": action}, status_code=400)
        await container.pet_repo.update_pet_state(pet_id, result.state)

    return {
        "ok": True,
        "pet_id": pet_id,
        "chat_id": chat_id,
        "action": action,
        "delta": result.delta,
        "state": container.state_domain.public_state(result.state),
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
        pet_id=pet_id_param or None,
    )
    if isinstance(resolved, JSONResponse):
        return resolved
    pet_id, chat_id = resolved

    set_values = body.get("set") if isinstance(body.get("set"), dict) else {}
    delta_values = body.get("delta") if isinstance(body.get("delta"), dict) else {}
    for key in container.config.state_numeric_keys:
        if key in req.query_params:
            set_values[key] = req.query_params[key]
    rv_payload = set_values.get("recent_vibe")
    if rv_payload is None:
        rv_payload = body.get("recent_vibe")

    changed: dict = {}

    def _mutator(state: dict) -> dict:
        for key in container.config.state_numeric_keys:
            if key in set_values:
                state[key] = max(0.0, min(100.0, float(set_values[key])))
                changed[key] = state[key]
            if key in delta_values:
                state[key] = max(0.0, min(100.0, float(state[key]) + float(delta_values[key])))
                changed[key] = state[key]
        if rv_payload is not None:
            rv = str(rv_payload).strip()
            if rv.lower() == "random" and container.config.recent_vibe_pool:
                rv = random.choice(container.config.recent_vibe_pool)
            date_key, _ = container.state_domain.local_date_hour(time.time())
            state["recent_vibe"] = rv
            state["recent_vibe_date"] = date_key
            changed["recent_vibe"] = rv
        state["last_update_ts"] = time.time()
        return state

    state = await container.pet_repo.mutate_state(pet_id, _mutator)
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
