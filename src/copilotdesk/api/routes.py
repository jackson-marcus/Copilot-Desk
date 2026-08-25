"""API routes: /ask, /report, /schema, /health."""

from __future__ import annotations

import functools
import logging
import pickle

import duckdb
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from copilotdesk.pipeline import as_answer, build_analyst_pipeline
from copilotdesk.settings import get_config, resolve_path

logger = logging.getLogger(__name__)
router = APIRouter()

# The topology is fixed at import time; only envelopes flow through it per request.
ANALYST = build_analyst_pipeline()


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=400)


@functools.lru_cache(maxsize=1)
def _report() -> dict:
    path = resolve_path(get_config()["data"]["artifacts_dir"]) / "report.pkl"
    if not path.exists():
        raise FileNotFoundError(
            "Report missing; run make_warehouse.py + copilotdesk.agents.evaluate"
        )
    with open(path, "rb") as f:
        return pickle.load(f)


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/schema")
def schema() -> dict:
    db = resolve_path(get_config()["data"]["db_path"])
    if not db.exists():
        raise HTTPException(status_code=503, detail="warehouse missing; run make_warehouse.py")
    con = duckdb.connect(str(db), read_only=True)
    try:
        tables = con.execute("SELECT table_name FROM information_schema.tables").fetchall()
        out = {}
        for (table,) in tables:
            cols = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ?",
                [table],
            ).fetchall()
            out[table] = [{"column": c, "type": t} for c, t in cols]
    finally:
        con.close()
    return {"tables": out}


@router.post("/ask")
def ask(request: AskRequest) -> dict:
    db = resolve_path(get_config()["data"]["db_path"])
    if not db.exists():
        raise HTTPException(status_code=503, detail="warehouse missing; run make_warehouse.py")
    envelope = ANALYST(request.question)
    if envelope.halted:
        logger.warning("pipeline halted at %s: %s", envelope.trace[-1].name, envelope.error)
        raise HTTPException(status_code=422, detail=f"could not answer: {envelope.error}")
    return as_answer(envelope)


@router.get("/report")
def report() -> dict:
    try:
        return _report()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
