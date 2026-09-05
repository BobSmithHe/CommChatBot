from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import auth, chat, code, conversations, health, knowledge, memories, platform, tasks
from .bootstrap import get_container
from .infra.config import get_settings
from .platform.database import init_db
from .extensions.builtin.terminal import cleanup_orphaned_terminal_containers, terminal_manager
from .extensions.builtin.workspace.lsp import lsp_manager
from .platform.services.task_queue import task_queue
from .worker import run_worker
from .platform.services.notifications import notification_dispatcher
from .platform.lifecycle import BackgroundService, PlatformRuntime

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    container = get_container()
    cleanup_orphaned_terminal_containers()
    init_db(recover_tasks=settings.agent_worker_mode.casefold() != "external")
    background_services = [
        BackgroundService("scheduler", container.automation_scheduler.run),
        BackgroundService("notifications", notification_dispatcher.run),
    ]
    if settings.agent_worker_mode.casefold() == "embedded":
        task_queue.wake_recovered()
        async def embedded_worker() -> None:
            await run_worker(initialize=False)

        background_services.append(BackgroundService("agent-worker", embedded_worker))
    platform_runtime = PlatformRuntime(background_services)
    await platform_runtime.start()
    try:
        yield
    finally:
        await platform_runtime.stop()
        terminal_manager.close_all()
        lsp_manager.close_all()
        await container.extension_services["mcp"].close()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[item.strip() for item in settings.cors_origins.split(",") if item.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(conversations.router)
    app.include_router(chat.router)
    app.include_router(knowledge.router)
    app.include_router(code.router)
    app.include_router(tasks.router)
    app.include_router(memories.router)
    app.include_router(platform.router)
    return app


app = create_app()
