"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from copilotdesk import __version__
from copilotdesk.api.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app() -> FastAPI:
    app = FastAPI(
        title="copilotdesk",
        description="Multi-agent analytics copilot: a planner routes a natural-language question to a SQL builder over a DuckDB star schema with AST guardrails, a chart recommender, and a grounded narrative writer — every step traced and verifiable.",
        version=__version__,
    )
    app.include_router(router)
    return app


app = create_app()
