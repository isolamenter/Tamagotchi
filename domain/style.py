from __future__ import annotations

import hashlib
import math
import re

from config import AppConfig


class StyleDomain:
    """Pure query construction, scoring and rendering for style examples."""

    def __init__(self, config: AppConfig):
        corpus = config.style_corpus
        retrieval = corpus.get("retrieval", {})
        runtime = config.style_retrieval
        self.examples = tuple(corpus.get("examples", []))
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

    def example_id(self, example: dict) -> str:
        return self._sha256(str(example.get("response", "")).strip())

    def format_embedding_document(self, example: dict) -> str:
        return (
            f"适用场景：{str(example.get('context', '')).strip()}\n"
            f"关键词：{'、'.join(str(item) for item in example.get('keywords', []))}\n"
            f"回复示例：{str(example.get('response', '')).strip()}"
        )

    def corpus_records(self) -> list[dict]:
        records = []
        for example in self.examples:
            document = self.format_embedding_document(example)
            records.append(
                {
                    "example_id": self.example_id(example),
                    "content_hash": self._sha256(document),
                    "document": document,
                    "example": example,
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
        response = str(example.get("response") or "").strip()
        return str(
            example.get("intent") or self.intent_by_response.get(response) or response
        ).strip()

    def _risk(self, example: dict) -> str:
        response = str(example.get("response") or "").strip()
        return str(
            example.get("risk") or self.risk_by_response.get(response) or "normal"
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
        scored: list[tuple[float, float, int, str, dict]] = []
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
            combined = semantic + lexical_bonus
            scored.append(
                (
                    combined,
                    semantic,
                    lexical_specificity,
                    example_id,
                    example,
                )
            )

        scored.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
        cap = self.max_examples if limit is None else min(2, max(0, int(limit)))
        if cap <= 0 or not scored:
            return []

        first = scored[0]
        selected = [
            {
                **first[4],
                "_example_id": first[3],
                "_semantic_score": first[1],
                "_combined_score": first[0],
            }
        ]
        if cap < 2:
            return selected

        first_intent = self._intent(first[4])
        for candidate in scored[1:]:
            if self._intent(candidate[4]) != first_intent:
                continue
            if candidate[1] < self.second_semantic_threshold:
                continue
            if first[0] - candidate[0] > self.second_max_gap:
                continue
            selected.append(
                {
                    **candidate[4],
                    "_example_id": candidate[3],
                    "_semantic_score": candidate[1],
                    "_combined_score": candidate[0],
                }
            )
            break
        return selected

    def render_selected(self, examples: list[dict]) -> str:
        if not examples:
            return ""
        lines = [
            self.example_template.format(
                context=example["context"], response=example["response"]
            )
            for example in examples
        ]
        body = "\n".join(lines)
        footer = f"\n{self.footer}" if self.footer else ""
        return f"{self.header}{body}{footer}"

    def render_examples_block(self, query: str, *, scope: str = "reply") -> str:
        return self.render_selected(self.select_examples(query, scope=scope))
