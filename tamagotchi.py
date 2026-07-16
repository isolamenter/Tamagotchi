from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from config import AppConfig, load_config
from domain.card import CardDomain
from domain.gameplay import GameplayDomain
from domain.memory import MemoryDomain
from domain.pet import PetDomain
from domain.state import StateDomain
from integrations.feishu_client import FeishuClient
from integrations.llm_client import LLMClient
from repositories.memory_repo import MemoryRepository
from repositories.message_repo import MessageRepository
from repositories.card_repo import CardRepository
from repositories.pet_repo import PetRepository
from repositories.sqlite import Database
from repositories.system_repo import SystemRepository
from routes import feishu, gm, health, web
from runtime import RuntimeState
from services.autonomous_service import AutonomousService
from services.card_service import CardService
from services.gameplay_service import GameplayService
from services.memory_service import MemoryService
from services.observer_service import ObserverService
from services.reply_service import ReplyService


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tamagotchi")


@dataclass
class AppServices:
    reply: ReplyService
    observer: ObserverService
    autonomous: AutonomousService
    card: CardService
    gameplay: GameplayService
    memory: MemoryService


@dataclass
class AppContainer:
    config: AppConfig
    runtime: RuntimeState
    db: Database
    state_domain: StateDomain
    card_domain: CardDomain
    gameplay_domain: GameplayDomain
    pet_domain: PetDomain
    memory_domain: MemoryDomain
    system_repo: SystemRepository
    pet_repo: PetRepository
    message_repo: MessageRepository
    memory_repo: MemoryRepository
    card_repo: CardRepository
    feishu: FeishuClient
    llm: LLMClient
    services: AppServices


def build_container(config: AppConfig | None = None) -> AppContainer:
    config = config or load_config()
    runtime = RuntimeState()

    state_domain = StateDomain(config)
    gameplay_domain = GameplayDomain(config)
    card_domain = CardDomain(config, state_domain, gameplay_domain)
    pet_domain = PetDomain(config)
    memory_domain = MemoryDomain(config)

    db = Database(config)
    db.init_db()

    system_repo = SystemRepository(db)
    pet_repo = PetRepository(db, state_domain, runtime)
    message_repo = MessageRepository(db)
    memory_repo = MemoryRepository(db, memory_domain, config)
    card_repo = CardRepository(db)

    llm = LLMClient(config)
    feishu_client = FeishuClient(config, system_repo, runtime)

    memory_service = MemoryService(
        config,
        runtime,
        memory_domain,
        pet_domain,
        memory_repo,
        llm,
    )
    observer_service = ObserverService(
        config,
        runtime,
        message_repo,
        memory_service,
        pet_repo,
    )
    gameplay_service = GameplayService(
        state_domain,
        gameplay_domain,
        pet_repo,
    )
    card_service = CardService(
        config,
        runtime,
        state_domain,
        gameplay_domain,
        card_domain,
        pet_domain,
        pet_repo,
        card_repo,
        message_repo,
        system_repo,
        gameplay_service,
        memory_service,
        feishu_client,
        llm,
    )
    autonomous_service = AutonomousService(
        config,
        state_domain,
        gameplay_domain,
        card_domain,
        pet_domain,
        pet_repo,
        message_repo,
        system_repo,
        gameplay_service,
        memory_service,
        observer_service,
        feishu_client,
        llm,
        card_service=card_service,
    )
    reply_service = ReplyService(
        config,
        runtime,
        state_domain,
        gameplay_domain,
        pet_domain,
        pet_repo,
        message_repo,
        system_repo,
        memory_service,
        observer_service,
        card_service,
        feishu_client,
        llm,
    )

    services = AppServices(
        reply=reply_service,
        observer=observer_service,
        autonomous=autonomous_service,
        card=card_service,
        gameplay=gameplay_service,
        memory=memory_service,
    )

    return AppContainer(
        config=config,
        runtime=runtime,
        db=db,
        state_domain=state_domain,
        card_domain=card_domain,
        gameplay_domain=gameplay_domain,
        pet_domain=pet_domain,
        memory_domain=memory_domain,
        system_repo=system_repo,
        pet_repo=pet_repo,
        message_repo=message_repo,
        memory_repo=memory_repo,
        card_repo=card_repo,
        feishu=feishu_client,
        llm=llm,
        services=services,
    )


def create_app(config: AppConfig | None = None) -> FastAPI:
    container = build_container(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(container.services.autonomous.run_loop())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await container.services.observer.flush_all()
            await container.feishu.close()

    fastapi_app = FastAPI(lifespan=lifespan)
    fastapi_app.state.container = container
    fastapi_app.include_router(health.router)
    fastapi_app.include_router(gm.router)
    fastapi_app.include_router(web.router)
    fastapi_app.include_router(feishu.router)
    return fastapi_app


app = create_app()
