from __future__ import annotations

import json
import logging
import time
import uuid

from config import AppConfig
from domain.card import CardDomain
from domain.pet import PetDomain
from integrations.feishu_client import FeishuClient
from integrations.llm_client import LLMClient
from repositories.card_repo import CardRepository
from repositories.message_repo import MessageRepository
from repositories.pet_repo import PetRepository
from repositories.system_repo import SystemRepository
from runtime import RuntimeState
from services.memory_service import MemoryService
from services.style_service import StyleService

log = logging.getLogger("tamagotchi")


class CardService:
    def __init__(
        self,
        config: AppConfig,
        runtime: RuntimeState,
        card_domain: CardDomain,
        pet_domain: PetDomain,
        pet_repo: PetRepository,
        card_repo: CardRepository,
        message_repo: MessageRepository,
        system_repo: SystemRepository,
        memory_service: MemoryService,
        feishu: FeishuClient,
        llm: LLMClient,
        style_service: StyleService | None = None,
    ):
        self.config = config
        self.runtime = runtime
        self.card_domain = card_domain
        self.pet_domain = pet_domain
        self.pet_repo = pet_repo
        self.card_repo = card_repo
        self.message_repo = message_repo
        self.system_repo = system_repo
        self.memory_service = memory_service
        self.feishu = feishu
        self.llm = llm
        self.style_service = style_service

    async def resolve_user_name(self, open_id: str) -> str:
        if not open_id:
            return "群友"
        cached = await self.system_repo.get_cached_user_name(open_id)
        return cached or f"群友-{open_id[-4:]}"

    async def send_persistent_card(
        self,
        pet_id: int,
        chat_id: str,
        text: str,
        *,
        mode: str = "content",
        action_keys: list[str] | None = None,
        img_key: str | None = None,
        card_id: str | None = None,
        base_text: str | None = None,
    ) -> tuple[str, str]:
        now = time.time()
        card_id = card_id or f"card-{uuid.uuid4().hex}"
        keys = list(action_keys) if action_keys else []
        await self.card_repo.create_instance(
            card_id=card_id,
            pet_id=pet_id,
            mode=mode,
            need_id="",
            need_round=0,
            built_at=now,
            expires_at=now + 86400,
            max_settlements=1,
            base_text=base_text if base_text is not None else text,
            action_keys=keys,
            img_key=img_key,
        )
        card = self.card_domain.build_pet_card(
            pet_id,
            text,
            action_keys=keys,
            built_at=int(now),
            base_text=base_text if base_text is not None else text,
            img_key=img_key,
            card_id=card_id,
            mode=mode,
        )
        message_id = await self.feishu.send_card(chat_id, card)
        await self.card_repo.mark_announced(card_id, message_id)
        return card_id, message_id

    async def handle_card_action(self, event: dict, event_id: str | None) -> dict:
        action = event.get("action") or {}
        value = action.get("value") or {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}
        if not isinstance(value, dict) or int(value.get("v") or 0) != 2:
            return {"toast": {"type": "info", "content": "这张互动卡已更新，请等下一张吧~"}}
        card_id = (value.get("card_id") or "").strip()
        action_key = (value.get("action") or "").strip()
        operator = event.get("operator") or {}
        clicker_open_id = operator.get("open_id") or operator.get("union_id") or ""
        sender_name = await self.resolve_user_name(clicker_open_id)

        pet_id = int(value.get("pet_id") or 0)
        if pet_id and action_key:
            await self.message_repo.append_message(
                pet_id,
                "user",
                f"[{sender_name} 点击了: {action_key}]",
                sender_name=sender_name,
                sender_open_id=clicker_open_id,
                is_observer=False,
            )
        return {
            "toast": {"type": "success", "content": self.config.card_toast_done},
        }
