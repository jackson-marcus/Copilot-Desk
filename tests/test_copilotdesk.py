"""Planner routing, SQL guardrails, end-to-end answers, eval metrics, API."""

from __future__ import annotations

from copilotdesk.agents.planner import plan
from copilotdesk.agents.sqlbuilder import build_sql, guard_sql


def test_planner_routes_intents():
    assert plan("What is total revenue?")["intent"] == "kpi"
    assert plan("Show revenue by region")["intent"] == "breakdown"
    assert plan("What is the revenue trend over time?")["intent"] == "trend"
    top = plan("Top 5 categories by revenue")
    assert top["intent"] == "top_n" and top["top_n"] == 5 and top["dimension"] == "category"


def test_guardrails_block_writes_and_inject_limit():
    ok = guard_sql("SELECT region, SUM(revenue) FROM fact_orders o GROUP BY region")
    assert ok["ok"] and "LIMIT" in ok["sql"].upper()

    for bad in [
        "DROP TABLE fact_orders",
        "DELETE FROM fact_orders",
        "SELECT 1; SELECT 2",
        "UPDATE fact_orders SET revenue = 0",
    ]:
        assert not guard_sql(bad)["ok"], bad


def test_build_sql_shapes_match_intent():
    kpi = build_sql(plan("total revenue"))
    assert "GROUP BY" not in kpi and "SUM(o.revenue)" in kpi

    trend = build_sql(plan("monthly revenue trend"))
    assert "DATE_TRUNC" in trend and "period" in trend

    topn = build_sql(plan("top 3 regions by revenue"))
    assert "LIMIT 3" in topn and "ORDER BY revenue DESC" in topn


def test_end_to_end_answers(warehouse):
    from copilotdesk.agents.pipeline import answer

    kpi = answer("What is total revenue?")
    assert "error" not in kpi and kpi["chart"] == "metric"
    assert kpi["data"][0]["revenue"] > 0
    assert len(kpi["trace"]) == 6  # planner, sql, guard, exec, chart, narrator

    breakdown = answer("Show revenue by region")
    assert breakdown["chart"] == "bar" and len(breakdown["data"]) == 4  # 4 regions
    assert "leads on revenue" in breakdown["narrative"]

    trend = answer("Show monthly revenue")
    assert trend["chart"] == "line" and len(trend["data"]) == 12  # 12 months


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
    assert "LIMIT" in body["sql"].upper()
    assert len(body["trace"]) == 6

    report = api_client.get("/report").json()
    assert report["metrics"]["intent_accuracy"] >= 0.9
