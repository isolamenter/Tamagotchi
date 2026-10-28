from __future__ import annotations

import asyncio
import json
import logging
import time

from dafeiyu.config import AppConfig
from dafeiyu.domain.consolidation import DiaryNotReady
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
        self.consolidation = None
        self.consolidation_tasks = {}

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
        return await self._send_reply(
            conversation_id,
            chat_id,
            reply,
            source=source,
            extra_state=extra_state,
            example_ids=frozenset(ids),
            image_key=image_key,
        )

    async def _send_reply(
        self,
        conversation_id: int,
        chat_id: str,
        reply: GeneratedReply,
        *,
        source: str,
        extra_state: dict,
        example_ids: frozenset[str] = frozenset(),
        image_key: str = "",
    ) -> tuple[str, dict]:
        await self.delivery.send(
            conversation_id,
            chat_id,
            reply.text,
            source=source,
            mode=reply.mode,
            example_ids=example_ids,
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
        self, conversation_id: int, state: dict, *, scheduled_context: str = ""
    ) -> tuple[str, str] | None:
        rows = await self.messages.recent_messages(conversation_id, limit=15)
        last_activity = max(state["last_reply_ts"], state["last_proactive_ts"])
        status = await self.conversations.evaluation_status(conversation_id)
        now = time.time()
        if now - status["last_attempt"] < 60:
            return None
        fresh = [
            r
            for r in rows
            if r["role"] == "user"
            and r["id"] > status["until_id"]
            and r["ts"] > last_activity
            and 0
            <= now - r["ts"]
            <= float(self.config.settings.get("autonomous", {}).get("fresh_activity_sec", 180))
        ]
        if len(fresh) < self.config.proactive_min_new_msgs:
            return None
        lines = [
            self.context.wrap_user(
                r["content"],
                r["sender_name"],
                sender_open_id=r.get("sender_open_id", ""),
                mentions=r.get("mentions", ()),
                ts=r["ts"],
            )
            if r["role"] == "user"
            else self.config.character_name + ": " + r["content"]
            for r in rows[-8:]
        ]
        await self.conversations.record_evaluation(conversation_id, None, now)
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
                            + state["last_proactive_topic"]
                            + "\n上次主动话题是否已被人回应："
                            + str(state["last_proactive_answered"])
                            + (
                                "\n【本次拟发布的定时内容】\n"
                                + scheduled_context
                                + "\n只判断这项内容是否适合此刻发布；适合普通接话不代表适合发布它。"
                                if scheduled_context
                                else ""
                            )
                            + "\n若上次未获回应，避免语义重复；不要只比话题字面。只有新消息提供自然参与价值才说话。",
                        },
                    ],
                    max_tokens=250,
                    temperature=0.3,
                )
            )
            if not isinstance(data, dict) or type(data.get("should_speak")) is not bool:
                raise ValueError("invalid participation decision")
            if data["should_speak"] and (
                not isinstance(data.get("reason"), str) or not isinstance(data.get("topic"), str)
            ):
                raise ValueError("invalid participation reason/topic")
            await self.conversations.record_evaluation(
                conversation_id, max(r["id"] for r in fresh), now
            )
            if data["should_speak"] is not True:
                return None
            reason, topic = (
                str(data.get("reason") or "").strip(),
                str(data.get("topic") or "").strip(),
            )
            if not reason or not topic:
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
        if event["kind"] == "diary":
            diary = await self.consolidation.identities.diary(conversation_id, date)
            if diary is None:
                raise DiaryNotReady("今天的每日整理尚未产生日记，或没有合适内容")
            return await self._send_reply(
                conversation_id,
                chat_id,
                GeneratedReply(diary["text"], "normal"),
                source="diary",
                extra_state={event["state_key"]: date} if mark_date else {},
            )
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
        if self.state_domain.is_weekend_rest(now) or self._cooling_down(state, now):
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
        self,
        conversation_id: int,
        chat_id: str,
        now: float | None = None,
        *,
        run_internal: bool = True,
    ) -> bool:
        now = time.time() if now is None else now
        if run_internal and self.consolidation is not None:
            await self.observer.flush_observer_buffer(conversation_id)
            await self.consolidation.maybe_consolidate(conversation_id, now)
        if (
            self.runtime.chat_pending_messages.get(conversation_id)
            or self.runtime.chat_lock(conversation_id).locked()
        ):
            return False
        async with self.runtime.chat_lock(conversation_id):
            await self.observer.flush_observer_buffer(conversation_id)
            state = await self.conversations.load_conversation_state(conversation_id)
            due = self.scheduled_event_due(state, now)
            scheduled_context = ""
            if due and due[0]["kind"] == "diary":
                diary = await self.consolidation.identities.diary(conversation_id, due[1])
                if diary is None:
                    due = None  # A pending/empty diary does not block ordinary conversation.
                else:
                    scheduled_context = "日记：" + diary["text"]
            if due:
                event, date = due
                if event["kind"] == "dream":
                    scheduled_context = "梦境分享：" + event["instruction"]
                if (
                    self._cooling_down(state, now)
                    or await self.evaluate_proactive_speaking(
                        conversation_id, state, scheduled_context=scheduled_context
                    )
                    is None
                ):
                    # Skipped is not sent: fresh activity may qualify later inside the grace window.
                    return False
                await self._scheduled_locked(conversation_id, chat_id, event, date, True)
                return True
            return await self._maybe_interject_locked(conversation_id, chat_id, state, now)

    async def tick_all_conversations(self) -> None:
        await self.system.clean_old_events()
        await self.observer.flush_all()
        for row in await self.conversations.load_all_conversations():
            try:
                cid = row["id"]
                task = self.consolidation_tasks.get(cid)
                if self.consolidation is not None and (task is None or task.done()):
                    self.consolidation_tasks[cid] = self.runtime.spawn(
                        self.consolidation.maybe_consolidate(cid)
                    )
                await self.tick_conversation(cid, row["chat_id"], run_internal=False)
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
