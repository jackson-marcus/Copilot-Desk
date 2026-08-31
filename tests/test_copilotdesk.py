"""Envelope immutability, filter behaviour, pipeline composition, eval metrics, API."""

from __future__ import annotations

import pandas as pd
import pytest

from copilotdesk.agents.planner import plan
from copilotdesk.agents.sqlbuilder import build_sql, guard_sql
from copilotdesk.pipeline import (
    ANSWER_KEYS,
    ChartFilter,
    Filter,
    GuardrailFilter,
    NarratorFilter,
    PlannerFilter,
    SqlBuilderFilter,
    as_answer,
    build_analyst_pipeline,
    compose,
)
from copilotdesk.pipeline.envelope import Emission, Envelope, TraceEntry
from copilotdesk.pipeline.filters import BaseFilter

STAGES = (
    "planner",
    "sql_builder",
    "guardrail",
    "executor",
    "chart",
    "reconciler",
    "narrator",
)


# --------------------------------------------------------------------------- #
# Envelope: the immutability guarantees the whole pattern rests on
# --------------------------------------------------------------------------- #


def test_envelope_derivations_never_mutate_the_original():
    base = Envelope(question="What is total revenue?")
    grown = base.with_payload(intent="kpi").with_trace_entry(TraceEntry("planner", {}, 0.1))

    assert base.payload == {} and base.trace == ()
    assert grown.payload["intent"] == "kpi" and len(grown.trace) == 1
    assert grown.question == base.question
    assert not base.halted and not grown.halted

    halted = grown.halted_with("nope")
    assert halted.halted and halted.error == "nope"
    assert not grown.halted  # deriving a halt left the source alone


def test_envelope_payload_rejects_in_place_mutation():
    env = Envelope(question="q").with_payload(intent="kpi")
    with pytest.raises(TypeError):
        env.payload["intent"] = "trend"  # type: ignore[index]


def test_envelope_require_names_the_missing_key():
    with pytest.raises(KeyError, match="plan"):
        Envelope(question="q").require("plan")


# --------------------------------------------------------------------------- #
# Filters: each one testable in isolation on a hand-built envelope
# --------------------------------------------------------------------------- #


def test_filters_satisfy_the_protocol_and_are_named():
    filters = build_analyst_pipeline().filters
    assert tuple(f.name for f in filters) == STAGES
    for filt in filters:
        assert isinstance(filt, Filter)


def test_planner_filter_routes_intents():
    def routed(question: str) -> Envelope:
        return PlannerFilter().apply(Envelope(question=question))

    assert routed("What is total revenue?").get("intent") == "kpi"
    assert routed("Show revenue by region").get("intent") == "breakdown"
    assert routed("What is the revenue trend over time?").get("intent") == "trend"

    top = routed("Top 5 categories by revenue")
    assert top.get("intent") == "top_n"
    assert top.get("plan")["top_n"] == 5 and top.get("plan")["dimension"] == "category"

    # the filter also leaves exactly one trace entry behind, timed
    assert [e.name for e in top.trace] == ["planner"]
    assert top.trace[0].duration_ms >= 0


def test_sql_builder_filter_shapes_sql_per_intent():
    def sql_for(question: str) -> str:
        env = compose(PlannerFilter(), SqlBuilderFilter())(question)
        return env.get("raw_sql")

    kpi = sql_for("total revenue")
    assert "GROUP BY" not in kpi and "SUM(o.revenue)" in kpi

    trend = sql_for("monthly revenue trend")
    assert "DATE_TRUNC" in trend and "period" in trend

    topn = sql_for("top 3 regions by revenue")
    assert "LIMIT 3" in topn and "ORDER BY revenue DESC" in topn


def test_guardrail_filter_injects_limit_and_halts_on_writes():
    guard = GuardrailFilter(row_limit=1000)

    ok = guard.apply(
        Envelope(question="q").with_payload(
            raw_sql="SELECT region, SUM(revenue) FROM fact_orders o GROUP BY region"
        )
    )
    assert not ok.halted and "LIMIT" in ok.get("sql").upper()

    for bad in [
        "DROP TABLE fact_orders",
        "DELETE FROM fact_orders",
        "SELECT 1; SELECT 2",
        "UPDATE fact_orders SET revenue = 0",
    ]:
        rejected = guard.apply(Envelope(question="q").with_payload(raw_sql=bad))
        assert rejected.halted, bad
        # the rejection is recorded before the halt, so the trace explains itself
        assert [e.name for e in rejected.trace] == ["guardrail"], bad
        assert rejected.error == rejected.trace[0].output["reason"], bad


def test_chart_filter_maps_intent_to_encoding():
    chart = ChartFilter()
    for intent, expected in [
        ("trend", "line"),
        ("breakdown", "bar"),
        ("top_n", "bar"),
        ("kpi", "metric"),
    ]:
        env = chart.apply(Envelope(question="q").with_payload(intent=intent))
        assert env.get("chart") == expected


# --------------------------------------------------------------------------- #
# Runner: halt propagation and failure containment are structural, not per-filter
# --------------------------------------------------------------------------- #


def test_halted_envelope_passes_through_downstream_filters_untouched():
    halted = Envelope(question="q").halted_with("blocked upstream")
    for filt in build_analyst_pipeline().filters:
        assert filt.apply(halted) is halted


def test_filter_exception_halts_the_pipe_but_preserves_the_trace():
    class Exploding(BaseFilter):
        name = "exploding"

        def process(self, envelope: Envelope) -> Emission:
            raise RuntimeError("boom")

    class NeverRuns(BaseFilter):
        name = "never_runs"

        def process(self, envelope: Envelope) -> Emission:  # pragma: no cover
            raise AssertionError("downstream of a halt must not execute")

    env = compose(PlannerFilter(), Exploding(), NeverRuns())("What is total revenue?")
    assert env.halted and "boom" in env.error
    assert [e.name for e in env.trace] == ["planner", "exploding"]


def test_pipeline_is_reorderable_and_extensible():
    pipe = compose(PlannerFilter(), SqlBuilderFilter())
    assert pipe.stage_names == ("planner", "sql_builder")

    extended = pipe.then(GuardrailFilter(row_limit=10))
    assert extended.stage_names == ("planner", "sql_builder", "guardrail")
    assert pipe.stage_names == ("planner", "sql_builder")  # original untouched


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


def test_end_to_end_answers(warehouse):
    analyst = build_analyst_pipeline()

    kpi = as_answer(analyst("What is total revenue?"))
    assert "error" not in kpi and kpi["chart"] == "metric"
    assert kpi["data"][0]["revenue"] > 0
    assert [step["agent"] for step in kpi["trace"]] == list(STAGES)

    breakdown = as_answer(analyst("Show revenue by region"))
    assert breakdown["chart"] == "bar" and len(breakdown["data"]) == 4  # 4 regions
    assert "leads on revenue" in breakdown["narrative"]
    assert breakdown["verdict"] == "verified"

    trend = as_answer(analyst("Show monthly revenue"))
    assert trend["chart"] == "line" and len(trend["data"]) == 12  # 12 months


def test_as_answer_projects_only_the_public_keys(warehouse):
    env = build_analyst_pipeline()("Show revenue by region")
    body = as_answer(env)

    assert set(body) == {"question", "trace", *ANSWER_KEYS}
    # internal payload the filters pass between themselves stays off the wire
    assert "frame" in env.payload and "plan" in env.payload
    assert "frame" not in body and "plan" not in body


class _EmptyExecutor(BaseFilter):
    """Stands in for the real executor to prove the narrator is row-driven."""

    name = "executor"

    def process(self, envelope: Envelope) -> Emission:
        frame = pd.DataFrame(columns=["region", "revenue"])
        return Emission(trace={"rows": 0, "columns": list(frame.columns)}, payload={"frame": frame})


def test_narrator_reports_empty_results_without_inventing():
    """Swapping one filter out is the whole change - nothing else is touched."""
    empty = compose(
        PlannerFilter(),
        SqlBuilderFilter(),
        GuardrailFilter(),
        _EmptyExecutor(),
        NarratorFilter(),
    )("Show revenue by region")
    assert empty.get("narrative") == "No rows matched the query."


def test_evaluation_metrics(warehouse):
    m = warehouse["metrics"]
    assert m["intent_accuracy"] >= 0.9
    assert m["guardrail_pass_rate"] == 1.0
    assert m["execution_rate"] == 1.0


def test_api_contract(api_client):
    assert api_client.get("/health").json() == {"status": "ok"}

    schema = api_client.get("/schema").json()
    assert "fact_orders" in schema["tables"]

    body = api_client.post("/ask", json={"question": "Top 5 categories by revenue"}).json()
    assert body["intent"] == "top_n" and body["chart"] == "bar"
    assert body["verdict"] == "verified"
    assert {c["check"] for c in body["checks"]} == {"population", "null_metric"}
    assert "LIMIT" in body["sql"].upper()
    assert [step["agent"] for step in body["trace"]] == list(STAGES)
    assert all("duration_ms" in step for step in body["trace"])

    report = api_client.get("/report").json()
    assert report["metrics"]["intent_accuracy"] >= 0.9


# --------------------------------------------------------------------------- #
# The pure stage logic the filters delegate to
# --------------------------------------------------------------------------- #


def test_guard_sql_row_limit_is_injectable():
    assert "LIMIT 7" in guard_sql("SELECT 1 AS x", row_limit=7)["sql"]
    assert build_sql(plan("total revenue")).startswith("SELECT")
