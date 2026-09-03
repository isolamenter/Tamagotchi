from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from config import AppConfig
from domain.pet import PetDomain
from domain.gameplay import GameplayDomain
from domain.state import StateDomain
from integrations.feishu_client import FeishuClient
from integrations.llm_client import LLMClient
from repositories.message_repo import MessageRepository
from repositories.pet_repo import PetRepository
from repositories.system_repo import SystemRepository
from runtime import RuntimeState
from services.memory_service import MemoryService
from services.observer_service import ObserverService
from services.card_service import CardService
from services.style_service import StyleService

log = logging.getLogger("tamagotchi")


class ReplyService:
    REPLY_MODES = frozenset({"reaction", "normal", "substantive"})

    def __init__(
        self,
        config: AppConfig,
        runtime: RuntimeState,
        state_domain: StateDomain,
        gameplay_domain: GameplayDomain,
        pet_domain: PetDomain,
        pet_repo: PetRepository,
        message_repo: MessageRepository,
        system_repo: SystemRepository,
        memory_service: MemoryService,
        observer_service: ObserverService,
        card_service: CardService,
        feishu: FeishuClient,
        llm: LLMClient,
        style_service: StyleService | None = None,
    ):
        self.config = config
        self.runtime = runtime
        self.state_domain = state_domain
        self.gameplay_domain = gameplay_domain
        self.pet_domain = pet_domain
        self.pet_repo = pet_repo
        self.message_repo = message_repo
        self.system_repo = system_repo
        self.memory_service = memory_service
        self.observer_service = observer_service
        self.card_service = card_service
        self.feishu = feishu
        self.llm = llm
        self.style_service = style_service

    @classmethod
    def parse_llm_content(cls, content: str) -> tuple[str, str, str, bool]:
        """Parse normal, double-encoded, or partially malformed JSON safely."""
        candidate = (content or "").strip()
        for _ in range(2):
            try:
                data = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                break
            if isinstance(data, dict):
                reply_mode = str(data.get("reply_mode") or "").strip().lower()
                if reply_mode not in cls.REPLY_MODES:
                    reply_mode = "unknown"
                return (
                    str(data.get("reply") or "").strip(),
                    str(data.get("speaker_name") or "").strip(),
                    reply_mode,
                    True,
                )
            if isinstance(data, str):
                candidate = data.strip()
                continue
            break

        # Providers occasionally return a valid reply field followed by a
        # malformed optional field. Recover only safe quoted scalar values so
        # the raw JSON object is never sent to Feishu.
        match = re.search(
            r'"reply"\s*:\s*"((?:\\.|[^"\\])*)"', candidate, re.DOTALL
        )
        if match:
            encoded_reply = match.group(1)
            try:
                reply = json.loads(f'"{encoded_reply}"')
            except json.JSONDecodeError:
                reply = encoded_reply.replace(r'\"', '"').replace(r"\n", "\n")
            mode_match = re.search(
                r'"reply_mode"\s*:\s*"([^"\\]*)"', candidate
            )
            reply_mode = (
                mode_match.group(1).strip().lower() if mode_match else "unknown"
            )
            if reply_mode not in cls.REPLY_MODES:
                reply_mode = "unknown"
            return str(reply).strip(), "", reply_mode, False
        return candidate, "", "unknown", False

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

        style_block = (
            await self.style_service.render_examples_block(
                user_text, scope="reply", history=hist
            )
            if self.style_service
            else self.pet_domain.render_style_examples(user_text, scope="reply")
        )

        pre_user_system = (
            self.state_domain.render_state(current_state, include_vibe=False)
            + "\n"
            + self.config.json_output_prompt
            + "\n"
            + style_block
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
            temperature=self.config.reply_temperature,
        )

        reply, speaker_name, reply_mode, structured = self.parse_llm_content(content)
        if not structured:
            if reply and reply != content.strip():
                log.warning("recovered reply from malformed JSON: %r", content[:200])
            else:
                log.warning("LLM returned non-JSON: %r", content[:200])
        if reply_mode == "unknown":
            log.warning("LLM returned missing or invalid reply_mode: %r", content[:200])

        if not reply:
            reply = self.config.fallback_replies["empty_llm"]

        log.info(
            "pet %d reply shape: mode=%s chars=%d",
            pet_id,
            reply_mode,
            len(reply),
        )

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
            out["last_social_ts"] = time.time()
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

    async def is_direct_to_bot(self, chat_type: str, mentions: list[dict]) -> bool:
        # A p2p message is intrinsically addressed to the bot.  In groups we
        # require a real @mention; when bot identity cannot be fetched, fail
        # closed and keep observing instead of unexpectedly interrupting chat.
        if chat_type == "p2p":
            return True
        try:
            bot_open_id = await self.feishu.get_bot_open_id()
        except Exception:
            log.exception("get bot open_id failed; group message stays observer-only")
            return False
        return any((mention.get("id") or {}).get("open_id") == bot_open_id for mention in mentions)

    async def _send_response(
        self, chat_type: str, chat_id: str, message_id: str, text: str
    ) -> None:
        if chat_type == "p2p":
            await self.feishu.send_text(chat_id, text)
        else:
            await self.feishu.reply_text(message_id, text)

    async def _maybe_compress(self, pet_id: int) -> None:
        if await self.message_repo.count_unsummarized(pet_id) > self.config.compress_threshold:
            asyncio.create_task(self.memory_service.compress_pet_memory(pet_id))

    async def _attach_first_need_cta(
        self, pet_id: int, chat_id: str, state: dict
    ) -> None:
        """Send an unannounced need card once; ordinary dialogue never settles it."""
        if not self.config.card_enabled or not self.config.gameplay_enabled:
            return
        now = time.time()
        async with self.runtime.state_lock(pet_id):
            current = await self.pet_repo.load_pet_state(pet_id)
            current = self.gameplay_domain.expired_need_cleared(current, now)
            need = self.gameplay_domain.current_need(current, now)
            if not need or need.get("announced_card_id"):
                return
            text = f"{need.get('title', '需要照料')}：{need.get('description', '')}"
            try:
                card_id, _ = await self.card_service.send_persistent_card(
                    pet_id, chat_id, text, current, mode="need"
                )
            except Exception:
                # Keep active_need unannounced.  A later direct message or tick
                # can retry without manufacturing a new need.
                log.exception("failed to attach need CTA for pet %d", pet_id)
                return
            current["active_need"] = {**need, "announced_card_id": card_id}
            await self.pet_repo.update_pet_state(pet_id, current)

    async def _caption_image_key(self, message_id: str, image_key: str) -> str | None:
        """下载图片 → vision 一句话描述 → 字节即弃。任一步失败返回 None。"""
        image_key = (image_key or "").strip()
        if not message_id or not image_key:
            return None
        try:
            image_bytes = await self.feishu.download_image(message_id, image_key)
        except Exception:
            log.exception("download image failed")
            return None
        if not image_bytes:
            return None
        return await self.llm.describe_image(image_bytes)

    async def _caption_image(self, message_id: str, msg: dict) -> str | None:
        """单图消息的 caption：从 content 取 image_key。任一步失败返回 None。"""
        try:
            content = json.loads(msg.get("content") or "{}")
        except json.JSONDecodeError:
            return None
        return await self._caption_image_key(message_id, content.get("image_key") or "")

    async def _parse_post(self, message_id: str, msg: dict) -> str | None:
        """富文本抽文本 + 内嵌图走同一套 caption。空返回 None。"""
        try:
            content = json.loads(msg.get("content") or "{}")
        except json.JSONDecodeError:
            return None
        paragraphs = content.get("content") or []
        if not isinstance(paragraphs, list):
            return None
        parts: list[str] = []
        title = (content.get("title") or "").strip()
        if title:
            parts.append(title)
        for para in paragraphs:
            if not isinstance(para, list):
                continue
            for el in para:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a"):
                    parts.append(el.get("text") or "")
                elif tag == "at":
                    name = (el.get("user_name") or el.get("text") or "").strip()
                    if name:
                        parts.append("@" + name)
                elif tag == "img":
                    caption = await self._caption_image_key(
                        message_id, el.get("image_key") or ""
                    )
                    parts.append(
                        f"【发了一张图：{caption}】" if caption
                        else "【发了一张图，但没看清】"
                    )
        text = re.sub(r"\s+", " ", " ".join(parts)).strip()
        return text or None

    async def _mentions_with_names(self, mentions: list[dict]) -> list[dict]:
        """mentions 缺 name 时用 open_id 回填缓存名，保证 @目标不丢。"""
        enriched: list[dict] = []
        for mention in mentions or []:
            if not isinstance(mention, dict):
                continue
            mention = dict(mention)
            if not (mention.get("name") or "").strip():
                open_id = ((mention.get("id") or {}).get("open_id") or "").strip()
                if open_id:
                    try:
                        name = await self.resolve_user_name(open_id)
                    except Exception:
                        name = ""
                    if name:
                        mention["name"] = name
            enriched.append(mention)
        return enriched

    def _append_missing_mentions(
        self, user_text: str, mentions: list[dict], sender_open_id: str
    ) -> str:
        """mentions 里出现、文本里没有的名字，补成后缀 @X @Y。

        飞书只回传 name 不保证文本带占位符时用；跳过发送者自己。
        """
        missing = []
        for mention in mentions or []:
            if not isinstance(mention, dict):
                continue
            name = (mention.get("name") or "").strip()
            if not name or f"@{name}" in user_text:
                continue
            open_id = ((mention.get("id") or {}).get("open_id") or "").strip()
            if open_id and open_id == sender_open_id:
                continue
            missing.append("@" + name)
        if missing:
            user_text = (user_text + " " + " ".join(missing)).strip()
        return user_text

    async def _buffer_observer_text(
        self, pet_id: int, text: str, sender_name: str, sender_open_id: str
    ) -> None:
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

    async def handle_message_event(self, event: dict) -> None:
        msg = event.get("message") or {}
        message_id = msg.get("message_id")
        chat_id = msg.get("chat_id")
        if not message_id or not chat_id:
            return

        mentions = msg.get("mentions") or []
        chat_type = msg.get("chat_type") or "group"
        sender_open_id = (
            ((event.get("sender") or {}).get("sender_id") or {}).get("open_id")
        ) or ""
        sender_name = (
            await self.resolve_user_name(sender_open_id) if sender_open_id else "群友"
        )

        is_direct = await self.is_direct_to_bot(chat_type, mentions)
        msg_type = msg.get("message_type")

        if msg_type == "image":
            caption = await self._caption_image(message_id, msg)
            # 看完即弃：字节已在 _caption_image 内释放，这里只留一句话。
            user_text = (
                f"【发了一张图：{caption}】" if caption
                else "【发了一张图，但没看清】"
            )
        elif msg_type == "post":
            text = await self._parse_post(message_id, msg)
            if not text:
                if is_direct:
                    log.info("ignoring empty post message (direct, sent non_text fallback)")
                    await self._send_response(chat_type, chat_id, message_id, self.config.fallback_replies["non_text"])
                else:
                    log.info("ignoring empty post message (observer-only, no @)")
                return
            mentions = await self._mentions_with_names(mentions)
            user_text = self.pet_domain.clean_text(text, mentions)
            user_text = self._append_missing_mentions(user_text, mentions, sender_open_id)
            if not user_text:
                if is_direct:
                    await self._send_response(chat_type, chat_id, message_id, self.config.fallback_replies["empty_text"])
                return
        elif msg_type == "text":
            try:
                content = json.loads(msg.get("content") or "{}")
            except json.JSONDecodeError:
                if is_direct:
                    log.warning("bad content json: %r", msg.get("content"))
                return
            mentions = await self._mentions_with_names(mentions)
            user_text = self.pet_domain.clean_text(content.get("text", ""), mentions)
            user_text = self._append_missing_mentions(user_text, mentions, sender_open_id)
            if not user_text:
                if is_direct:
                    await self._send_response(chat_type, chat_id, message_id, self.config.fallback_replies["empty_text"])
                return
        elif is_direct:
            log.info("ignoring msg_type=%r (direct, sent non_text fallback)", msg_type)
            await self._send_response(chat_type, chat_id, message_id, self.config.fallback_replies["non_text"])
            return
        else:
            log.info("ignoring msg_type=%r (observer-only, no @)", msg_type)
            return

        if not is_direct:
            pet_id = await self.pet_repo.find_pet(chat_id)
            if pet_id is None:
                return
            await self._buffer_observer_text(
                pet_id, user_text, sender_name, sender_open_id
            )
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
            await self._send_response(chat_type, chat_id, message_id, self.config.fallback_replies["quiet_hours"])
            log.info("pet %d @ during quiet hours, sent sleeping reply", pet_id)
            await self._maybe_compress(pet_id)
            return

        if self.config.reply_min_interval_sec > 0:
            now = time.time()
            try:
                db_ts = float((await self.pet_repo.load_pet_state(pet_id)).get("last_reply_ts", 0.0))
            except (TypeError, ValueError):
                db_ts = 0.0
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
        reply_state = await self.pet_repo.load_pet_state(pet_id)
        try:
            reply, reply_state, learned_name = await self.call_llm_with_memory(
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

        try:
            await self._send_response(chat_type, chat_id, message_id, reply)
        except Exception:
            # The conversation history must reflect what users actually saw.
            # Release the in-memory gate so a transport outage does not turn
            # into a phantom reply cooldown.
            self.runtime.reply_gate.pop(pet_id, None)
            log.exception("reply delivery failed for pet %d", pet_id)
            return
        await self.message_repo.append_message(pet_id, "assistant", reply)
        await self.mark_replied(pet_id)
        await self._attach_first_need_cta(pet_id, chat_id, reply_state)

        if learned_name:
            await self._send_response(
                chat_type, chat_id, message_id,
                self.config.fallback_replies["name_learned"].format(name=learned_name),
            )

        await self._maybe_compress(pet_id)
