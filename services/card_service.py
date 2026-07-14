from __future__ import annotations

import asyncio
import json
import logging
import time

from config import AppConfig
from domain.card import CardDomain
from domain.gameplay import GameplayDomain
from domain.pet import PetDomain
from domain.state import StateDomain
from integrations.feishu_client import FeishuClient
from integrations.llm_client import LLMClient
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
        if cached:
            return cached
        return f"群友-{open_id[-4:]}"

    async def card_action_followup(
        self,
        message_id: str,
        action_key: str,
        clicker_open_id: str,
        did_text: str | None = None,
        pending_text: str | None = None,
    ) -> None:
        try:
            entry = self.runtime.card_followup_buffer.get(message_id)
            if not entry:
                return
            pet_id = entry["pet_id"]
            sender_name = (
                await self.resolve_user_name(clicker_open_id)
                if clicker_open_id
                else "群友"
            )
            text_cfg = self.config.card_action_text.get(action_key, {})
            did = text_cfg.get("did", "和你互动了一下")
            pending = text_cfg.get("pending", "…")
            if did_text:
                did = did_text
            if pending_text:
                pending = pending_text

            await self.message_repo.append_message(
                pet_id, "user", did, sender_name=sender_name, is_observer=False
            )

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
                        sender_name=sender_name, did=did
                    ),
                }
            )

            reaction = ""
            try:
                reaction = await self.llm.chat_text(
                    messages,
                    max_tokens=self.config.card_reply_max_tokens,
                    temperature=0.9,
                )
            except Exception:
                log.exception("card action LLM reply failed; keeping pending text")
            if not reaction:
                reaction = pending

            await self.message_repo.append_message(pet_id, "assistant", reaction)

            entry = self.runtime.card_followup_buffer.get(message_id)
            if not entry:
                return
            entry["lines"].append(reaction)
            if self.config.card_log_max_lines > 0:
                del entry["lines"][: -self.config.card_log_max_lines]
            new_state = await self.pet_repo.load_pet_state(pet_id)
            display = self.card_domain.compose_card_text(entry["base_text"], entry["lines"])
            await self.feishu.update_card_message(
                message_id,
                self.card_domain.rebuild_action_card(
                    pet_id,
                    display,
                    new_state,
                    entry.get("card_keys"),
                    bool(entry.get("is_fixed")),
                    entry.get("built_at"),
                    entry["base_text"],
                    entry.get("img_key"),
                ),
            )

            if await self.message_repo.count_unsummarized(pet_id) > self.config.compress_threshold:
                asyncio.create_task(self.memory_service.compress_pet_memory(pet_id))
        except Exception:
            log.exception("card action followup failed")

    async def handle_card_action(self, event: dict, event_id: str | None) -> dict:
        try:
            action = event.get("action") or {}
            value = action.get("value") or {}
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = {}
            try:
                pet_id = int(value.get("pet_id"))
            except (TypeError, ValueError):
                pet_id = None
            action_key = value.get("action")
            need_id = value.get("need_id") or ""
            need_kind = value.get("need_kind") or ""
            card_keys = value.get("keys")
            is_fixed = bool(value.get("fixed"))
            built_at = value.get("built_at")
            base_text = value.get("base") or ""
            img_key = value.get("img_key") or None
            context = event.get("context") or {}
            message_id = context.get("open_message_id")
            operator = event.get("operator") or {}
            clicker_open_id = operator.get("open_id") or operator.get("union_id") or ""
            sender_name = (
                await self.resolve_user_name(clicker_open_id)
                if clicker_open_id
                else "群友"
            )

            is_need_action = bool(
                need_id and self.gameplay_domain.choice_for_action(need_kind, action_key)
            )
            if pet_id is None or (
                not is_need_action and action_key not in self.config.card_actions
            ):
                return {"toast": {"type": "error", "content": "这个按钮点不动了…"}}

            now = time.time()

            if (
                self.config.card_button_ttl_sec > 0
                and isinstance(built_at, (int, float))
                and now - built_at >= self.config.card_button_ttl_sec
            ):
                return {"toast": {"type": "info", "content": self.config.card_toast_expired}}

            if event_id and await self.system_repo.check_and_register_event(event_id):
                return {}

            async with self.runtime.state_lock(pet_id):
                try:
                    state = await self.pet_repo.load_pet_state(pet_id)
                except ValueError:
                    return {"toast": {"type": "error", "content": "找不到我了…"}}

                click_ts = dict(state.get("card_action_ts") or {})
                raw_last = click_ts.get(clicker_open_id)
                last_ts = float(raw_last) if isinstance(raw_last, (int, float)) else 0.0
                if (
                    self.config.card_action_cooldown_sec > 0
                    and now - last_ts < self.config.card_action_cooldown_sec
                ):
                    return {
                        "toast": {
                            "type": "info",
                            "content": self.config.card_toast_cooldown,
                        }
                    }

                if is_need_action:
                    current_need = self.gameplay_domain.current_need(state, now)
                    if not current_need or current_need.get("id") != need_id:
                        return {
                            "toast": {
                                "type": "info",
                                "content": "这个需求已经变了，等下一张卡片吧~",
                            }
                        }
                try:
                    card_result = self.gameplay_service.resolve_card_action_state(
                        state,
                        action_key,
                        sender_name,
                        now,
                        prefer_free=is_fixed,
                    )
                except ValueError:
                    return {
                        "toast": {
                            "type": "error",
                            "content": "这个卡片动作已经接不上了…",
                        }
                    }
                new_state = card_result.state
                click_ts[clicker_open_id] = now
                new_state["card_action_ts"] = self.card_domain.prune_card_click_ts(
                    click_ts, now
                )
                new_state["last_update_ts"] = now
                await self.pet_repo.update_pet_state(pet_id, new_state)

            pending = card_result.pending
            did_text = card_result.did
            if message_id:
                entry = self.runtime.card_followup_buffer.setdefault(
                    message_id,
                    {
                        "pet_id": pet_id,
                        "card_keys": card_keys,
                        "is_fixed": is_fixed,
                        "built_at": built_at,
                        "base_text": base_text,
                        "img_key": img_key,
                        "lines": [],
                    },
                )
                base = entry["base_text"]
                display = self.card_domain.compose_card_text(
                    base, entry["lines"], pending
                )
                card = self.card_domain.rebuild_action_card(
                    pet_id, display, new_state, card_keys, is_fixed, built_at, base,
                    entry.get("img_key"),
                )
                asyncio.create_task(
                    self.card_action_followup(
                        message_id,
                        action_key,
                        clicker_open_id,
                        did_text=did_text,
                        pending_text=pending,
                    )
                )
                self.card_domain.prune_card_followup_buffer(
                    self.runtime.card_followup_buffer, now
                )
            else:
                card = self.card_domain.rebuild_action_card(
                    pet_id, pending, new_state, card_keys, is_fixed, built_at, base_text,
                    img_key,
                )

            log.info(
                "pet %d card action %s by %s -> %s",
                pet_id,
                action_key,
                clicker_open_id or "?",
                {key: round(new_state.get(key, 0)) for key in self.config.state_numeric_keys},
            )
            return {
                "toast": {"type": "success", "content": self.config.card_toast_done},
                "card": {"type": "raw", "data": card},
            }
        except Exception:
            log.exception("card action failed")
            return {"toast": {"type": "error", "content": "呜…出了点小问题"}}
