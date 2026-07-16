from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from config import AppConfig
from domain.card import CardDomain
from domain.gameplay import GameplayDomain
from domain.pet import PetDomain
from domain.state import StateDomain
from integrations.feishu_client import FeishuClient
from integrations.llm_client import LLMClient
from repositories.card_repo import CardRepository
from repositories.message_repo import MessageRepository
from repositories.pet_repo import PetRepository
from repositories.system_repo import SystemRepository
from runtime import RuntimeState
from services.gameplay_service import GameplayService
from services.memory_service import MemoryService

log = logging.getLogger("tamagotchi")


class CardService:
    def __init__(
        self,
        config: AppConfig,
        runtime: RuntimeState,
        state_domain: StateDomain,
        gameplay_domain: GameplayDomain,
        card_domain: CardDomain,
        pet_domain: PetDomain,
        pet_repo: PetRepository,
        card_repo: CardRepository,
        message_repo: MessageRepository,
        system_repo: SystemRepository,
        gameplay_service: GameplayService,
        memory_service: MemoryService,
        feishu: FeishuClient,
        llm: LLMClient,
    ):
        self.config = config
        self.runtime = runtime
        self.state_domain = state_domain
        self.gameplay_domain = gameplay_domain
        self.card_domain = card_domain
        self.pet_domain = pet_domain
        self.pet_repo = pet_repo
        self.card_repo = card_repo
        self.message_repo = message_repo
        self.system_repo = system_repo
        self.gameplay_service = gameplay_service
        self.memory_service = memory_service
        self.feishu = feishu
        self.llm = llm

    async def resolve_user_name(self, open_id: str) -> str:
        if not open_id:
            return "群友"
        cached = await self.system_repo.get_cached_user_name(open_id)
        return cached or f"群友-{open_id[-4:]}"

    def _instance_max_settlements(self, mode: str) -> int:
        return self.config.card_scheduled_max_settlements if mode == "scheduled" else 1

    def _actions_for(self, state: dict, mode: str, action_keys: list[str] | None) -> list[str]:
        if action_keys is not None:
            return list(action_keys)
        if mode == "need":
            need = self.gameplay_domain.current_need(state, time.time())
            return self.gameplay_domain.choice_keys_for_need(need.get("kind", "")) if need else []
        return self.card_domain.pick_card_actions(state)

    async def send_persistent_card(
        self,
        pet_id: int,
        chat_id: str,
        text: str,
        state: dict,
        *,
        mode: str,
        action_keys: list[str] | None = None,
        img_key: str | None = None,
        card_id: str | None = None,
        base_text: str | None = None,
    ) -> tuple[str, str]:
        """Persist the callback contract before sending; mark it announced only on success."""
        now = time.time()
        card_id = card_id or f"card-{uuid.uuid4().hex}"
        need = self.gameplay_domain.current_need(state, now) if mode == "need" else {}
        keys = self._actions_for(state, mode, action_keys)
        await self.card_repo.create_instance(
            card_id=card_id,
            pet_id=pet_id,
            mode=mode,
            need_id=need.get("id", ""),
            need_round=int(need.get("round", 0) or 0),
            built_at=now,
            expires_at=now + self.config.card_button_ttl_sec,
            max_settlements=self._instance_max_settlements(mode),
            base_text=base_text if base_text is not None else text,
            action_keys=keys,
            img_key=img_key,
        )
        card = self.card_domain.build_pet_card(
            pet_id,
            text,
            state,
            action_keys=keys,
            built_at=int(now),
            base_text=base_text if base_text is not None else text,
            img_key=img_key,
            card_id=card_id,
            mode=mode,
            need_round=int(need.get("round", 0) or 0),
            expires_at=now + self.config.card_button_ttl_sec,
        )
        message_id = await self.feishu.send_card(chat_id, card)
        await self.card_repo.mark_announced(card_id, message_id)
        return card_id, message_id

    def _build_from_instance(
        self,
        instance: dict,
        state: dict,
        text: str,
        *,
        disable_actions: bool = False,
    ) -> dict:
        keys = [] if disable_actions else list(instance.get("action_keys_json") or [])
        return self.card_domain.build_pet_card(
            int(instance["pet_id"]),
            text,
            state,
            action_keys=keys,
            built_at=int(instance["built_at"]),
            base_text=instance.get("base_text") or text,
            img_key=instance.get("img_key") or None,
            card_id=instance["card_id"],
            mode=instance["mode"],
            need_round=int(instance.get("need_round", 0) or 0),
            expires_at=float(instance.get("expires_at", 0) or 0),
        )

    async def _reaction(
        self,
        pet_id: int,
        action_key: str,
        sender_name: str,
        did_text: str,
        pending_text: str,
        clicker_open_id: str,
    ) -> str:
        await self.message_repo.append_message(
            pet_id,
            "user",
            did_text,
            sender_name=sender_name,
            sender_open_id=clicker_open_id,
            is_observer=False,
        )
        try:
            history, state = await self.pet_repo.load_pet_context(pet_id)
            messages = self.pet_domain.base_messages(self.config.system_prompt, history)
            messages.append(
                {
                    "role": "system",
                    "content": self.state_domain.render_state(state)
                    + "\n"
                    + self.config.persona_reinforcement,
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": self.config.card_action_reply_prompt.format(
                        sender_name=sender_name, did=did_text
                    ),
                }
            )
            reaction = await self.llm.chat_text(
                messages,
                max_tokens=self.config.card_reply_max_tokens,
                temperature=0.9,
            )
        except Exception:
            log.exception("card action LLM reply failed")
            reaction = ""
        reaction = reaction or pending_text
        await self.message_repo.append_message(pet_id, "assistant", reaction)
        return reaction

    async def _append_reaction_and_patch(
        self,
        card_id: str,
        message_id: str,
        action_key: str,
        sender_name: str,
        clicker_open_id: str,
        did_text: str,
        pending_text: str,
    ) -> None:
        instance = await self.card_repo.get_instance(card_id)
        if not instance:
            return
        reaction = await self._reaction(
            int(instance["pet_id"]),
            action_key,
            sender_name,
            did_text,
            pending_text,
            clicker_open_id,
        )
        async with self.runtime.card_update_lock(message_id):
            instance = await self.card_repo.append_feedback(
                card_id, reaction, self.config.card_log_max_lines
            )
            if not instance:
                return
            state = await self.pet_repo.load_pet_state(int(instance["pet_id"]))
            lines = list(instance.get("feedback_lines_json") or [])
            text = self.card_domain.compose_card_text(instance.get("base_text") or "", lines)
            disable = int(instance.get("settlement_count", 0)) >= int(
                instance.get("max_settlements", 1)
            )
            await self.feishu.update_card_message(
                message_id, self._build_from_instance(instance, state, text, disable_actions=disable)
            )
        if await self.message_repo.count_unsummarized(int(instance["pet_id"])) > self.config.compress_threshold:
            asyncio.create_task(self.memory_service.compress_pet_memory(int(instance["pet_id"])))

    async def handle_card_action(self, event: dict, event_id: str | None) -> dict:
        action = event.get("action") or {}
        value = action.get("value") or {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}
        if not isinstance(value, dict):
            return {"toast": {"type": "info", "content": "这张互动卡已更新，请等下一张吧~"}}
        card_id = (value.get("card_id") or "").strip()
        mode = (value.get("mode") or "").strip()
        action_key = (value.get("action") or "").strip()
        if int(value.get("v") or 0) != 2 or not card_id or mode not in {"need", "free", "scheduled"}:
            return {"toast": {"type": "info", "content": "这张互动卡已更新，请等下一张吧~"}}
        context = event.get("context") or {}
        message_id = context.get("open_message_id") or ""
        operator = event.get("operator") or {}
        clicker_open_id = operator.get("open_id") or operator.get("union_id") or ""
        sender_name = await self.resolve_user_name(clicker_open_id)
        now = time.time()
        instance = await self.card_repo.get_instance(card_id)
        if not instance or instance.get("mode") != mode:
            return {"toast": {"type": "info", "content": "这张互动卡已更新，请等下一张吧~"}}
        message_id = message_id or instance.get("message_id") or ""
        if now >= float(instance["expires_at"]):
            return {"toast": {"type": "info", "content": self.config.card_toast_expired}}
        if action_key not in list(instance.get("action_keys_json") or []):
            return {"toast": {"type": "error", "content": "这个按钮点不动了…"}}

        card_result = None
        display_instance = instance
        async with self.runtime.state_lock(int(instance["pet_id"])):
            state = await self.pet_repo.load_pet_state(int(instance["pet_id"]))
            if mode == "need":
                need = self.gameplay_domain.current_need(state, now)
                if (
                    not need
                    or need.get("id") != instance.get("need_id")
                    or int(need.get("round", 0) or 0) != int(instance.get("need_round", 0) or 0)
                ):
                    return {"toast": {"type": "info", "content": "这个需求已经变了，等下一张卡片吧~"}}
            claim = await self.card_repo.claim(card_id, clicker_open_id, action_key, now)
            if claim == "expired":
                return {"toast": {"type": "info", "content": self.config.card_toast_expired}}
            if claim in {"missing", "duplicate"}:
                return {"toast": {"type": "info", "content": "这次互动已经记下啦~"}}
            if claim == "settle":
                card_result = self.gameplay_service.resolve_card_action_state(
                    state,
                    action_key,
                    sender_name,
                    now,
                    prefer_free=mode != "need",
                )
                state = card_result.state
                state["last_social_ts"] = now
                if mode == "free":
                    state["last_free_card_ts"] = now
                await self.pet_repo.update_pet_state(int(instance["pet_id"]), state)
            display_state = state

        current = self.gameplay_domain.current_need(display_state, now)
        action_text = self.gameplay_domain.action_text(
            action_key,
            current.get("kind") if current and instance.get("need_id") else None,
        )
        did_text = card_result.did if card_result else action_text.get("did", "和你互动了一下")
        pending_text = card_result.pending if card_result else action_text.get("pending", "…")

        if card_result and mode == "need" and not card_result.resolved:
            # 一次弱方案只缓解：续办卡使用新 card_id，旧卡永远不能再次结算。
            continued = self.gameplay_domain.current_need(display_state, now)
            new_card_id = f"card-{uuid.uuid4().hex}"
            keys = self.gameplay_domain.choice_keys_for_need(continued.get("kind", ""))
            await self.card_repo.create_instance(
                card_id=new_card_id,
                pet_id=int(instance["pet_id"]),
                mode="need",
                need_id=continued.get("id", ""),
                need_round=int(continued.get("round", 0) or 0),
                built_at=now,
                expires_at=now + self.config.card_button_ttl_sec,
                max_settlements=1,
                base_text=instance.get("base_text") or "",
                action_keys=keys,
                img_key=instance.get("img_key") or None,
            )
            await self.card_repo.mark_announced(new_card_id, message_id)
            await self.pet_repo.mutate_state(
                int(instance["pet_id"]),
                lambda s: {
                    **s,
                    "active_need": {**s["active_need"], "announced_card_id": new_card_id},
                },
            )
            display_instance = await self.card_repo.get_instance(new_card_id) or instance
            card = self._build_from_instance(
                display_instance,
                display_state,
                self.card_domain.compose_card_text(
                    instance.get("base_text") or "", [], pending_text + " 还需要一点照料。"
                ),
            )
            reaction_card_id = new_card_id
        else:
            refreshed = await self.card_repo.get_instance(card_id)
            display_instance = refreshed or instance
            disable = int(display_instance.get("settlement_count", 0)) >= int(
                display_instance.get("max_settlements", 1)
            )
            card = self._build_from_instance(display_instance, display_state, pending_text, disable_actions=disable)
            reaction_card_id = card_id

        if message_id:
            asyncio.create_task(
                self._append_reaction_and_patch(
                    reaction_card_id,
                    message_id,
                    action_key,
                    sender_name,
                    clicker_open_id,
                    did_text,
                    pending_text,
                )
            )
        return {
            "toast": {"type": "success", "content": self.config.card_toast_done},
            "card": {"type": "raw", "data": card},
        }
