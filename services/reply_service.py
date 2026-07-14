from __future__ import annotations

import asyncio
import json
import logging
import time

from config import AppConfig
from domain.pet import PetDomain
from domain.state import StateDomain
from integrations.feishu_client import FeishuClient
from integrations.llm_client import LLMClient
from repositories.message_repo import MessageRepository
from repositories.pet_repo import PetRepository
from repositories.system_repo import SystemRepository
from runtime import RuntimeState
from services.memory_service import MemoryService
from services.observer_service import ObserverService

log = logging.getLogger("tamagotchi")


class ReplyService:
    def __init__(
        self,
        config: AppConfig,
        runtime: RuntimeState,
        state_domain: StateDomain,
        pet_domain: PetDomain,
        pet_repo: PetRepository,
        message_repo: MessageRepository,
        system_repo: SystemRepository,
        memory_service: MemoryService,
        observer_service: ObserverService,
        feishu: FeishuClient,
        llm: LLMClient,
    ):
        self.config = config
        self.runtime = runtime
        self.state_domain = state_domain
        self.pet_domain = pet_domain
        self.pet_repo = pet_repo
        self.message_repo = message_repo
        self.system_repo = system_repo
        self.memory_service = memory_service
        self.observer_service = observer_service
        self.feishu = feishu
        self.llm = llm

    async def resolve_user_name(self, open_id: str) -> str:
        if not open_id:
            return "群友"
        cached = await self.system_repo.get_cached_user_name(open_id)
        if cached:
            return cached
        return f"群友-{open_id[-4:]}"

    async def call_llm_with_memory(
        self,
        pet_id: int,
        user_text: str,
        sender_name: str = "",
        current_msg_id: int | None = None,
        sender_open_id: str = "",
    ) -> tuple[str, dict, str]:
        history, current_state = await self.pet_repo.load_pet_context(pet_id)

        recall_block = await self.memory_service.build_recall_block(
            pet_id, query=user_text
        )
        system_content = self.config.system_prompt + recall_block

        hist = [item for item in history if item.get("id") != current_msg_id]
        messages = self.pet_domain.base_messages(system_content, hist)

        pre_user_system = (
            self.state_domain.render_state(current_state)
            + "\n"
            + self.config.json_output_prompt
            + "\n"
            + self.config.persona_reinforcement
        )
        messages.append({"role": "system", "content": pre_user_system})
        messages.append(
            {
                "role": "user",
                "content": self.pet_domain.wrap_user(user_text, sender_name=sender_name),
            }
        )

        content = await self.llm.chat_json(
            messages,
            max_tokens=self.config.reply_max_tokens,
            temperature=0.9,
        )

        speaker_name = ""
        try:
            data = json.loads(content)
            reply = (data.get("reply") or "").strip()
            speaker_name = (data.get("speaker_name") or "").strip()
        except json.JSONDecodeError:
            log.warning("LLM returned non-JSON: %r", content[:200])
            reply = content

        if not reply:
            reply = self.config.fallback_replies["empty_llm"]

        learned_name = ""
        if (
            speaker_name
            and sender_open_id
            and not speaker_name.startswith("群友")
            and speaker_name in user_text
        ):
            if await self.system_repo.get_cached_user_name(sender_open_id) != speaker_name:
                await self.system_repo.set_cached_user_name(sender_open_id, speaker_name)
                learned_name = speaker_name
                log.info(
                    "pet %d learned name from chat: %s -> %s",
                    pet_id,
                    sender_open_id,
                    speaker_name,
                )

        def _mutator(state: dict) -> dict:
            # 普通对话只读取 state；五维玩法状态只能由卡片动作修改。
            out = dict(state)
            out["last_update_ts"] = time.time()
            return out

        new_state = await self.pet_repo.mutate_state(pet_id, _mutator)
        log.info(
            "pet %d state read-only: %s -> %s",
            pet_id,
            {key: round(current_state.get(key, 0)) for key in self.config.state_numeric_keys},
            {key: round(new_state.get(key, 0)) for key in self.config.state_numeric_keys},
        )

        return reply, new_state, learned_name

    async def mark_replied(self, pet_id: int) -> None:
        await self.pet_repo.mutate_state(
            pet_id, lambda s: {**s, "last_reply_ts": time.time()}
        )

    async def is_direct_to_bot(self, mentions: list[dict]) -> bool:
        try:
            bot_open_id = await self.feishu.get_bot_open_id()
        except Exception:
            log.exception(
                "get bot open_id failed; falling back to treating all messages as direct"
            )
            return True
        return any((mention.get("id") or {}).get("open_id") == bot_open_id for mention in mentions)

    async def _maybe_compress(self, pet_id: int) -> None:
        if await self.message_repo.count_unsummarized(pet_id) > self.config.compress_threshold:
            asyncio.create_task(self.memory_service.compress_pet_memory(pet_id))

    async def handle_message_event(self, event: dict) -> None:
        msg = event.get("message") or {}
        message_id = msg.get("message_id")
        chat_id = msg.get("chat_id")
        if not message_id or not chat_id:
            return

        mentions = msg.get("mentions") or []
        sender_open_id = (
            ((event.get("sender") or {}).get("sender_id") or {}).get("open_id")
        ) or ""
        sender_name = (
            await self.resolve_user_name(sender_open_id) if sender_open_id else "群友"
        )

        is_direct = await self.is_direct_to_bot(mentions)
        msg_type = msg.get("message_type")

        if not is_direct:
            if msg_type != "text":
                return
            try:
                content = json.loads(msg.get("content") or "{}")
            except json.JSONDecodeError:
                return
            text = self.pet_domain.clean_text(content.get("text", ""), mentions)
            if not text:
                return
            pet_id = await self.pet_repo.find_pet(chat_id)
            if pet_id is None:
                return
            buf = self.runtime.observer_buffer.setdefault(pet_id, [])
            buf.append(
                {
                    "content": text,
                    "sender_name": sender_name,
                    "open_id": sender_open_id,
                    "ts": time.time(),
                }
            )
            log.info(
                "pet %d buffered observer [%s]: %r (buffer=%d)",
                pet_id,
                sender_name,
                text[:80],
                len(buf),
            )
            if len(buf) >= self.config.observer_flush_max_count:
                await self.observer_service.flush_observer_buffer(pet_id)
            return

        if msg_type != "text":
            await self.feishu.reply_text(message_id, self.config.fallback_replies["non_text"])
            return

        try:
            content = json.loads(msg.get("content") or "{}")
        except json.JSONDecodeError:
            log.warning("bad content json: %r", msg.get("content"))
            return

        user_text = self.pet_domain.clean_text(content.get("text", ""), mentions)
        if not user_text:
            await self.feishu.reply_text(message_id, self.config.fallback_replies["empty_text"])
            return

        pet_id = await self.pet_repo.get_or_create_pet(chat_id)
        log.info(
            "pet_id=%d chat_id=%s sender=%s user_text=%r",
            pet_id,
            chat_id,
            sender_name,
            user_text,
        )

        await self.observer_service.flush_observer_buffer(pet_id)

        if self.state_domain.in_quiet_hours(time.time()):
            await self.message_repo.append_message(
                pet_id,
                "user",
                user_text,
                sender_name=sender_name,
                is_observer=False,
                sender_open_id=sender_open_id,
            )
            await self.feishu.reply_text(
                message_id, self.config.fallback_replies["quiet_hours"]
            )
            log.info("pet %d @ during quiet hours, sent sleeping reply", pet_id)
            await self._maybe_compress(pet_id)
            return

        if self.config.reply_min_interval_sec > 0:
            now = time.time()
            db_ts = float((await self.pet_repo.load_pet_state(pet_id)).get("last_reply_ts", 0.0))
            last_reply_ts = max(db_ts, self.runtime.reply_gate.get(pet_id, 0.0))
            elapsed = now - last_reply_ts
            if elapsed < self.config.reply_min_interval_sec:
                await self.message_repo.append_message(
                    pet_id,
                    "user",
                    user_text,
                    sender_name=sender_name,
                    is_observer=False,
                    sender_open_id=sender_open_id,
                )
                log.info(
                    "pet %d @ within reply cooldown (%.0fs left), recorded without reply",
                    pet_id,
                    self.config.reply_min_interval_sec - elapsed,
                )
                await self._maybe_compress(pet_id)
                return
            self.runtime.reply_gate[pet_id] = now

        current_msg_id = await self.message_repo.append_message(
            pet_id,
            "user",
            user_text,
            sender_name=sender_name,
            is_observer=False,
            sender_open_id=sender_open_id,
        )
        learned_name = ""
        try:
            reply, _, learned_name = await self.call_llm_with_memory(
                pet_id,
                user_text,
                sender_name=sender_name,
                current_msg_id=current_msg_id,
                sender_open_id=sender_open_id,
            )
        except Exception as exc:
            log.exception("llm error")
            reply = self.config.fallback_replies["llm_error_template"].format(
                error_class=exc.__class__.__name__
            )
        log.info("reply=%r", reply)

        await self.message_repo.append_message(pet_id, "assistant", reply)
        await self.feishu.reply_text(message_id, reply)
        await self.mark_replied(pet_id)

        if learned_name:
            await self.feishu.reply_text(
                message_id,
                self.config.fallback_replies["name_learned"].format(name=learned_name),
            )

        await self._maybe_compress(pet_id)
