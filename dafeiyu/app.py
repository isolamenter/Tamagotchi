from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from dafeiyu import __version__
from dafeiyu.config import AppConfig, load_config
from dafeiyu.domain.conversation import ConversationContext
from dafeiyu.domain.memory import MemoryDomain
from dafeiyu.domain.state import StateDomain
from dafeiyu.integrations.feishu_client import FeishuClient
from dafeiyu.integrations.llm_client import LLMClient
from dafeiyu.repositories.conversation_repo import ConversationRepository
from dafeiyu.repositories.identity_repo import IdentityRepository
from dafeiyu.repositories.memory_repo import MemoryRepository
from dafeiyu.repositories.message_repo import MessageRepository
from dafeiyu.repositories.sqlite import Database
from dafeiyu.repositories.style_repo import StyleEmbeddingRepository
from dafeiyu.repositories.system_repo import SystemRepository
from dafeiyu.routes import feishu, gm, health, web
from dafeiyu.runtime import RuntimeState
from dafeiyu.services.autonomous_service import AutonomousService
from dafeiyu.services.consolidation_service import ConsolidationService
from dafeiyu.services.delivery import DeliveryService
from dafeiyu.services.incoming import IncomingService
from dafeiyu.services.memory_service import MemoryService
from dafeiyu.services.observer_service import ObserverService
from dafeiyu.services.reply_service import ReplyService
from dafeiyu.services.scheduled_service import ScheduledService
from dafeiyu.services.style_service import StyleService

log = logging.getLogger("dafeiyu")


@dataclass
class AppServices:
    reply: ReplyService
    observer: ObserverService
    autonomous: AutonomousService
    memory: MemoryService
    style: StyleService
    incoming: IncomingService
    delivery: DeliveryService
    consolidation: ConsolidationService
    scheduled: ScheduledService


@dataclass
class AppContainer:
    config: AppConfig
    runtime: RuntimeState
    db: Database
    state_domain: StateDomain
    conversation_domain: ConversationContext
    memory_domain: MemoryDomain
    system_repo: SystemRepository
    conversation_repo: ConversationRepository
    message_repo: MessageRepository
    memory_repo: MemoryRepository
    style_repo: StyleEmbeddingRepository
    feishu: FeishuClient
    llm: LLMClient
    services: AppServices


def build_container(config: AppConfig) -> AppContainer:
    runtime = RuntimeState()
    state, context, memory_domain = (
        StateDomain(config),
        ConversationContext(config),
        MemoryDomain(config),
    )
    db = Database(config.db_path)
    db.init_db()
    system = SystemRepository(db)
    conversations = ConversationRepository(db, state, runtime)
    messages, memories, styles = (
        MessageRepository(db),
        MemoryRepository(db, memory_domain, config),
        StyleEmbeddingRepository(db),
    )
    llm, feishu_client = LLMClient(config), FeishuClient(config, system, runtime)
    style = StyleService(config, context.style_domain, styles, llm)
    memory = MemoryService(config, runtime, memory_domain, context, memories, llm)
    identities = IdentityRepository(db)
    memory.identities = identities
    consolidation = ConsolidationService(config, runtime, state, identities, memory, llm)
    observer = ObserverService(config, runtime, messages, memory, conversations)
    incoming = IncomingService(context, system, feishu_client, llm)
    delivery = DeliveryService(config, runtime, conversations, messages, feishu_client, llm)
    autonomous = AutonomousService(
        config,
        runtime,
        state,
        context,
        conversations,
        messages,
        system,
        memory,
        observer,
        llm,
        style,
        delivery,
    )
    autonomous.consolidation = consolidation
    scheduled = ScheduledService(
        config, runtime, state, conversations, system, identities, autonomous
    )
    autonomous.scheduled = scheduled
    reply = ReplyService(
        config,
        runtime,
        context,
        conversations,
        messages,
        system,
        memory,
        observer,
        incoming,
        delivery,
        llm,
        style,
        autonomous,
    )
    services = AppServices(
        reply, observer, autonomous, memory, style, incoming, delivery, consolidation, scheduled
    )
    return AppContainer(
        config,
        runtime,
        db,
        state,
        context,
        memory_domain,
        system,
        conversations,
        messages,
        memories,
        styles,
        feishu_client,
        llm,
        services,
    )


def create_app(config: AppConfig | None = None) -> FastAPI:
    """uvicorn dafeiyu.app:create_app --factory. Importing this module never opens a database."""
    container = build_container(config if config is not None else load_config())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def initialize_style() -> None:
            try:
                await asyncio.wait_for(
                    container.services.style.initialize(),
                    timeout=container.config.style_retrieval["init_timeout_sec"],
                )
            except Exception as exc:
                container.services.style.initialization_error = type(exc).__name__
                log.warning("Style index unavailable: %s", type(exc).__name__)

        container.runtime.spawn(initialize_style())
        container.runtime.spawn(container.services.autonomous.run_loop())
        try:
            yield
        finally:
            await container.services.observer.flush_all()
            await container.runtime.close()
            try:
                await container.feishu.close()
            finally:
                await container.llm.close()

    app = FastAPI(title="大肥鱼", version=__version__, lifespan=lifespan)
    app.state.container = container

    @app.exception_handler(Exception)
    async def operation_failed(request, error):
        log.error("request failed path=%s error=%s", request.url.path, type(error).__name__)
        return JSONResponse(
            {"detail": "操作未完成，请查看服务日志后重试；发送状态不明时先核对群聊记录"},
            status_code=502,
        )

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/gm/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(health.router)
    app.include_router(feishu.router)
    app.include_router(gm.router)
    app.include_router(web.router)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web"), name="static")
    return app
