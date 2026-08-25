"""The filters of the analyst pipe: plan -> SQL -> guard -> execute -> chart -> narrate.

Each filter is an independent transformation on an :class:`Envelope`. It reads
only what upstream filters put in the payload, adds its own keys, and emits the
value that becomes its trace entry. Filters never call each other, never mutate
the envelope, and never know their position in the chain - which is what makes
the chain reorderable and each stage testable on a hand-built envelope.

``Filter`` is the structural contract; ``BaseFilter`` is the template method
that every concrete filter here reuses: halt pass-through, timing, trace
recording and failure containment happen once, in one place.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

import duckdb
import pandas as pd

from copilotdesk.agents.planner import plan
from copilotdesk.agents.sqlbuilder import build_sql, guard_sql
from copilotdesk.pipeline.envelope import Emission, Envelope, TraceEntry
from copilotdesk.settings import get_config, resolve_path

PREVIEW_ROWS = 50


@runtime_checkable
class Filter(Protocol):
    """A named, side-effect-free transformation of one envelope into the next."""

    name: str

    def apply(self, envelope: Envelope) -> Envelope: ...


class BaseFilter(ABC):
    """Template method supplying the invariants every filter must honour."""

    name: str = "filter"

    def apply(self, envelope: Envelope) -> Envelope:
        if envelope.halted:
            return envelope  # a halted envelope flows through untouched

        started = time.perf_counter()
        try:
            emission = self.process(envelope)
        except Exception as exc:  # a filter blowing up must not destroy the trace
            elapsed = (time.perf_counter() - started) * 1000
            reason = f"{self.name} failed: {exc}"
            entry = TraceEntry(self.name, {"error": reason}, round(elapsed, 3))
            return envelope.with_trace_entry(entry).halted_with(reason)

        elapsed = (time.perf_counter() - started) * 1000
        entry = TraceEntry(self.name, emission.trace, round(elapsed, 3))
        nxt = envelope.with_payload(**emission.payload).with_trace_entry(entry)
        return nxt.halted_with(emission.error) if emission.error else nxt

    @abstractmethod
    def process(self, envelope: Envelope) -> Emission:
        """Compute this stage's payload delta and trace output."""


class PlannerFilter(BaseFilter):
    """Route the natural-language question to an intent, metric and dimension."""

    name = "planner"

    def process(self, envelope: Envelope) -> Emission:
        plan_obj = plan(envelope.question)
        return Emission(trace=plan_obj, payload={"plan": plan_obj, "intent": plan_obj["intent"]})


class SqlBuilderFilter(BaseFilter):
    """Compose a parameter-free SQL query from the typed plan."""

    name = "sql_builder"

    def process(self, envelope: Envelope) -> Emission:
        raw_sql = build_sql(envelope.require("plan"))
        return Emission(trace=raw_sql, payload={"raw_sql": raw_sql})


class GuardrailFilter(BaseFilter):
    """Validate the SQL against the sqlglot AST and halt the pipe if it fails.

    This is the only filter that can reject: everything downstream of it is
    allowed to assume it is operating on governed, LIMIT-bounded SQL.
    """

    name = "guardrail"

    def __init__(self, row_limit: int | None = None) -> None:
        self._row_limit = row_limit

    @property
    def row_limit(self) -> int:
        # Resolved per call so tests can repoint configuration at a temp warehouse.
        return (
            self._row_limit if self._row_limit is not None else get_config()["agent"]["row_limit"]
        )

    def process(self, envelope: Envelope) -> Emission:
        verdict = guard_sql(envelope.require("raw_sql"), row_limit=self.row_limit)
        if not verdict["ok"]:
            return Emission(trace=verdict, error=verdict["reason"])
        return Emission(trace=verdict, payload={"sql": verdict["sql"]})


class ExecutorFilter(BaseFilter):
    """Run the governed SQL on a read-only DuckDB connection."""

    name = "executor"

    def __init__(self, db_path: Path | str | None = None, preview_rows: int = PREVIEW_ROWS) -> None:
        self._db_path = db_path
        self.preview_rows = preview_rows

    @property
    def db_path(self) -> Path:
        if self._db_path is not None:
            return Path(self._db_path)
        return resolve_path(get_config()["data"]["db_path"])

    def process(self, envelope: Envelope) -> Emission:
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            frame = con.execute(envelope.require("sql")).fetchdf()
        finally:
            con.close()
        columns = list(frame.columns)
        return Emission(
            trace={"rows": len(frame), "columns": columns},
            payload={
                "frame": frame,
                "columns": columns,
                "data": frame.head(self.preview_rows).to_dict(orient="records"),
            },
        )


class ChartFilter(BaseFilter):
    """Pick the visual encoding implied by the intent."""

    name = "chart"

    CHART_BY_INTENT: ClassVar[dict[str, str]] = {
        "trend": "line",
        "breakdown": "bar",
        "top_n": "bar",
        "kpi": "metric",
    }

    def process(self, envelope: Envelope) -> Emission:
        chart = self.CHART_BY_INTENT.get(envelope.require("intent"), "metric")
        return Emission(trace=chart, payload={"chart": chart})


class NarratorFilter(BaseFilter):
    """Write a takeaway computed from the returned rows - never invented."""

    name = "narrator"

    def process(self, envelope: Envelope) -> Emission:
        narrative = _narrate(envelope.question, envelope.require("plan"), envelope.require("frame"))
        return Emission(trace=narrative, payload={"narrative": narrative})


def _narrate(question: str, plan_obj: dict[str, Any], df: pd.DataFrame) -> str:
    if df.empty:
        return "No rows matched the query."
    if plan_obj["intent"] == "kpi":
        col = df.columns[-1]
        return f"{question.rstrip('?')}: **{df.iloc[0][col]:,.2f}**."
    metric = plan_obj["metric"]
    if plan_obj["intent"] == "trend":
        first, last = df.iloc[0], df.iloc[-1]
        direction = "up" if last[metric] > first[metric] else "down"
        change = (last[metric] / max(first[metric], 1e-9) - 1) * 100
        return (
            f"{metric.title()} trended {direction} {abs(change):.0f}% across "
            f"{len(df)} periods, from {first[metric]:,.0f} to {last[metric]:,.0f}."
        )
    dim = plan_obj["dimension"]
    top = df.iloc[0]
    share = top[metric] / df[metric].sum() * 100
    return (
        f"Across {len(df)} {dim}s, **{top[dim]}** leads on {metric} "
        f"({top[metric]:,.0f}, {share:.0f}% of total)."
    )
