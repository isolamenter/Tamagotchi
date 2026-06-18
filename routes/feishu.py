from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("tamagotchi")
router = APIRouter()


@router.post("/feishu/webhook")
async def feishu_webhook(req: Request, background: BackgroundTasks):
    container = req.app.state.container
    try:
        raw = await req.json()
    except Exception:
        log.warning("malformed webhook body (not JSON)")
        return JSONResponse({"code": 400}, status_code=400)
    if not isinstance(raw, dict):
        log.warning("webhook body is not a JSON object: %r", type(raw).__name__)
        return JSONResponse({"code": 400}, status_code=400)

    if "encrypt" in raw:
        if not container.config.feishu_encrypt_key:
            log.error("received encrypted payload but FEISHU_ENCRYPT_KEY not set")
            return JSONResponse({"code": 400}, status_code=400)
        try:
            body = json.loads(container.feishu.decrypt(raw["encrypt"]))
        except Exception:
            log.exception("decrypt failed")
            return JSONResponse({"code": 400}, status_code=400)
    else:
        body = raw

    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}

    header = body.get("header") or {}

    if not hmac.compare_digest(
        header.get("token") or "", container.config.feishu_verification_token
    ):
        log.warning("verification token mismatch")
        return JSONResponse({"code": 401}, status_code=401)

    event_id = header.get("event_id")
    event_type = header.get("event_type")

    if event_type == "card.action.trigger":
        if not container.config.card_enabled:
            return {}
        return await container.services.card.handle_card_action(
            body.get("event") or {}, event_id
        )

    if event_id and await container.system_repo.check_and_register_event(event_id):
        return {"code": 0}

    if event_type == "im.message.receive_v1":
        background.add_task(
            container.services.reply.handle_message_event, body.get("event") or {}
        )

    return {"code": 0}

