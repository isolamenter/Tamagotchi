from __future__ import annotations

import time

from domain.gameplay import GameplayDomain, GameplayResult
from domain.state import StateDomain
from repositories.pet_repo import PetRepository


class GameplayService:
    def __init__(
        self,
        state_domain: StateDomain,
        gameplay_domain: GameplayDomain,
        pet_repo: PetRepository,
    ):
        self.state_domain = state_domain
        self.gameplay_domain = gameplay_domain
        self.pet_repo = pet_repo

    async def ensure_pet_gameplay(self, pet_id: int, now: float | None = None) -> dict:
        now = time.time() if now is None else now

        def _mutator(state: dict) -> dict:
            return self.gameplay_domain.expired_need_cleared(state, now)

        return await self.pet_repo.mutate_state(pet_id, _mutator)

    async def sync_need_clock(self, pet_id: int, quiet: bool, now: float | None = None) -> dict:
        now = time.time() if now is None else now

        def _mutator(state: dict) -> dict:
            if quiet:
                return self.gameplay_domain.pause_need(state, now)
            state = self.gameplay_domain.resume_need(state, now)
            return self.gameplay_domain.expired_need_cleared(state, now)

        return await self.pet_repo.mutate_state(pet_id, _mutator)

    async def maybe_create_need(
        self, pet_id: int, now: float | None = None
    ) -> tuple[dict, dict | None]:
        now = time.time() if now is None else now
        created: dict | None = None

        def _mutator(state: dict) -> dict:
            nonlocal created
            state, created = self.gameplay_domain.maybe_create_need(state, now, pet_id)
            if created:
                state["last_proactive_ts"] = now
            return state

        state = await self.pet_repo.mutate_state(pet_id, _mutator)
        return state, created

    def resolve_need_choice_state(
        self, state: dict, action: str, actor_name: str, now: float | None = None
    ) -> GameplayResult:
        now = time.time() if now is None else now
        return self.gameplay_domain.apply_choice(state, action, actor_name, now)

    def resolve_card_action_state(
        self,
        state: dict,
        action: str,
        actor_name: str,
        now: float | None = None,
        *,
        prefer_free: bool = False,
    ) -> GameplayResult:
        now = time.time() if now is None else now
        return self.gameplay_domain.apply_card_action(
            state, action, actor_name, now, prefer_free=prefer_free
        )
