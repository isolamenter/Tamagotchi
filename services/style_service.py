from __future__ import annotations

import asyncio
import logging

from config import AppConfig
from domain.style import StyleDomain
from integrations.llm_client import LLMClient
from repositories.style_repo import StyleEmbeddingRepository

log = logging.getLogger("tamagotchi")


class StyleService:
    """Async embedding orchestration around the pure StyleDomain."""

    def __init__(
        self,
        config: AppConfig,
        domain: StyleDomain,
        repository: StyleEmbeddingRepository,
        llm: LLMClient,
    ):
        self.config = config
        self.domain = domain
        self.repository = repository
        self.llm = llm
        self._vectors: dict[str, list[float]] = {}
        self._ready = False
        self._init_lock = asyncio.Lock()
        self._queries = 0
        self._semantic_hits = 0
        self._fallback_hits = 0
        self._multi_hits = 0
        self._card_hits = 0
        self._score_sum = 0.0

    @property
    def ready(self) -> bool:
        return self._ready

    async def _embed_records(self, records: list[dict]) -> list[dict] | None:
        batch_size = max(
            1, int(self.config.style_retrieval["embedding_batch_size"])
        )
        prepared: list[dict] = []
        for start in range(0, len(records), batch_size):
            batch = records[start : start + batch_size]
            vectors = await self.llm.embed_texts(
                [record["document"] for record in batch],
                purpose="retrieval_document",
            )
            if vectors is None or len(vectors) != len(batch):
                return None
            for record, vector in zip(batch, vectors):
                normalized = self.domain.normalize_vector(vector)
                if not normalized:
                    return None
                prepared.append({**record, "vector": normalized})
        return prepared

    async def initialize(self) -> None:
        async with self._init_lock:
            records = self.domain.corpus_records()
            cached = await self.repository.load(
                self.config.llm_provider, self.config.embed_model
            )
            prepared_by_id: dict[str, dict] = {}
            missing: list[dict] = []
            for record in records:
                row = cached.get(record["example_id"])
                normalized = self.domain.normalize_vector(
                    list(row.get("vector", [])) if row else []
                )
                if (
                    row
                    and row.get("embedding_type") == record["embedding_type"]
                    and row.get("content_hash") == record["content_hash"]
                    and int(row.get("dimension", 0)) == len(normalized)
                    and normalized
                ):
                    prepared_by_id[record["example_id"]] = {
                        **record,
                        "vector": normalized,
                    }
                else:
                    missing.append(record)

            if missing:
                generated = await self._embed_records(missing)
                if generated is None:
                    raise RuntimeError(
                        f"style embedding generation failed for {len(missing)} entries"
                    )
                prepared_by_id.update(
                    {record["example_id"]: record for record in generated}
                )

            prepared = [prepared_by_id[record["example_id"]] for record in records]
            dimensions = {len(record["vector"]) for record in prepared}
            if len(dimensions) != 1:
                # Same model name can still change output dimensionality. Rebuild
                # everything before replacing the last known-good cache.
                rebuilt = await self._embed_records(records)
                if rebuilt is None:
                    raise RuntimeError("style embedding full rebuild failed")
                prepared = rebuilt
                dimensions = {len(record["vector"]) for record in prepared}
            if len(dimensions) != 1 or not prepared:
                raise RuntimeError("style embedding dimensions are inconsistent")

            if missing or len(cached) != len(records):
                await self.repository.replace_all(
                    self.config.llm_provider,
                    self.config.embed_model,
                    prepared,
                )
            self._vectors = {
                record["example_id"]: record["vector"] for record in prepared
            }
            self._ready = True
            type_counts: dict[str, int] = {}
            for record in prepared:
                kind = str(record["embedding_type"])
                type_counts[kind] = type_counts.get(kind, 0) + 1
            log.info(
                "style embeddings ready entries=%d generated=%d dimension=%d "
                "types=%s provider=%s model=%s",
                len(prepared),
                len(missing),
                next(iter(dimensions)),
                type_counts,
                self.config.llm_provider,
                self.config.embed_model,
            )

    def _record_metrics(
        self,
        selected: list[dict],
        cards: list[dict],
        semantic_used: bool,
    ) -> None:
        self._queries += 1
        if selected:
            if semantic_used:
                self._semantic_hits += 1
            else:
                self._fallback_hits += 1
            self._score_sum += float(selected[0].get("_semantic_score", 0.0))
        if len(selected) > 1:
            self._multi_hits += 1
        if cards:
            self._card_hits += 1
        if self._queries % 100 == 0:
            log.info(
                "style retrieval metrics queries=%d semantic_hits=%d fallback_hits=%d "
                "multi_hits=%d card_hits=%d avg_top_semantic=%.3f",
                self._queries,
                self._semantic_hits,
                self._fallback_hits,
                self._multi_hits,
                self._card_hits,
                self._score_sum / max(1, self._semantic_hits),
            )

    async def render_examples_block(
        self,
        current_text: str,
        *,
        scope: str = "reply",
        history: list[dict] | None = None,
    ) -> str:
        query = self.domain.build_query(current_text, history)
        query_vector = None
        semantic_used = False
        if query and self._ready:
            query_vector = await self.llm.embed_text(
                query, purpose="retrieval_query"
            )
            if query_vector:
                expected_dimension = (
                    len(next(iter(self._vectors.values()))) if self._vectors else 0
                )
                if len(query_vector) == expected_dimension:
                    semantic_used = True
                else:
                    log.warning(
                        "style query dimension mismatch query=%d index=%d; "
                        "using lexical fallback",
                        len(query_vector),
                        expected_dimension,
                    )
                    query_vector = None
            else:
                log.warning("style query embedding unavailable; using lexical fallback")

        selected = self.domain.select_examples(
            current_text,
            scope=scope,
            query_vector=query_vector,
            vectors=self._vectors if semantic_used else None,
        )
        cards = self.domain.select_example_cards(
            current_text,
            scope=scope,
            query_vector=query_vector,
            vectors=self._vectors if semantic_used else None,
        )
        self._record_metrics(selected, cards, semantic_used)
        if selected or cards:
            log.debug(
                "style retrieval scope=%s semantic=%s catchphrases=%s cards=%s "
                "top_score=%.3f",
                scope,
                semantic_used,
                [item["_example_id"][:12] for item in selected],
                [item["_example_id"] for item in cards],
                (
                    float(selected[0].get("_combined_score", 0.0))
                    if selected
                    else float(cards[0].get("_semantic_score", 0.0))
                ),
            )
        return self.domain.render_selected(
            selected,
            example_cards=cards,
            scope=scope,
        )
