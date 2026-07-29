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
from repositories.style_repo import StyleEmbeddingRepository
from routes import feishu, gm, health, web
from runtime import RuntimeState
from services.autonomous_service import AutonomousService
from services.card_service import CardService
from services.gameplay_service import GameplayService
from services.memory_service import MemoryService
from services.observer_service import ObserverService
from services.reply_service import ReplyService
from services.style_service import StyleService


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
    style: StyleService


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
    style_repo: StyleEmbeddingRepository
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
    style_repo = StyleEmbeddingRepository(db)
    card_repo = CardRepository(db)

    llm = LLMClient(config)
    feishu_client = FeishuClient(config, system_repo, runtime)
    style_service = StyleService(
        config,
        pet_domain.style_domain,
        style_repo,
        llm,
    )

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
        style_service=style_service,
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
        style_service=style_service,
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
        style_service=style_service,
    )

    services = AppServices(
        reply=reply_service,
        observer=observer_service,
        autonomous=autonomous_service,
        card=card_service,
        gameplay=gameplay_service,
        memory=memory_service,
        style=style_service,
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
        style_repo=style_repo,
        card_repo=card_repo,
        feishu=feishu_client,
        llm=llm,
        services=services,
    )


def create_app(config: AppConfig | None = None) -> FastAPI:
    container = build_container(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def initialize_style() -> None:
            try:
                await asyncio.wait_for(
                    container.services.style.initialize(),
                    timeout=float(
                        container.config.style_retrieval["init_timeout_sec"]
                    ),
                )
            except Exception:
                log.exception(
                    "style embedding initialization failed; lexical fallback remains active"
                )

        style_task = asyncio.create_task(initialize_style())
        task = asyncio.create_task(container.services.autonomous.run_loop())
        try:
            yield
        finally:
            style_task.cancel()
            task.cancel()
            for background_task in (style_task, task):
                try:
                    await background_task
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
