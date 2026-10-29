from __future__ import annotations

import asyncio
import json
import logging
import time

from dafeiyu.domain.consolidation import DiaryNotReady
from dafeiyu.domain.reply import GeneratedReply
from dafeiyu.integrations.feishu_client import DeliveryRejected

log = logging.getLogger("dafeiyu")


class ScheduledService:
    """Independent daily jobs; only final delivery holds the conversation lock."""

    def __init__(
        self, config, runtime, state_domain, conversations, system, identities, autonomous
    ):
        self.config, self.runtime, self.state_domain = config, runtime, state_domain
        self.conversations, self.system, self.identities = conversations, system, identities
        self.autonomous = autonomous
        self.locks: dict[tuple[int, str], asyncio.Lock] = {}
        self.dream_limit = asyncio.Semaphore(1)

    def _lock(self, cid: int, kind: str) -> asyncio.Lock:
        return self.locks.setdefault((cid, kind), asyncio.Lock())

    def _in_window(self, event: dict, date: str, now: float) -> bool:
        local = self.state_domain.local_time(now)
        return (
            not self.state_domain.is_weekend_rest(now)
            and self.state_domain.local_date_hour(now)[0] == date
            and 0
            <= local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec - event["hour"] * 3600
            <= self.config.scheduled_grace_sec
        )

    async def _load(self, cid: int, kind: str, date: str) -> dict:
        raw = await self.system.get_sys_cache(f"scheduled:{cid}:{kind}")
        job = json.loads(raw) if raw else None
        if job is None or job["date"] != date:
            return {"date": date, "until_id": 0, "last_attempt": 0, "status": "pending"}
        return job

    async def status(self, cid: int) -> dict:
        result = {}
        for kind in ("dream", "diary"):
            raw = await self.system.get_sys_cache(f"scheduled:{cid}:{kind}")
            if raw:
                job = json.loads(raw)
                result[kind] = {
                    key: job[key] for key in ("date", "status", "last_attempt", "until_id")
                }
        return result

    async def _save(self, cid: int, kind: str, job: dict) -> None:
        await self.system.set_sys_cache(
            f"scheduled:{cid}:{kind}", json.dumps(job, ensure_ascii=False)
        )

    def _skip(self, cid: int, kind: str, reason: str) -> tuple[bool, bool]:
        log.info("scheduled skipped conversation=%d kind=%s reason=%s", cid, kind, reason)
        return True, False

    async def _prepare(
        self, cid: int, event: dict, date: str, job: dict, *, automatic=False
    ) -> bool:
        if event["kind"] == "dream":
            # Bound expensive preparation without holding any conversation lock.
            async with self.dream_limit:
                if automatic:
                    state = await self.conversations.load_conversation_state(cid)
                    _, fresh = await self.autonomous.fresh_activity(
                        cid, state, job["until_id"], time.time()
                    )
                    if (
                        not self._in_window(event, date, time.time())
                        or self.runtime.chat_pending_messages.get(cid)
                        or self.autonomous.cooling_down(state, time.time())
                        or not fresh
                    ):
                        self._skip(cid, event["kind"], "preparation_admission_changed")
                        return False
                await self._prepare_content(cid, event, date, job)
        else:
            await self._prepare_content(cid, event, date, job)
        return True

    async def _prepare_content(self, cid: int, event: dict, date: str, job: dict) -> None:
        kind = event["kind"]
        if kind == "diary":
            diary = await self.identities.diary(cid, date)
            if diary is None:
                raise DiaryNotReady("今天的每日整理尚未产生日记，或没有合适内容")
            job.update(text=diary["text"], mode="normal", image_key="", example_ids=[])
        elif not job.get("text"):
            prompt = (
                self.config.scheduled_event_prompt.format(
                    event_name=event["name"],
                    scheduled_hour=event["hour"],
                    instruction=event["instruction"],
                )
                + "\n"
                + event.get("extra_prompt", "")
            )
            reply, ids = await asyncio.wait_for(
                self.autonomous.prepare_reply(
                    cid, prompt, event["instruction"], max_tokens=self.config.scheduled_max_tokens
                ),
                timeout=self.config.settings["autonomous"]["scheduled_generation_timeout_sec"],
            )
            job.update(
                text=reply.text,
                mode=reply.mode,
                image_prompt=reply.image_prompt,
                image_key="",
                example_ids=list(ids),
            )
            # Save the text before optional image work, including across cancellation/restart.
            await self._save(cid, kind, job)
        if kind == "dream" and not job.get("image_done"):
            if event.get("gen_image") and self.config.card_enabled and self.config.image_model:

                async def image():
                    content = await self.autonomous.llm.generate_image(
                        job.get("image_prompt") or job["text"]
                    )
                    if content:
                        return await self.autonomous.delivery.feishu.upload_image(content)
                    return ""

                try:
                    job["image_key"] = (
                        await asyncio.wait_for(image(), timeout=self.config.card_image_timeout_sec)
                        or ""
                    )
                except Exception as exc:
                    log.warning(
                        "dream image preparation failed conversation=%d error=%s",
                        cid,
                        type(exc).__name__,
                    )
            job["image_done"] = True
        job["status"] = "ready"
        await self._save(cid, kind, job)

    async def _deliver(self, cid, chat_id, event, date, job, *, mark_date, activity=None):
        kind = event["kind"]
        # A crash after this durable guard must never cause an automatic duplicate.
        job["status"] = "sending"
        await self._save(cid, kind, job)
        if activity is not None:
            activity_ts = max(row["ts"] for row in activity)
            fresh_sec = float(self.config.settings["autonomous"]["fresh_activity_sec"])
            if (
                self.runtime.chat_pending_messages.get(cid)
                or not self._in_window(event, date, time.time())
                or not 0 <= time.time() - activity_ts <= fresh_sec
            ):
                job["status"] = "ready"
                await self._save(cid, kind, job)
                self._skip(cid, kind, "admission_changed_before_send")
                return None
        try:
            result = await self.autonomous.send_reply(
                cid,
                chat_id,
                GeneratedReply(job["text"], job["mode"]),
                source=kind,
                extra_state={
                    **({event["state_key"]: date} if mark_date else {}),
                    "last_proactive_topic": job.get("topic") or event["name"],
                    "last_proactive_answered": False,
                },
                example_ids=frozenset(job.get("example_ids", [])),
                image_key=job.get("image_key", ""),
            )
        except DeliveryRejected:
            job["status"] = "ready"
            await self._save(cid, kind, job)
            raise
        except BaseException:
            # Includes cancellation while the remote result is unknown. On restart,
            # a durable 'sending' state is treated just like 'uncertain'.
            job["status"] = "uncertain"
            await self._save(cid, kind, job)
            raise
        if activity is not None:
            job["until_id"] = max(row["id"] for row in activity)
        job["status"] = "sent" if mark_date else "ready"
        await self._save(cid, kind, job)
        log.info(
            "scheduled sent conversation=%d kind=%s date=%s marked=%s", cid, kind, date, mark_date
        )
        return result

    async def speak(self, cid, chat_id, event, date, *, mark_date=True):
        """Explicit GM operation bypasses automatic admission gates, not uncertain sends."""
        async with self._lock(cid, event["kind"]):
            job = await self._load(cid, event["kind"], date)
            if job["status"] in {"sending", "uncertain"}:
                raise ValueError("发送状态不明，请先核对群聊记录；停止重复发送")
            await self.autonomous.observer.flush_observer_buffer(cid)
            await self._prepare(cid, event, date, job)
            async with self.runtime.chat_lock(cid):
                return await self._deliver(cid, chat_id, event, date, job, mark_date=mark_date)

    async def maybe_publish(self, cid: int, chat_id: str) -> tuple[bool, bool]:
        """Returns (handled, sent); an unavailable diary leaves ordinary chat available."""
        now = time.time()
        date, _ = self.state_domain.local_date_hour(now)
        state = await self.conversations.load_conversation_state(cid)
        for event in self.config.scheduled_events:
            if self._in_window(event, date, now) and state[event["state_key"]] != date:
                return await self._maybe_event(cid, chat_id, event, date)
        return False, False

    async def _maybe_event(self, cid, chat_id, event, date) -> tuple[bool, bool]:
        kind = event["kind"]
        lock = self._lock(cid, kind)
        if lock.locked():
            return self._skip(cid, kind, "job_busy")
        async with lock:
            job = await self._load(cid, kind, date)
            if job["status"] in {"sending", "uncertain", "sent"}:
                return self._skip(cid, kind, job["status"])
            diary = await self.identities.diary(cid, date) if kind == "diary" else None
            if kind == "diary" and diary is None:
                return False, False
            state = await self.conversations.load_conversation_state(cid)
            if self.runtime.chat_pending_messages.get(cid) or self.runtime.chat_lock(cid).locked():
                return self._skip(cid, kind, "direct_reply_busy")
            if self.autonomous.cooling_down(state, time.time()):
                return self._skip(cid, kind, "cooldown")
            if time.time() - job["last_attempt"] < 60:
                return self._skip(cid, kind, "backoff")
            await self.autonomous.observer.flush_observer_buffer(cid)
            rows, fresh = await self.autonomous.fresh_activity(
                cid, state, job["until_id"], time.time()
            )
            if not fresh:
                self._skip(cid, kind, "no_fresh_activity")
                return False, False
            job["last_attempt"] = time.time()
            await self._save(cid, kind, job)
            context = (
                ("日记：" + diary["text"])
                if diary
                else ("梦境分享：" + (job.get("text") or event["instruction"]))
            )
            try:
                decision = await asyncio.wait_for(
                    self.autonomous.participation_decision(rows, state, scheduled_context=context),
                    timeout=self.config.settings["autonomous"]["scheduled_evaluation_timeout_sec"],
                )
                if decision is None:
                    job["until_id"] = max(r["id"] for r in fresh)
                    await self._save(cid, kind, job)
                    self._skip(cid, kind, "participation_denied")
                    return False, False
                approved_activity = (state["last_reply_ts"], state["last_proactive_ts"])
                job["topic"] = decision[1]
                if not self._in_window(event, date, time.time()):
                    return self._skip(cid, kind, "window_closed")
                if self.runtime.chat_pending_messages.get(cid):
                    return self._skip(cid, kind, "direct_reply_pending")
                if not await self._prepare(cid, event, date, job, automatic=True):
                    return True, False
                # Nothing slow that creates content is allowed inside chat_lock.
                if (
                    self.runtime.chat_pending_messages.get(cid)
                    or self.runtime.chat_lock(cid).locked()
                ):
                    return self._skip(cid, kind, "direct_reply_busy")
                async with self.runtime.chat_lock(cid):
                    await self.autonomous.observer.flush_observer_buffer(cid)
                    state = await self.conversations.load_conversation_state(cid)
                    if not self._in_window(event, date, time.time()):
                        return self._skip(cid, kind, "window_closed")
                    if (state["last_reply_ts"], state["last_proactive_ts"]) != approved_activity:
                        return self._skip(cid, kind, "conversation_changed")
                    if state[event["state_key"]] == date:
                        return self._skip(cid, kind, "already_sent")
                    if self.runtime.chat_pending_messages.get(cid):
                        return self._skip(cid, kind, "direct_reply_pending")
                    if self.autonomous.cooling_down(state, time.time()):
                        return self._skip(cid, kind, "cooldown")
                    _, fresh = await self.autonomous.fresh_activity(
                        cid, state, job["until_id"], time.time()
                    )
                    if not fresh:
                        return self._skip(cid, kind, "activity_expired")
                    result = await self._deliver(
                        cid,
                        chat_id,
                        event,
                        date,
                        job,
                        mark_date=True,
                        activity=fresh,
                    )
                    return True, result is not None
            except Exception as exc:
                log.warning(
                    "scheduled attempt failed conversation=%d kind=%s error=%s",
                    cid,
                    kind,
                    type(exc).__name__,
                )
                return True, False
