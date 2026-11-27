"""FastAPI app: API routers, built frontend (SPA) and lifespan wiring."""

from __future__ import annotations

import logging
import mimetypes
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import drawings, health, jobs, tools
from app.config import PROJECT_ROOT, Settings, get_settings
from app.db import JobStore
from app.services.deepseek_client import LLMClient, make_client
from app.services.pipeline import Pipeline

FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
mimetypes.add_type("application/manifest+json", ".webmanifest")  # not in the Windows registry by default
mimetypes.add_type("image/svg+xml", ".svg")
log = logging.getLogger("pdf_to_step")


def create_app(settings: Settings | None = None, llm: LLMClient | None = None, use_process_pool: bool = True) -> FastAPI:
    settings = settings or get_settings()
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        store = JobStore(settings.db_path)
        await store.init()
        n = await store.recover_interrupted()
        if n:
            log.warning("marked %d interrupted job(s) as failed", n)
        factory = None
        if use_process_pool:
            def factory(workers: int | None = None):
                return ProcessPoolExecutor(max_workers=workers or max(1, settings.cad_workers),
                                           mp_context=multiprocessing.get_context("spawn"))
        client = llm or make_client(settings)
        app.state.settings = settings
        app.state.store = store
        app.state.pipeline = Pipeline(settings, store, client, factory)
        app.state.pipeline.warmup()  # import OCCT in the workers now so PATCH /spec rebuilds stay ~1 s
        log.info("DeepSeek: %s | data dir: %s", "enabled" if settings.llm_enabled else "MOCK (no API key)",
                 settings.data_dir)
        try:
            yield
        finally:
            await app.state.pipeline.shutdown()

    app = FastAPI(title="PDF-to-STEP CNC Generator", version="1.0.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(drawings.router)
    app.include_router(tools.router)

    if FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            if full_path.startswith("api/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            candidate = (FRONTEND_DIST / full_path).resolve()
            if full_path and FRONTEND_DIST.resolve() in candidate.parents and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port)
