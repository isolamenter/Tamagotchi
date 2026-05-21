from __future__ import annotations

import asyncio
import json
import logging
import random
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
from services.memory_service import MemoryService
from services.observer_service import ObserverService

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
    ):
        self.config = config
        self.state_domain = state_domain
        self.card_domain = card_domain
        self.pet_domain = pet_domain
        self.pet_repo = pet_repo
        self.message_repo = message_repo
        self.system_repo = system_repo
        self.memory_service = memory_service
        self.observer_service = observer_service
        self.feishu = feishu
        self.llm = llm

    def should_tick_speak(
        self, state: dict, last_proactive_ts: float, now: float
    ) -> str | None:
        if self.state_domain.in_quiet_hours(now):
            return None
        if now - last_proactive_ts < self.config.proactive_cooldown_sec:
            return None
        if state["hunger"] >= self.config.hunger_trigger:
            return self.config.proactive_trigger_templates["hunger"].format(
                hunger=round(state["hunger"])
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
        date_key, hour = self.state_domain.local_date_hour(now)
        for event in self.config.scheduled_events:
            if hour >= event["hour"] and state.get(event["state_key"]) != date_key:
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
    ) -> tuple[str, dict] | None:
        history, current_state = await self.pet_repo.load_pet_context(pet_id)

        recall_block = await self.memory_service.build_recall_block(
            pet_id, query="", k_recent=5
        )
        system_content = self.config.system_prompt + recall_block
        messages = self.pet_domain.base_messages(system_content, history)

        pre = (
            self.state_domain.render_state(current_state)
            + "\n"
            + self.config.json_output_prompt
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
            temperature=0.95,
        )

        try:
            data = json.loads(content)
            reply = (data.get("reply") or "").strip()
            delta = data.get("state_delta") or {}
            if not isinstance(delta, dict):
                delta = {}
        except json.JSONDecodeError:
            log.warning("autonomous LLM returned non-JSON, skipping: %r", content[:200])
            return None

        if not reply:
            log.warning("autonomous LLM returned empty reply, skipping")
            return None

        now = time.time()
        new_state = self.state_domain.apply_delta(current_state, delta)
        new_state["last_update_ts"] = now
        if set_last_proactive:
            new_state["last_proactive_ts"] = now
        if extra_state:
            new_state.update(extra_state)

        if as_card and self.config.card_enabled:
            await self.feishu.send_card(
                chat_id,
                self.card_domain.build_pet_card(
                    pet_id, reply, new_state, action_keys=card_actions
                ),
            )
        else:
            await self.feishu.send_text(chat_id, reply)

        await self.message_repo.append_message(pet_id, "assistant", reply)
        await self.pet_repo.update_pet_state(pet_id, new_state)

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
        action = event.get("card_action")
        return await self.autonomous_speak(
            pet_id,
            chat_id,
            self.config.scheduled_event_prompt.format(
                event_name=event["name"],
                scheduled_hour=event["hour"],
                instruction=event["instruction"],
            ),
            self.config.scheduled_user_stub_template.format(event_name=event["name"]),
            f"SCHEDULED {event['kind']} date={date_key}",
            max_tokens=self.config.scheduled_max_tokens,
            extra_state={event["state_key"]: date_key} if mark_date else None,
            as_card=True,
            card_actions=[action] if action else [],
        )

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
            scheduled = self.scheduled_event_due(current, now)
            if scheduled is not None:
                event, date_key = scheduled
                try:
                    await self.scheduled_speak(pet_id, chat_id, event, date_key)
                except Exception:
                    log.exception("scheduled speak failed for pet %d", pet_id)
                continue
            last_proactive_ts = float(stored.get("last_proactive_ts", 0))
            trigger = self.should_tick_speak(current, last_proactive_ts, now)
            if trigger is None:
                continue
            try:
                await self.proactive_speak(pet_id, chat_id, trigger)
            except Exception:
                log.exception("proactive speak failed for pet %d", pet_id)

    async def run_loop(self) -> None:
        log.info(
            "autonomous_loop start tick=%ds cooldown=%ds quiet=%s tz_offset=%s",
            self.config.tick_interval_sec,
            self.config.proactive_cooldown_sec,
            self.config.quiet_hours,
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

