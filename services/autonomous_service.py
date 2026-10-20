from __future__ import annotations

import asyncio
import json
import logging
import time

from config import AppConfig
from domain.card import CardDomain
from domain.pet import PetDomain
from domain.state import StateDomain
from integrations.feishu_client import FeishuClient
from integrations.llm_client import LLMClient
from repositories.message_repo import MessageRepository
from repositories.pet_repo import PetRepository
from repositories.system_repo import SystemRepository
from runtime import RuntimeState
from services.card_service import CardService
from services.memory_service import MemoryService
from services.observer_service import ObserverService
from services.style_service import StyleService

log = logging.getLogger("tamagotchi")


class AutonomousService:
    def __init__(
        self,
        config: AppConfig,
        state_domain: StateDomain,
        card_domain: CardDomain,
        pet_domain: PetDomain,
        pet_repo: PetRepository,
        message_repo: MessageRepository,
        system_repo: SystemRepository,
        memory_service: MemoryService,
        observer_service: ObserverService,
        feishu: FeishuClient,
        llm: LLMClient,
        card_service: CardService | None = None,
        style_service: StyleService | None = None,
        runtime: RuntimeState | None = None,
    ):
        self.config = config
        self.state_domain = state_domain
        self.card_domain = card_domain
        self.pet_domain = pet_domain
        self.pet_repo = pet_repo
        self.message_repo = message_repo
        self.system_repo = system_repo
        self.card_service = card_service
        self.style_service = style_service
        self.memory_service = memory_service
        self.observer_service = observer_service
        self.feishu = feishu
        self.llm = llm
        self.runtime = runtime

    def scheduled_event_due(self, state: dict, now: float) -> tuple[dict, str] | None:
        if self.state_domain.is_weekend_rest(now):
            return None
        local = self.state_domain.local_time(now)
        date_key = time.strftime("%Y-%m-%d", local)
        seconds_today = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
        for event in self.config.scheduled_events:
            since_event = seconds_today - int(event["hour"]) * 3600
            if (
                0 <= since_event <= self.config.scheduled_grace_sec
                and state.get(event["state_key"]) != date_key
            ):
                return event, date_key
        return None

    async def autonomous_speak(
        self,
        pet_id: int,
        chat_id: str,
        prompt: str,
        user_stub: str,
        log_label: str,
        *,
        max_tokens: int,
        set_last_proactive: bool = False,
        extra_state: dict | None = None,
        as_card: bool = False,
        gen_image: bool = False,
        style_query: str = "",
    ) -> tuple[str, dict] | None:
        history, _current_state = await self.pet_repo.load_pet_context(pet_id)

        recall_block = await self.memory_service.build_recall_block(
            pet_id, query="", k_recent=5
        )
        system_content = self.config.system_prompt + recall_block
        messages = self.pet_domain.base_messages(system_content, history)

        style_block = (
            await self.style_service.render_examples_block(
                style_query, scope="proactive"
            )
            if self.style_service
            else self.pet_domain.render_style_examples(style_query, scope="proactive")
        )

        pre = (
            self.config.json_output_prompt
            + "\n"
            + style_block
            + "\n"
            + self.config.persona_reinforcement
            + "\n"
            + prompt
        )
        messages.append({"role": "system", "content": pre})
        messages.append({"role": "user", "content": user_stub})

        content = await self.llm.chat_json(
            messages,
            max_tokens=max_tokens,
            temperature=self.config.autonomous_temperature,
        )

        try:
            data = json.loads(content)
            reply = (data.get("reply") or "").strip()
            image_prompt = (data.get("image_prompt") or "").strip()
        except json.JSONDecodeError:
            log.warning("autonomous LLM returned non-JSON, skipping: %r", content[:200])
            return None

        if not reply:
            log.warning("autonomous LLM returned empty reply, skipping")
            return None

        now = time.time()

        def _mutator(state: dict) -> dict:
            out = dict(state)
            out["last_update_ts"] = now
            if set_last_proactive:
                out["last_proactive_ts"] = now
            if extra_state:
                out.update(extra_state)
            return out

        img_key: str | None = None
        if gen_image and self.config.image_model:
            try:
                image_bytes = await self.llm.generate_image(image_prompt or reply)
                if image_bytes:
                    img_key = await self.feishu.upload_image(image_bytes)
            except Exception:
                log.exception("image generation failed for pet %d", pet_id)

        if as_card and self.config.card_enabled and self.card_service is not None:
            await self.card_service.send_persistent_card(
                pet_id,
                chat_id,
                reply,
                img_key=img_key,
            )
        else:
            await self.feishu.send_text(chat_id, reply)

        await self.message_repo.append_message(pet_id, "assistant", reply)
        new_state = await self.pet_repo.mutate_state(pet_id, _mutator)

        log.info(
            "pet %d %s: reply=%r",
            pet_id,
            log_label,
            reply[:100],
        )
        return reply, new_state

    async def evaluate_proactive_speaking(
        self, pet_id: int, state: dict
    ) -> tuple[str, str] | None:
        """评估最近群聊进展是否值得主动开口。返回 (trigger_reason, topic) 或 None。"""
        # 获取最近 15 条消息（包含人类消息与自身回复，保留完整上下文）
        recent_rows = await self.message_repo.recent_messages(pet_id, limit=15)
        human_msgs = [r for r in recent_rows if r.get("role") == "user"]
        if len(human_msgs) < self.config.proactive_min_new_msgs:
            return None

        last_proactive_ts = float(state.get("last_proactive_ts", 0) or 0)
        last_reply_ts = float(state.get("last_reply_ts", 0) or 0)
        last_activity_ts = max(last_proactive_ts, last_reply_ts)

        # 检查上次宠物开口（主动发言或直接回复）后，群里是否有足够的新人类消息
        new_human_msgs = [
            m for m in human_msgs if float(m.get("ts", 0)) > last_activity_ts
        ]
        if len(new_human_msgs) < self.config.proactive_min_new_msgs:
            return None

        # 构建真实的群聊对话片段（包含群友发言和宠物之前的发言，防止上下文割裂或误以为未回答）
        lines = []
        pet_name = getattr(self.config, "pet_name", "大肥鱼")
        for m in recent_rows[-8:]:
            role = m.get("role")
            if role == "assistant":
                speaker = pet_name
            else:
                speaker = m.get("sender_name") or "群友"
            lines.append(f"[{speaker}]: {m.get('content')}")
        recent_dialogue = "\n".join(lines)

        eval_messages = [
            {"role": "system", "content": self.config.proactive_eval_prompt},
            {
                "role": "user",
                "content": f"最近群聊对话片段：\n{recent_dialogue}\n\n上次主动话题：{state.get('last_proactive_topic', '无')}",
            },
        ]
        try:
            content = await self.llm.chat_json(eval_messages, max_tokens=250, temperature=0.3)
            data = json.loads(content)
            if not data.get("should_speak"):
                return None
            reason = str(data.get("reason") or "").strip()
            topic = str(data.get("topic") or "").strip()
            # 同一话题如果前次无人理，且当前提取的核心话题仍旧相同，禁止追问
            last_topic = state.get("last_proactive_topic", "")
            if not state.get("last_proactive_answered", True) and topic and topic == last_topic:
                return None
            return (reason, topic)
        except Exception:
            log.exception("evaluate proactive speak failed for pet %d", pet_id)
            return None

    async def proactive_speak(
        self, pet_id: int, chat_id: str, trigger: str, topic: str = ""
    ) -> tuple[str, dict] | None:
        extra = {
            "last_proactive_topic": topic,
            "last_proactive_answered": False,
        }
        return await self.autonomous_speak(
            pet_id,
            chat_id,
            self.config.proactive_prompt.format(trigger=trigger),
            f"[系统主动触发] 触发理由：{trigger}",
            f"PROACTIVE trigger={trigger[:60]!r}",
            max_tokens=self.config.reply_max_tokens,
            set_last_proactive=True,
            extra_state=extra,
            as_card=False,
            style_query=trigger,
        )

    async def scheduled_speak(
        self,
        pet_id: int,
        chat_id: str,
        event: dict,
        date_key: str,
        *,
        mark_date: bool = True,
    ) -> tuple[str, dict] | None:
        prompt = self.config.scheduled_event_prompt.format(
            event_name=event["name"],
            scheduled_hour=event["hour"],
            instruction=event["instruction"],
        )
        extra_prompt = (event.get("extra_prompt") or "").strip()
        if extra_prompt:
            prompt = prompt + "\n" + extra_prompt
        return await self.autonomous_speak(
            pet_id,
            chat_id,
            prompt,
            f"[系统定时触发] 到了今天的{event['name']}窗口",
            f"SCHEDULED {event['kind']} date={date_key}",
            max_tokens=self.config.scheduled_max_tokens,
            set_last_proactive=True,
            extra_state={event["state_key"]: date_key} if mark_date else None,
            as_card=True,
            gen_image=bool(event.get("gen_image")),
        )

    async def tick_pet(
        self,
        pet_id: int,
        chat_id: str,
        current: dict | None = None,
        now: float | None = None,
    ) -> bool:
        now = time.time() if now is None else now
        if current is None:
            current = await self.pet_repo.load_pet_state(pet_id)

        # 1. 检查定时事件发送窗口 (梦境 / 日记)
        fired_scheduled = False
        for _ in range(len(self.config.scheduled_events)):
            scheduled = self.scheduled_event_due(current, now)
            if scheduled is None:
                break
            event, date_key = scheduled
            # 检查是否有真实人类素材：如果群里连 1 条真实人类发言都没有，跳过本窗口
            recent_rows = await self.message_repo.recent_messages(pet_id, limit=20)
            human_msgs = [r for r in recent_rows if r.get("role") == "user"]
            if not human_msgs:
                log.info(
                    "pet %d scheduled event %s skipped: insufficient human material",
                    pet_id,
                    event["kind"],
                )
                current = await self.pet_repo.mutate_state(
                    pet_id, lambda s: {**s, event["state_key"]: date_key}
                )
                continue

            result = await self.scheduled_speak(pet_id, chat_id, event, date_key)
            fired_scheduled = True
            if result is None:
                break
            _, current = result
        if fired_scheduled:
            return True

        # 2. 检查静默时段与冷却
        if self.state_domain.in_quiet_hours(now):
            return False

        # 若当前有直接回复正在排队或进行中，跳过主动发言
        if getattr(self, "runtime", None) is not None:
            if self.runtime.chat_pending_messages.get(pet_id):
                return False
            if pet_id in self.runtime.chat_locks and self.runtime.chat_locks[pet_id].locked():
                return False

        last_proactive_ts = float(current.get("last_proactive_ts", 0) or 0)
        last_reply_ts = float(current.get("last_reply_ts", 0) or 0)
        cooldown = self.config.proactive_cooldown_sec
        if not current.get("last_proactive_answered", True):
            cooldown = getattr(self.config, "unanswered_cooldown_sec", cooldown)
        reply_cooldown = float(getattr(self.config, "proactive_reply_cooldown_sec", 60.0) or 60.0)
        if now - last_proactive_ts < cooldown or now - last_reply_ts < reply_cooldown:
            return False

        # 4. 基于新人类素材的主动发言价值评估
        eval_result = await self.evaluate_proactive_speaking(pet_id, current)
        if eval_result is None:
            return False

        reason, topic = eval_result
        if getattr(self, "runtime", None) is not None:
            async with self.runtime.chat_lock(pet_id):
                await self.proactive_speak(pet_id, chat_id, reason, topic)
        else:
            await self.proactive_speak(pet_id, chat_id, reason, topic)
        return True

    async def tick_all_pets(self) -> None:
        try:
            await self.system_repo.clean_old_events()
        except Exception:
            log.exception("clean_old_events failed")

        await self.observer_service.flush_all()

        now = time.time()
        rows = await self.pet_repo.load_all_pets()
        for row in rows:
            pet_id = row["id"]
            chat_id = row["chat_id"]
            stored = self.pet_repo.decode_state(row["state_json"])
            try:
                await self.tick_pet(pet_id, chat_id, current=stored, now=now)
            except Exception:
                log.exception("tick failed for pet %d", pet_id)

    async def run_loop(self) -> None:
        log.info(
            "autonomous_loop start tick=%ds cooldown=%ds quiet=%s quiet_weekends=%s tz_offset=%s",
            self.config.tick_interval_sec,
            self.config.proactive_cooldown_sec,
            self.config.quiet_hours,
            self.config.quiet_weekends,
            self.config.proactive_tz_offset_hours,
        )
        while True:
            try:
                await asyncio.sleep(self.config.tick_interval_sec)
                await self.tick_all_pets()
            except asyncio.CancelledError:
                log.info("autonomous_loop cancelled")
                raise
            except Exception:
                log.exception("autonomous tick crashed, loop continues")
