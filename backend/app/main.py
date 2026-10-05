"""ParkWise FastAPI application.

Everything is mounted under ``/api/v1`` so the Vercel rewrite can send the whole
``/api`` prefix to this single serverless function. Public reads need no auth;
mutating and admin routes are added in Phase 5 behind the same
``X-Admin-Token`` gate Ghost Space uses, so there is one convention across both
projects.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import lots as lots_api
from app.core.config import settings
from app.core.database import database_kind, init_db, ping
from app.schemas.core import HealthOut

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
logger = logging.getLogger("parkwise")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A cold Neon can take a few seconds to accept connections, so init_db
    # retries rather than failing the first request.
    init_db()
    if settings.auto_seed:
        from app.db.seed import seed
        from app.core.database import SessionLocal

        with SessionLocal() as db:
            seed(db)
    logger.info("ParkWise ready: database=%s", database_kind())
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description=(
        "Priority-based campus parking allocation. Tiers are reserved quotas, "
        "never evictions, and every decision carries its explanation."
    ),
    lifespan=lifespan,
)

# Same-origin in production (Vercel rewrites /api/* to this function), so CORS
# is only here to make `npm run dev` on :5173 work against :8000 locally.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(lots_api.router, prefix=settings.api_prefix)


@app.get(f"{settings.api_prefix}/health", response_model=HealthOut, tags=["system"])
def health() -> HealthOut:
    return HealthOut(
        status="ok" if ping() else "degraded",
        app=settings.app_name,
        version=settings.version,
        environment=settings.environment,
        database=database_kind(),
        nodes=True,
    )


@app.get("/", tags=["system"])
def root() -> dict[str, str]:
    return {"app": settings.app_name, "docs": "/docs", "health": f"{settings.api_prefix}/health"}
