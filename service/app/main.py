"""FastAPI application.

The service is the logic layer; n8n is the orchestration layer. The split is
deliberate: branching, scheduling, retries and fan-out are what n8n is good
at and are visible on its canvas, while normalization, deduplication,
provider fallback and the audit rules are code, because they need tests and
a diff history.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import get_settings
from .deps import AppState
from .routers import leads, ops, seo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("leadbridge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.services = AppState(settings)
    logger.info(
        "leadbridge up: llm=%s crm=%s auth=%s",
        app.state.services.llm.active(),
        app.state.services.crm.active_name(),
        "enforced" if settings.ingest_secret else "open",
    )
    try:
        yield
    finally:
        await app.state.services.aclose()


app = FastAPI(
    title="LeadBridge",
    version="0.1.0",
    summary="Lead intake, triage, CRM upsert and technical SEO audit, "
    "orchestrated from n8n.",
    lifespan=lifespan,
)

app.include_router(ops.router)
app.include_router(leads.router)
app.include_router(seo.router)


@app.get("/", tags=["ops"])
async def root() -> dict[str, str]:
    return {"service": "leadbridge", "docs": "/docs", "health": "/ops/health"}
