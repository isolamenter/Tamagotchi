from __future__ import annotations

import asyncio
import json
import logging
import random
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
from services.gameplay_service import GameplayService
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
        gameplay_domain: GameplayDomain,
        card_domain: CardDomain,
        pet_domain: PetDomain,
        pet_repo: PetRepository,
        message_repo: MessageRepository,
        system_repo: SystemRepository,
        gameplay_service: GameplayService,
        memory_service: MemoryService,
        observer_service: ObserverService,
        feishu: FeishuClient,
        llm: LLMClient,
        card_service: CardService | None = None,
        style_service: StyleService | None = None,
    ):
        self.config = config
        self.state_domain = state_domain
        self.gameplay_domain = gameplay_domain
        self.card_domain = card_domain
        self.pet_domain = pet_domain
        self.pet_repo = pet_repo
        self.message_repo = message_repo
        self.system_repo = system_repo
        self.gameplay_service = gameplay_service
        self.card_service = card_service
        self.style_service = style_service
        self.memory_service = memory_service
        self.observer_service = observer_service
        self.feishu = feishu
        self.llm = llm

    def should_tick_speak(
        self, state: dict, last_active_ts: float, now: float
    ) -> str | None:
        if self.state_domain.in_quiet_hours(now):
            return None
        if now - last_active_ts < self.config.proactive_cooldown_sec:
            return None
        if state["satiety"] <= self.config.satiety_trigger:
            return self.config.proactive_trigger_templates["satiety"].format(
                satiety=round(state["satiety"])
            )
        if state["mood"] <= self.config.mood_trigger:
            return self.config.proactive_trigger_templates["mood"].format(
                mood=round(state["mood"])
            )
        if state["energy"] <= self.config.energy_trigger:
            return self.config.proactive_trigger_templates["energy"].format(
                energy=round(state["energy"])
            )
        if state["curiosity"] <= self.config.curiosity_trigger:
            return self.config.proactive_trigger_templates["curiosity"].format(
                curiosity=round(state["curiosity"])
            )
        if state["affection"] <= self.config.affection_trigger:
            return self.config.proactive_trigger_templates["affection"].format(
                affection=round(state["affection"])
            )
        if random.random() < self.config.spontaneous_prob:
            return self.config.proactive_trigger_templates["spontaneous"]
        return None

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
        card_actions: list[str] | None = None,
        gen_image: bool = False,
        style_query: str = "",
    ) -> tuple[str, dict] | None:
        history, current_state = await self.pet_repo.load_pet_context(pet_id)

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
            self.state_domain.render_state(current_state)
            + "\n"
            + self.config.json_output_prompt
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
            # 主动文本只读 state；卡片按钮才允许修改五维玩法状态。
            out = dict(state)
            out["last_update_ts"] = now
            if set_last_proactive:
                out["last_proactive_ts"] = now
            if extra_state:
                out.update(extra_state)
            return out

        # 卡片用展示快照渲染；权威值在发送成功后经 mutate_state 落库（保持「先发后写」）。
        display_state = _mutator(dict(current_state))

        img_key: str | None = None
        if gen_image and self.config.image_model:
            try:
                image_bytes = await self.llm.generate_image(image_prompt or reply)
                if image_bytes:
                    img_key = await self.feishu.upload_image(image_bytes)
            except Exception:
                log.exception("dream image pipeline failed for pet %d", pet_id)

        if as_card and self.config.card_enabled:
            mode = "scheduled" if card_actions is not None else "free"
            if self.card_service is None:
                raise RuntimeError("card service is required for autonomous cards")
            await self.card_service.send_persistent_card(
                pet_id,
                chat_id,
                reply,
                display_state,
                mode=mode,
                action_keys=card_actions,
                img_key=img_key,
            )
        else:
            await self.feishu.send_text(chat_id, reply)

        await self.message_repo.append_message(pet_id, "assistant", reply)
        new_state = await self.pet_repo.mutate_state(pet_id, _mutator)

        log.info(
            "pet %d %s: reply=%r state=%s",
            pet_id,
            log_label,
            reply[:100],
            {key: round(new_state.get(key, 0)) for key in self.config.state_numeric_keys},
        )
        return reply, new_state

    async def proactive_speak(
        self, pet_id: int, chat_id: str, trigger: str
    ) -> tuple[str, dict] | None:
        return await self.autonomous_speak(
            pet_id,
            chat_id,
            self.config.proactive_prompt.format(trigger=trigger),
            self.config.proactive_user_stub_template,
            f"PROACTIVE trigger={trigger[:60]!r}",
            max_tokens=self.config.reply_max_tokens,
            set_last_proactive=True,
            as_card=True,
            style_query=trigger,
        )

    async def need_speak(
        self, pet_id: int, chat_id: str, current: dict | None = None, now: float | None = None
    ) -> tuple[dict, dict] | None:
        now = time.time() if now is None else now
        if not self.config.card_enabled or not self.config.gameplay_enabled:
            return None
        state, created = await self.gameplay_service.maybe_create_need(pet_id, now)
        need = created or self.gameplay_domain.current_need(state, now)
        if not need or need.get("announced_card_id"):
            return None
        text = f"{need.get('title', '需要照料')}：{need.get('description', '')}"
        if self.card_service is None:
            raise RuntimeError("card service is required for need cards")
        card_id, _ = await self.card_service.send_persistent_card(
            pet_id, chat_id, text, state, mode="need"
        )
        state = await self.pet_repo.mutate_state(
            pet_id,
            lambda s: {
                **s,
                "active_need": {**s["active_need"], "announced_card_id": card_id},
            },
        )
        await self.message_repo.append_message(pet_id, "assistant", text)
        log.info(
            "pet %d NEED %s severity=%s state=%s",
            pet_id,
            need.get("kind"),
            need.get("severity"),
            {key: round(state.get(key, 0)) for key in self.config.state_numeric_keys},
        )
        return need, state

    async def scheduled_speak(
        self,
        pet_id: int,
        chat_id: str,
        event: dict,
        date_key: str,
        *,
        mark_date: bool = True,
    ) -> tuple[str, dict] | None:
        action = event.get("card_action")
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
            self.config.scheduled_user_stub_template.format(event_name=event["name"]),
            f"SCHEDULED {event['kind']} date={date_key}",
            max_tokens=self.config.scheduled_max_tokens,
            set_last_proactive=True,
            extra_state={event["state_key"]: date_key} if mark_date else None,
            as_card=True,
            card_actions=[action] if action else [],
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
        quiet = self.state_domain.in_quiet_hours(now)
        if self.gameplay_service is not None:
            current = await self.gameplay_service.sync_need_clock(pet_id, quiet, now)

        # 可能同时有多个到期定时事件（如进程停了一整天，梦境+日记都欠着）：
        # 逐个补发，每次用返回的 new_state 刷新 current（含 date_key 标记），
        # 迭代上限 = 事件总数，防死循环。任一触发则本 tick 不再走主动发言。
        fired_scheduled = False
        for _ in range(len(self.config.scheduled_events)):
            scheduled = self.scheduled_event_due(current, now)
            if scheduled is None:
                break
            event, date_key = scheduled
            result = await self.scheduled_speak(pet_id, chat_id, event, date_key)
            fired_scheduled = True
            if result is None:
                break
            _, current = result
        if fired_scheduled:
            return True
        if quiet:
            return False
        need_result = await self.need_speak(pet_id, chat_id, current=current, now=now)
        if need_result is not None:
            return True
        last_active_ts = max(
            float(current.get("last_proactive_ts", 0) or 0),
            float(current.get("last_free_card_ts", 0) or 0),
        )
        trigger = self.should_tick_speak(current, last_active_ts, now)
        if trigger is None:
            return False
        await self.proactive_speak(pet_id, chat_id, trigger)
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
            current = self.state_domain.decay_state(stored, now, pet_id)
            try:
                await self.tick_pet(pet_id, chat_id, current=current, now=now)
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
