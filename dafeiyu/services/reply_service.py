from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace

from dafeiyu.config import AppConfig
from dafeiyu.domain.conversation import ConversationContext
from dafeiyu.domain.meme import meme_rejection_reason
from dafeiyu.domain.reply import GeneratedReply
from dafeiyu.integrations.llm_client import LLMClient
from dafeiyu.repositories.conversation_repo import ConversationRepository
from dafeiyu.repositories.message_repo import MessageRepository
from dafeiyu.repositories.system_repo import SystemRepository
from dafeiyu.runtime import RuntimeState
from dafeiyu.services.autonomous_service import AutonomousService
from dafeiyu.services.delivery import DeliveryService
from dafeiyu.services.incoming import IncomingMessage, IncomingService
from dafeiyu.services.memory_service import MemoryService
from dafeiyu.services.observer_service import ObserverService
from dafeiyu.services.style_service import StyleService

log = logging.getLogger("dafeiyu")


class ReplyService:
    def __init__(
        self,
        config: AppConfig,
        runtime: RuntimeState,
        context: ConversationContext,
        conversations: ConversationRepository,
        messages: MessageRepository,
        system: SystemRepository,
        memory: MemoryService,
        observer: ObserverService,
        incoming: IncomingService,
        delivery: DeliveryService,
        llm: LLMClient,
        style: StyleService,
        autonomous: AutonomousService,
    ):
        self.config, self.runtime, self.context = config, runtime, context
        self.conversations, self.messages, self.system = conversations, messages, system
        self.memory, self.observer, self.incoming = memory, observer, incoming
        self.delivery, self.llm, self.style, self.autonomous = (
            delivery,
            llm,
            style,
            autonomous,
        )

    async def generate(
        self, conversation_id: int, items: list[IncomingMessage], current_ids: set[int]
    ) -> GeneratedReply:
        history, _ = await self.conversations.load_conversation_context(conversation_id)
        history = [row for row in history if row["id"] not in current_ids]
        query = "\n".join(item.text for item in items)
        recall = await self.memory.build_recall_block(conversation_id, query=query)
        style_text, example_ids = await self.style.render_examples_block(
            query,
            scope="reply",
            history=history,
            exclude_ids=self.runtime.last_style_ids.get(conversation_id, set()),
        )
        messages = self.context.base_messages(self.config.system_prompt + recall, history)
        messages.append(
            {
                "role": "system",
                "content": "\n".join(
                    (
                        self.config.json_output_prompt,
                        style_text,
                        self.config.persona_reinforcement,
                    )
                ),
            }
        )
        messages.extend(
            {
                "role": "user",
                "content": self.context.wrap_user(item.text, item.sender_name),
            }
            for item in items
        )
        content = await self.llm.chat_json(
            messages,
            max_tokens=self.config.reply_max_tokens,
            temperature=self.config.reply_temperature,
        )
        reply = replace(GeneratedReply.parse(content), example_ids=frozenset(example_ids))
        if reply.meme_tag:
            reason = (
                "ambiguous_batch"
                if len(items) != 1
                else meme_rejection_reason(reply.meme_tag, reply.meme_evidence, query, reply.text)
            )
            if reason:
                log.info(
                    "meme decision conversation=%d tag=%s result=%s",
                    conversation_id,
                    reply.meme_tag,
                    reason,
                )
                reply = replace(reply, meme_tag="", meme_evidence="")
        log.info(
            "reply generated conversation=%d mode=%s chars=%d",
            conversation_id,
            reply.mode,
            len(reply.text),
        )
        return reply

    async def handle_message_event(self, event: dict) -> None:
        message = await self.incoming.parse(event)
        if message is None:
            return
        if not message.direct:
            if message.fallback:
                return
            conversation_id = await self.conversations.find_conversation(message.chat_id)
            if conversation_id is None:
                return
            self.runtime.observer_buffer.setdefault(conversation_id, []).append(
                {
                    "content": message.text,
                    "sender_name": message.sender_name,
                    "open_id": message.sender_open_id,
                    "ts": time.time(),
                }
            )
            if (
                len(self.runtime.observer_buffer[conversation_id])
                >= self.config.observer_flush_max_count
            ):
                await self.observer.flush_observer_buffer(conversation_id)
            if self.config.event_interject_enabled:
                task = self.runtime.interject_tasks.get(conversation_id)
                if task is None or task.done():
                    self.runtime.interject_tasks[conversation_id] = self.runtime.spawn(
                        self.maybe_event_interject(conversation_id, message.chat_id)
                    )
            return
        conversation_id = await self.conversations.get_or_create_conversation(message.chat_id)
        self.runtime.chat_pending_messages.setdefault(conversation_id, []).append(message)
        async with self.runtime.chat_lock(conversation_id):
            while self.runtime.chat_pending_messages.get(conversation_id):
                await asyncio.sleep(self.config.reply_debounce_sec)
                items = self.runtime.chat_pending_messages.pop(conversation_id, [])
                await self.observer.flush_observer_buffer(conversation_id)
                await self._reply_batch(conversation_id, items)

    async def _reply_batch(self, conversation_id: int, items: list[IncomingMessage]) -> None:
        current_ids = set()
        for item in items:
            if item.text:
                current_ids.add(
                    await self.messages.append_message(
                        conversation_id,
                        "user",
                        item.text,
                        sender_name=item.sender_name,
                        sender_open_id=item.sender_open_id,
                    )
                )
        last = items[-1]
        valid = [item for item in items if not item.fallback]
        if valid:
            try:
                reply = await self.generate(conversation_id, valid, current_ids)
            except Exception as exc:
                log.warning(
                    "reply generation failed conversation=%d error=%s",
                    conversation_id,
                    type(exc).__name__,
                )
                reply = GeneratedReply(self.config.fallback_replies["empty_llm"], "reaction")
        else:
            reply = GeneratedReply(self.config.fallback_replies[last.fallback], "reaction")
        if reply.meme_tag and len(items) != 1:
            log.info(
                "meme decision conversation=%d tag=%s result=ambiguous_batch",
                conversation_id,
                reply.meme_tag,
            )
            reply = replace(reply, meme_tag="", meme_evidence="")
        try:
            await self.delivery.send(
                conversation_id,
                last.chat_id,
                reply.text,
                reply_to=last.message_id if last.chat_type != "p2p" else "",
                mode=reply.mode,
                meme_tag=reply.meme_tag,
                meme_evidence=reply.meme_evidence,
                meme_user_text=items[0].text if len(items) == 1 else "",
                example_ids=reply.example_ids,
            )
        except Exception as exc:
            log.error(
                "reply delivery failed conversation=%d error=%s",
                conversation_id,
                type(exc).__name__,
            )
            return
        if (
            len(valid) == 1
            and reply.speaker_name
            and valid[0].sender_open_id
            and reply.speaker_name in valid[0].text
            and not reply.speaker_name.startswith("群友")
        ):
            await self.system.set_cached_user_name(valid[0].sender_open_id, reply.speaker_name)
        if await self.messages.count_unsummarized(conversation_id) > self.config.compress_threshold:
            self.runtime.spawn(self.memory.compress_conversation_memory(conversation_id))

    async def maybe_event_interject(self, conversation_id: int, chat_id: str) -> None:
        await asyncio.sleep(self.config.event_interject_debounce_sec)
        if self.runtime.chat_pending_messages.get(conversation_id):
            return
        await self.observer.flush_observer_buffer(conversation_id)
        await self.autonomous.maybe_interject(conversation_id, chat_id)
