from __future__ import annotations

import asyncio
import json
import logging
import time

from dafeiyu.config import AppConfig
from dafeiyu.domain.conversation import ConversationContext
from dafeiyu.domain.reply import GeneratedReply
from dafeiyu.domain.state import StateDomain
from dafeiyu.integrations.llm_client import LLMClient
from dafeiyu.repositories.conversation_repo import ConversationRepository
from dafeiyu.repositories.message_repo import MessageRepository
from dafeiyu.repositories.system_repo import SystemRepository
from dafeiyu.runtime import RuntimeState
from dafeiyu.services.delivery import DeliveryService
from dafeiyu.services.memory_service import MemoryService
from dafeiyu.services.observer_service import ObserverService
from dafeiyu.services.style_service import StyleService

log = logging.getLogger("dafeiyu")


class AutonomousService:
    def __init__(
        self,
        config: AppConfig,
        runtime: RuntimeState,
        state_domain: StateDomain,
        context: ConversationContext,
        conversations: ConversationRepository,
        messages: MessageRepository,
        system: SystemRepository,
        memory: MemoryService,
        observer: ObserverService,
        llm: LLMClient,
        style: StyleService,
        delivery: DeliveryService,
    ):
        self.config, self.runtime, self.state_domain = config, runtime, state_domain
        self.context, self.conversations, self.messages = (
            context,
            conversations,
            messages,
        )
        self.system, self.memory, self.observer = system, memory, observer
        self.llm, self.style, self.delivery = llm, style, delivery

    def scheduled_event_due(self, state: dict, now: float) -> tuple[dict, str] | None:
        if self.state_domain.is_weekend_rest(now):
            return None
        local = self.state_domain.local_time(now)
        date = time.strftime("%Y-%m-%d", local)
        seconds = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
        for event in self.config.scheduled_events:
            if (
                0 <= seconds - event["hour"] * 3600 <= self.config.scheduled_grace_sec
                and state[event["state_key"]] != date
            ):
                return event, date
        return None

    def _cooling_down(self, state: dict, now: float) -> bool:
        delay = (
            self.config.proactive_cooldown_sec
            if state["last_proactive_answered"]
            else self.config.unanswered_cooldown_sec
        )
        return (
            now - state["last_proactive_ts"] < delay
            or now - state["last_reply_ts"] < self.config.proactive_reply_cooldown_sec
        )

    async def _speak(
        self,
        conversation_id: int,
        chat_id: str,
        prompt: str,
        query: str,
        *,
        source: str,
        max_tokens: int,
        extra_state: dict,
        gen_image: bool = False,
    ) -> tuple[str, dict]:
        history, _ = await self.conversations.load_conversation_context(conversation_id)
        recall = await self.memory.build_recall_block(conversation_id, query=query, k_recent=3)
        examples, ids = await self.style.render_examples_block(
            query,
            scope="proactive",
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
                        examples,
                        self.config.persona_reinforcement,
                        prompt,
                    )
                ),
            }
        )
        messages.append({"role": "user", "content": "[系统触发] 按本次情境自然发言"})
        reply = GeneratedReply.parse(
            await self.llm.chat_json(
                messages,
                max_tokens=max_tokens,
                temperature=self.config.autonomous_temperature,
            )
        )
        image_key = ""
        if gen_image and self.config.image_model:
            try:
                image = await asyncio.wait_for(
                    self.llm.generate_image(reply.image_prompt or reply.text),
                    timeout=self.config.card_image_timeout_sec,
                )
                if image:
                    image_key = await self.delivery.feishu.upload_image(image) or ""
            except Exception as exc:
                log.warning("dream image generation failed: %s", type(exc).__name__)
        await self.delivery.send(
            conversation_id,
            chat_id,
            reply.text,
            source=source,
            mode=reply.mode,
            example_ids=frozenset(ids),
            image_key=image_key,
            as_card=source in {"dream", "diary"} and self.config.card_enabled,
        )
        now = time.time()
        state = await self.conversations.mutate_state(
            conversation_id,
            lambda s: {
                **s,
                "last_update_ts": now,
                "last_proactive_ts": now,
                **extra_state,
            },
        )
        if await self.messages.count_unsummarized(conversation_id) > self.config.compress_threshold:
            self.runtime.spawn(self.memory.compress_conversation_memory(conversation_id))
        return reply.text, state

    async def evaluate_proactive_speaking(
        self, conversation_id: int, state: dict
    ) -> tuple[str, str] | None:
        rows = await self.messages.recent_messages(conversation_id, limit=15)
        last_activity = max(state["last_reply_ts"], state["last_proactive_ts"])
        if (
            sum(r["role"] == "user" and r["ts"] > last_activity for r in rows)
            < self.config.proactive_min_new_msgs
        ):
            return None
        lines = [
            f"[{self.config.character_name if r['role'] == 'assistant' else r['sender_name'] or '群友'}]: {r['content']}"
            for r in rows[-8:]
        ]
        try:
            data = json.loads(
                await self.llm.chat_json(
                    [
                        {
                            "role": "system",
                            "content": self.config.proactive_eval_prompt,
                        },
                        {
                            "role": "user",
                            "content": "最近群聊：\n"
                            + "\n".join(lines)
                            + "\n上次主动话题："
                            + state["last_proactive_topic"],
                        },
                    ],
                    max_tokens=250,
                    temperature=0.3,
                )
            )
            if not isinstance(data, dict) or data.get("should_speak") is not True:
                return None
            reason, topic = (
                str(data.get("reason") or "").strip(),
                str(data.get("topic") or "").strip(),
            )
            if (
                not reason
                or not topic
                or not state["last_proactive_answered"]
                and topic == state["last_proactive_topic"]
            ):
                return None
            return reason, topic
        except Exception as exc:
            log.warning(
                "interjection evaluation failed conversation=%d error=%s",
                conversation_id,
                type(exc).__name__,
            )
            return None

    async def _proactive_locked(
        self, conversation_id: int, chat_id: str, trigger: str, topic: str = ""
    ) -> tuple[str, dict]:
        return await self._speak(
            conversation_id,
            chat_id,
            self.config.proactive_prompt.format(trigger=trigger),
            trigger,
            source="proactive",
            max_tokens=self.config.reply_max_tokens,
            extra_state={
                "last_proactive_topic": topic,
                "last_proactive_answered": False,
            },
        )

    async def proactive_speak(
        self, conversation_id: int, chat_id: str, trigger: str, topic: str = ""
    ) -> tuple[str, dict]:
        async with self.runtime.chat_lock(conversation_id):
            await self.observer.flush_observer_buffer(conversation_id)
            return await self._proactive_locked(conversation_id, chat_id, trigger, topic)

    async def _scheduled_locked(
        self,
        conversation_id: int,
        chat_id: str,
        event: dict,
        date: str,
        mark_date: bool,
    ) -> tuple[str, dict]:
        prompt = self.config.scheduled_event_prompt.format(
            event_name=event["name"],
            scheduled_hour=event["hour"],
            instruction=event["instruction"],
        )
        prompt += "\n" + event.get("extra_prompt", "")
        return await self._speak(
            conversation_id,
            chat_id,
            prompt,
            event["instruction"],
            source=event["kind"],
            max_tokens=self.config.scheduled_max_tokens,
            extra_state={event["state_key"]: date} if mark_date else {},
            gen_image=bool(event.get("gen_image")),
        )

    async def scheduled_speak(
        self,
        conversation_id: int,
        chat_id: str,
        event: dict,
        date_key: str,
        *,
        mark_date: bool = True,
    ) -> tuple[str, dict]:
        async with self.runtime.chat_lock(conversation_id):
            await self.observer.flush_observer_buffer(conversation_id)
            return await self._scheduled_locked(
                conversation_id, chat_id, event, date_key, mark_date
            )

    async def _maybe_interject_locked(
        self, conversation_id: int, chat_id: str, state: dict, now: float
    ) -> bool:
        if self.state_domain.in_quiet_hours(now) or self._cooling_down(state, now):
            return False
        evaluation = await self.evaluate_proactive_speaking(conversation_id, state)
        if evaluation is None or self.runtime.chat_pending_messages.get(conversation_id):
            return False
        await self._proactive_locked(conversation_id, chat_id, *evaluation)
        return True

    async def maybe_interject(self, conversation_id: int, chat_id: str) -> bool:
        if (
            self.runtime.chat_pending_messages.get(conversation_id)
            or self.runtime.chat_lock(conversation_id).locked()
        ):
            return False
        async with self.runtime.chat_lock(conversation_id):
            state = await self.conversations.load_conversation_state(conversation_id)
            return await self._maybe_interject_locked(conversation_id, chat_id, state, time.time())

    async def tick_conversation(
        self, conversation_id: int, chat_id: str, now: float | None = None
    ) -> bool:
        now = time.time() if now is None else now
        if (
            self.runtime.chat_pending_messages.get(conversation_id)
            or self.runtime.chat_lock(conversation_id).locked()
        ):
            return False
        async with self.runtime.chat_lock(conversation_id):
            await self.observer.flush_observer_buffer(conversation_id)
            state = await self.conversations.load_conversation_state(conversation_id)
            due = self.scheduled_event_due(state, now)
            if due:
                event, date = due
                recent = await self.messages.recent_messages(conversation_id, 20)
                if not any(r["role"] == "user" and now - r["ts"] <= 86400 for r in recent):
                    await self.conversations.mutate_state(
                        conversation_id, lambda s: {**s, event["state_key"]: date}
                    )
                    return False
                await self._scheduled_locked(conversation_id, chat_id, event, date, True)
                return True
            return await self._maybe_interject_locked(conversation_id, chat_id, state, now)

    async def tick_all_conversations(self) -> None:
        await self.system.clean_old_events()
        await self.observer.flush_all()
        for row in await self.conversations.load_all_conversations():
            try:
                await self.tick_conversation(row["id"], row["chat_id"])
            except Exception as exc:
                log.error(
                    "autonomous task failed conversation=%d error=%s",
                    row["id"],
                    type(exc).__name__,
                )

    async def run_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.tick_interval_sec)
            try:
                await self.tick_all_conversations()
            except Exception as exc:
                log.error("autonomous loop failed: %s", type(exc).__name__)
