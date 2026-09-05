from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import auth, chat, code, conversations, health, knowledge, memories, tasks
from .infra.config import get_settings
from .infra.database import init_db
from .core.workspace.terminal import cleanup_orphaned_terminal_containers, terminal_manager
from .core.workspace.lsp import lsp_manager
from .core.task_queue import task_queue
from .worker import run_worker

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cleanup_orphaned_terminal_containers()
    init_db(recover_tasks=settings.agent_worker_mode.casefold() != "external")
    worker_task = None
    if settings.agent_worker_mode.casefold() == "embedded":
        task_queue.wake_recovered()
        worker_task = asyncio.create_task(run_worker(initialize=False))
    try:
        yield
    finally:
        if worker_task:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        terminal_manager.close_all()
        lsp_manager.close_all()


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
    return app


app = create_app()
