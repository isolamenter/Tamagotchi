from __future__ import annotations

import hashlib
import json
import math
import random
import re

from config import AppConfig


class StyleDomain:
    """Pure retrieval and rendering for structure cards and catchphrases."""

    CATCHPHRASE_EMBEDDING_TYPE = "catchphrase"
    EXAMPLE_CARD_EMBEDDING_TYPE = "example_card"

    def __init__(self, config: AppConfig, *, rng: random.Random | None = None):
        corpus = config.style_corpus
        retrieval = corpus.get("retrieval", {})
        cards = corpus.get("example_cards", {})
        runtime = config.style_retrieval
        self._rng = rng or random.SystemRandom()
        self.corpus_version = str(retrieval.get("corpus_version", "1"))
        self.examples = tuple(
            {
                **example,
                # Keep a primary response for callers that only need a stable
                # label. Rendering chooses from the full variant group below.
                "response": self.response_texts(example)[0],
            }
            for example in corpus.get("examples", [])
            if self.response_texts(example)
        )
        self.example_cards = tuple(
            card
            for card in cards.get("cards", [])
            if str(card.get("response") or "").strip()
        )
        self.example_card_max_examples = min(
            2, max(0, int(cards.get("max_examples", 2)))
        )
        self.example_card_header = str(cards.get("header") or "")
        self.example_card_footer = str(cards.get("footer") or "")
        self.example_card_aggressive_keywords = tuple(
            self._normalize_text(str(keyword))
            for keyword in cards.get("aggressive_keywords", [])
            if self._normalize_text(str(keyword))
        )
        self.intent_by_response = {
            response: intent
            for intent, responses in corpus.get("intent_groups", {}).items()
            for response in responses
        }
        self.risk_by_response = {
            response: risk
            for risk, responses in corpus.get("risk_groups", {}).items()
            for response in responses
        }
        self.max_examples = min(2, max(0, int(retrieval.get("max_examples", 2))))
        self.header = retrieval.get("header", "")
        self.example_template = retrieval.get(
            "example_template", "- {context} -> {response}"
        )
        self.footer = retrieval.get("footer", "")
        self.semantic_threshold = float(runtime["semantic_threshold"])
        self.example_card_semantic_threshold = float(
            runtime["example_card_semantic_threshold"]
        )
        self.second_semantic_threshold = float(
            runtime["second_semantic_threshold"]
        )
        self.second_max_gap = float(runtime["second_max_gap"])
        self.keyword_bonus = float(runtime["keyword_bonus"])
        self.keyword_bonus_cap = float(runtime["keyword_bonus_cap"])
        self.context_messages = max(0, int(runtime["context_messages"]))
        self.query_max_chars = max(1, int(runtime["query_max_chars"]))
        self.assistant_history_keep = max(
            0, int(runtime["assistant_history_keep"])
        )

    @staticmethod
    def _normalize_text(text: str) -> str:
        return re.sub(r"\s+", "", (text or "").lower())

    @staticmethod
    def normalize_vector(vector: list[float]) -> list[float]:
        magnitude = math.sqrt(sum(value * value for value in vector))
        if magnitude <= 0.0:
            return []
        return [value / magnitude for value in vector]

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        return sum(x * y for x, y in zip(a, b))

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def response_variants(example: dict) -> tuple[tuple[str, int], ...]:
        variants = example.get("variants") or []
        prepared = tuple(
            (
                str(item.get("text") if isinstance(item, dict) else item).strip(),
                max(1, int(item.get("source_count", 1)))
                if isinstance(item, dict)
                else 1,
            )
            for item in variants
            if str(item.get("text") if isinstance(item, dict) else item).strip()
        )
        if prepared:
            return prepared
        response = str(example.get("response") or "").strip()
        return ((response, max(1, int(example.get("source_count", 1)))),) if response else ()

    @classmethod
    def response_texts(cls, example: dict) -> tuple[str, ...]:
        return tuple(text for text, _weight in cls.response_variants(example))

    def _choose_response(self, example: dict) -> str:
        variants = self.response_variants(example)
        return self._rng.choices(
            [text for text, _weight in variants],
            weights=[weight for _text, weight in variants],
            k=1,
        )[0]

    def example_id(self, example: dict) -> str:
        variant_key = json.dumps(
            self.response_texts(example), ensure_ascii=False, separators=(",", ":")
        )
        return self._sha256(variant_key)

    @staticmethod
    def _card_context(card: dict) -> tuple[tuple[str, str], ...]:
        prepared = []
        for turn in card.get("context", []):
            if not isinstance(turn, (list, tuple)) or len(turn) != 2:
                continue
            speaker, content = (str(turn[0]).strip(), str(turn[1]).strip())
            if speaker and content:
                prepared.append((speaker, content))
        return tuple(prepared)

    @staticmethod
    def _speaker_label(speaker: str) -> str:
        return {
            "self": "我前面",
            "peer": "群友",
            "assistant": "当时的机器人",
        }.get(speaker, "群友")

    def example_card_id(self, card: dict) -> str:
        source_id = int(card.get("source_message_id", 0) or 0)
        if source_id:
            return f"card:{source_id}"
        payload = json.dumps(
            [self._card_context(card), str(card.get("response") or "")],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return f"card:{self._sha256(payload)}"

    def format_embedding_document(self, example: dict) -> str:
        return (
            f"口头禅适用场景：{str(example.get('context', '')).strip()}\n"
            f"关键词：{'、'.join(str(item) for item in example.get('keywords', []))}\n"
            f"口头禅候选：{'、'.join(self.response_texts(example))}"
        )

    def format_example_card_document(self, card: dict) -> str:
        context = "\n".join(
            f"{self._speaker_label(speaker)}：{content}"
            for speaker, content in self._card_context(card)
        )
        if not context:
            context = "（没有直接上文，是自然发起的话）"
        return (
            f"句子结构模式：{str(card.get('mode') or 'conversation')}\n"
            f"真实聊天上文：\n{context}\n"
            f"我的回复：{str(card.get('response') or '').strip()}"
        )

    def corpus_records(self) -> list[dict]:
        records = []
        for example in self.examples:
            document = self.format_embedding_document(example)
            records.append(
                {
                    "example_id": self.example_id(example),
                    "embedding_type": self.CATCHPHRASE_EMBEDDING_TYPE,
                    "content_hash": self._sha256(
                        f"corpus-version:{self.corpus_version}\n{document}"
                    ),
                    "document": document,
                    "example": example,
                }
            )
        for card in self.example_cards:
            document = self.format_example_card_document(card)
            records.append(
                {
                    "example_id": self.example_card_id(card),
                    "embedding_type": self.EXAMPLE_CARD_EMBEDDING_TYPE,
                    "content_hash": self._sha256(
                        f"corpus-version:{self.corpus_version}\n{document}"
                    ),
                    "document": document,
                    "example": card,
                }
            )
        return records

    def build_query(self, current_text: str, history: list[dict] | None = None) -> str:
        current_text = (current_text or "").strip()
        if not current_text:
            return ""
        prior_user_messages = [
            (item.get("content") or "").strip()
            for item in (history or [])
            if item.get("role") == "user" and (item.get("content") or "").strip()
        ]
        prior_user_messages = (
            prior_user_messages[-self.context_messages :]
            if self.context_messages
            else []
        )
        parts = [f"最近群聊：{text}" for text in prior_user_messages]
        parts.append(f"当前消息：{current_text}")
        return "\n".join(parts)[-self.query_max_chars :]

    def _keyword_matches(self, example: dict, current_text: str) -> list[str]:
        normalized_query = self._normalize_text(current_text)
        if not normalized_query:
            return []
        return [
            normalized_keyword
            for keyword in example.get("keywords", [])
            if (normalized_keyword := self._normalize_text(str(keyword)))
            and normalized_keyword in normalized_query
        ]

    def _intent(self, example: dict) -> str:
        # Corpus entries should declare intent. Response remains a conservative
        # fallback so an untagged entry can never unlock an unrelated second cue.
        response = self.response_texts(example)[0]
        return str(
            example.get("intent") or self.intent_by_response.get(response) or response
        ).strip()

    def _risk(self, example: dict) -> str:
        responses = self.response_texts(example)
        return str(
            example.get("risk")
            or next(
                (
                    self.risk_by_response[response]
                    for response in responses
                    if response in self.risk_by_response
                ),
                "normal",
            )
        ).strip()

    def select_examples(
        self,
        current_text: str,
        *,
        scope: str = "reply",
        limit: int | None = None,
        query_vector: list[float] | None = None,
        vectors: dict[str, list[float]] | None = None,
    ) -> list[dict]:
        if not (current_text or "").strip():
            return []

        normalized_query_vector = self.normalize_vector(query_vector or [])
        scored: list[tuple[float, float, int, int, str, dict]] = []
        for example in self.examples:
            scopes = example.get("scopes") or ["reply"]
            if scope not in scopes:
                continue

            matches = self._keyword_matches(example, current_text)
            if self._risk(example) == "aggressive" and not matches:
                continue

            example_id = self.example_id(example)
            semantic = 0.0
            if normalized_query_vector and vectors:
                semantic = self.cosine(
                    normalized_query_vector, vectors.get(example_id, [])
                )
            if semantic < self.semantic_threshold and not matches:
                continue

            lexical_bonus = min(
                self.keyword_bonus_cap, self.keyword_bonus * len(matches)
            )
            lexical_specificity = sum(10 + min(len(match), 8) for match in matches)
            source_count = max(1, int(example.get("source_count", 1)))
            combined = semantic + lexical_bonus
            scored.append(
                (
                    combined,
                    semantic,
                    lexical_specificity,
                    source_count,
                    example_id,
                    example,
                )
            )

        scored.sort(
            key=lambda item: (-item[0], -item[1], -item[2], -item[3], item[4])
        )
        cap = self.max_examples if limit is None else min(2, max(0, int(limit)))
        if cap <= 0 or not scored:
            return []

        first = scored[0]
        selected = [
            {
                **first[5],
                "_example_id": first[4],
                "_semantic_score": first[1],
                "_combined_score": first[0],
            }
        ]
        if cap < 2:
            return selected

        first_intent = self._intent(first[5])
        for candidate in scored[1:]:
            if self._intent(candidate[5]) != first_intent:
                continue
            if candidate[1] < self.second_semantic_threshold:
                continue
            if first[0] - candidate[0] > self.second_max_gap:
                continue
            selected.append(
                {
                    **candidate[5],
                    "_example_id": candidate[4],
                    "_semantic_score": candidate[1],
                    "_combined_score": candidate[0],
                }
            )
            break
        return selected

    def select_example_cards(
        self,
        current_text: str,
        *,
        scope: str = "reply",
        query_vector: list[float] | None = None,
        vectors: dict[str, list[float]] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        normalized_query_vector = self.normalize_vector(query_vector or [])
        if not normalized_query_vector or not vectors:
            return []
        cap = (
            self.example_card_max_examples
            if limit is None
            else min(2, max(0, int(limit)))
        )
        if cap <= 0:
            return []

        scored: list[tuple[float, str, dict]] = []
        normalized_current = self._normalize_text(current_text)
        aggressive_context = any(
            keyword in normalized_current
            for keyword in self.example_card_aggressive_keywords
        )
        for card in self.example_cards:
            if scope not in (card.get("scopes") or ["reply", "proactive", "card"]):
                continue
            if card.get("risk") == "aggressive" and not aggressive_context:
                continue
            card_id = self.example_card_id(card)
            semantic = self.cosine(
                normalized_query_vector, vectors.get(card_id, [])
            )
            if semantic < self.example_card_semantic_threshold:
                continue
            scored.append((semantic, card_id, card))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            {
                **card,
                "_example_id": card_id,
                "_semantic_score": semantic,
            }
            for semantic, card_id, card in scored[:cap]
        ]

    def render_example_cards(self, cards: list[dict]) -> str:
        if not cards:
            return ""
        blocks = []
        for card in cards:
            turns = [
                f"  {self._speaker_label(speaker)}：{content}"
                for speaker, content in self._card_context(card)
            ]
            if not turns:
                turns.append("  上文：（没有直接上文，是自然发起的话）")
            turns.append(f"  我的回复：{str(card.get('response') or '').strip()}")
            blocks.append(
                f"- 结构模式：{str(card.get('mode') or 'conversation')}\n"
                + "\n".join(turns)
            )
        body = "\n".join(blocks)
        footer = (
            f"\n{self.example_card_footer}"
            if self.example_card_footer
            else ""
        )
        return f"{self.example_card_header}{body}{footer}"

    def render_selected(
        self,
        examples: list[dict],
        *,
        example_cards: list[dict] | None = None,
        scope: str = "reply",
    ) -> str:
        blocks = []
        card_block = self.render_example_cards(example_cards or [])
        if card_block:
            blocks.append(card_block)
        if examples:
            lines = [
                self.example_template.format(
                    context=example["context"],
                    response=self._choose_response(example),
                )
                for example in examples
            ]
            body = "\n".join(lines)
            footer = f"\n{self.footer}" if self.footer else ""
            blocks.append(f"{self.header}{body}{footer}")
        return "\n".join(blocks)

    def render_examples_block(self, query: str, *, scope: str = "reply") -> str:
        return self.render_selected(
            self.select_examples(query, scope=scope), scope=scope
        )
