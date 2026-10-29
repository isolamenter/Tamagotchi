from __future__ import annotations

import asyncio
import json
import logging
import time

from dafeiyu.domain.consolidation import PROMPT, validate_result
from dafeiyu.domain.conversation import spoken_name

log = logging.getLogger("dafeiyu")


class ConsolidationService:
    def __init__(self, config, runtime, state_domain, identities, memory, llm):
        self.config, self.runtime, self.state_domain = config, runtime, state_domain
        self.identities, self.memory, self.llm = identities, memory, llm
        self.locks: dict[int, asyncio.Lock] = {}
        self.model_limit = asyncio.Semaphore(1)

    async def maybe_consolidate(self, cid: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        options = self.config.consolidation
        date, hour = self.state_domain.local_date_hour(now)
        if not options["enabled"] or hour < options["hour"]:
            return False
        lock = self.locks.setdefault(cid, asyncio.Lock())
        if lock.locked():
            return False
        async with lock, self.model_limit:  # Not chat_lock: human replies remain available.
            if self.runtime.observer_buffer.get(cid):
                # Received human messages must be stored before freezing the snapshot.
                return False
            state = await self.identities.begin_attempt(
                cid, date, now, options["max_calls_per_day"], options["retry_sec"]
            )
            if state is None:
                return False
            try:
                diary_event = next(
                    (e for e in self.config.scheduled_events if e["kind"] == "diary"), None
                )
                prompt = (
                    PROMPT
                    + "\n【仅用于 diary.text 的角色口吻，不改变记忆事实判断和输出结构】\n"
                    + self.config.persona["style"]["prompt"]
                )
                if diary_event:
                    prompt += "\n【日记写作要求】\n" + diary_event["instruction"]
                rows = await self.identities.pending(cid, state, options["max_messages"])
                if not rows:
                    raise ValueError("missing snapshot events")
                subjects = {r["sender_open_id"] for r in rows if r["sender_open_id"]}
                subjects.update(m["open_id"] for r in rows for m in json.loads(r["mentions_json"]))
                known = await self.identities.aliases(cid, subjects)
                known = [{k: r[k] for k in ("subject_open_id", "alias", "status")} for r in known]
                # Bound context independently of backlog or individual message size.
                known_json = json.dumps(known, ensure_ascii=False)
                side_budget = max(512, (options["max_context_chars"] - len(prompt) - 2000) // 5)
                while len(known_json) > side_budget:
                    known.pop()
                    known_json = json.dumps(known, ensure_ascii=False)
                profiles = await self.memory.memory_repo.subject_cards(
                    cid, subjects, 64, profiles_only=True
                )
                profiles = [
                    {k: p[k] for k in ("id", "subject_ids", "profile_key", "what", "confidence")}
                    for p in profiles
                    if p["profile_key"]
                ]
                while len(json.dumps(profiles, ensure_ascii=False)) > side_budget:
                    profiles.pop()
                remaining = (
                    options["max_context_chars"]
                    - len(json.dumps(profiles, ensure_ascii=False))
                    - len(prompt)
                    - len(known_json)
                    - 2000
                )
                events, until, offset = [], state["until_id"], state["next_offset"]
                for row in rows:
                    start = offset if not events else 0
                    record = {
                        k: row[k]
                        for k in (
                            "id",
                            "sender_open_id",
                            "sender_name",
                            "source_message_id",
                            "ts",
                            "source",
                            "content_kind",
                        )
                    }
                    record.update(
                        sender_name=spoken_name(row["sender_name"], row["sender_open_id"]),
                        mentions=json.loads(row["mentions_json"]),
                        start_offset=start,
                        legacy_unverified=row["evidence_text"] is None,
                        diary_eligible=bool(
                            diary_event
                            and row["evidence_text"] is not None
                            and self.state_domain.local_date_hour(row["ts"])[0] == date
                        ),
                        content="",
                        content_complete=False,
                    )
                    overhead = len(json.dumps(record, ensure_ascii=False)) + 4
                    # JSON escaping can multiply content size; calculate the actual serialized size.
                    available = remaining - overhead
                    if available < 128:
                        break
                    text = (
                        row["content"] if row["evidence_text"] is None else row["evidence_text"]
                    )[start:]
                    take = min(len(text), available)
                    while len(json.dumps(text[:take], ensure_ascii=False)) > available:
                        take = max(
                            0,
                            take
                            - max(
                                1, (len(json.dumps(text[:take], ensure_ascii=False)) - available)
                            ),
                        )
                    if take == 0 and text:
                        break
                    record.update(content=text[:take], content_complete=take == len(text))
                    events.append(record)
                    remaining -= len(json.dumps(record, ensure_ascii=False)) + 4
                    if take < len(text):
                        offset = start + take
                        break
                    until, offset = row["id"], 0
                if not events:
                    raise ValueError("context budget cannot fit event metadata")
                request = json.dumps(
                    {
                        "known_aliases": known,
                        "known_profiles": profiles,
                        # Platform IDs are retained for persisted provenance, not model references.
                        "events": [
                            {k: v for k, v in event.items() if k != "source_message_id"}
                            for event in events
                        ],
                        "diary_date": date,
                        "diary_enabled": diary_event is not None,
                    },
                    ensure_ascii=False,
                )
                if len(prompt) + len(request) > options["max_context_chars"]:
                    raise ValueError("context budget exceeded")
                content = await asyncio.wait_for(
                    self.llm.chat_json(
                        [
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": request},
                        ],
                        max_tokens=options["max_output_tokens"],
                        temperature=0.2,
                    ),
                    timeout=options["timeout_sec"],
                )
                aliases, cards, diary = validate_result(content, events, profiles)
                inserted = await self.identities.commit(
                    cid, state, until, offset, aliases, cards, now, diary=diary
                )
            except Exception as exc:
                log.warning(
                    "daily consolidation failed conversation=%d error=%s", cid, type(exc).__name__
                )
                await self.identities.fail(cid, type(exc).__name__)
                return False
            for card_id, card in inserted:
                self.runtime.spawn(
                    self.memory.embed_and_store(
                        card_id, self.memory.memory_domain.format_card_for_embed(card)
                    )
                )
            return True
