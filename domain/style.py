from __future__ import annotations

import hashlib
import re

from config import AppConfig


class StyleDomain:
    """Pure, local retrieval for curated style examples.

    Style examples describe how to phrase a response. They deliberately stay
    outside the event-memory repository, which describes what happened.
    """

    def __init__(self, config: AppConfig):
        corpus = config.style_corpus
        retrieval = corpus.get("retrieval", {})
        self.examples = tuple(corpus.get("examples", []))
        self.max_examples = max(0, int(retrieval.get("max_examples", 4)))
        self.header = retrieval.get("header", "")
        self.example_template = retrieval.get(
            "example_template", "- {context} -> {response}"
        )
        self.footer = retrieval.get("footer", "")

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", "", (text or "").lower())

    @staticmethod
    def _tie_break(query: str, response: str) -> str:
        return hashlib.sha256(f"{query}\0{response}".encode("utf-8")).hexdigest()

    def select_examples(
        self, query: str, *, scope: str = "reply", limit: int | None = None
    ) -> list[dict]:
        normalized_query = self._normalize(query)
        if not normalized_query:
            return []

        scored: list[tuple[int, str, dict]] = []
        for example in self.examples:
            scopes = example.get("scopes") or ["reply"]
            if scope not in scopes:
                continue
            score = 0
            for keyword in example.get("keywords", []):
                normalized_keyword = self._normalize(str(keyword))
                if normalized_keyword and normalized_keyword in normalized_query:
                    # Longer phrases are more specific and should outrank broad
                    # category words such as "游戏" or "加班".
                    score += 10 + min(len(normalized_keyword), 8)
            if score <= 0:
                continue
            response = str(example.get("response", "")).strip()
            context = str(example.get("context", "")).strip()
            if not response or not context:
                continue
            # Echoing an identical incoming phrase is rarely a useful style cue.
            if self._normalize(response) == normalized_query:
                score -= 5
            scored.append(
                (score, self._tie_break(normalized_query, response), example)
            )

        selected: list[dict] = []
        seen_responses: set[str] = set()
        cap = self.max_examples if limit is None else max(0, int(limit))
        if cap <= 0:
            return []
        for _score, _tie, example in sorted(
            scored, key=lambda item: (-item[0], item[1])
        ):
            response = str(example["response"]).strip()
            if response in seen_responses:
                continue
            selected.append(example)
            seen_responses.add(response)
            if len(selected) >= cap:
                break
        return selected

    def render_examples_block(self, query: str, *, scope: str = "reply") -> str:
        examples = self.select_examples(query, scope=scope)
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
