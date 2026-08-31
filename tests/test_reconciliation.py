"""The audit that runs between the rows and the story told about them.

Every warehouse here is built to contain one specific lie the analyst pipe would
otherwise tell, so each test fails if the corresponding check stops catching it.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pytest

from copilotdesk.agents.base import Reconciler
from copilotdesk.orchestrator.executor import AuditExecutor
from copilotdesk.orchestrator.memory import forget_all
from copilotdesk.pipeline import (
    ChartFilter,
    ExecutorFilter,
    GuardrailFilter,
    NarratorFilter,
    PlannerFilter,
    ReconcilerFilter,
    SqlBuilderFilter,
    compose,
)

CUSTOMERS = pd.DataFrame(
    {
        "customer_id": [1, 2, 3],
        "segment": ["consumer", "smb", "enterprise"],
        "region": ["north", "south", "east"],
    }
)
PRODUCTS = pd.DataFrame({"product_id": [1, 2], "category": ["widgets", "gadgets"]})


def orders(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame["order_id"] = range(1, len(frame) + 1)
    frame["product_id"] = 1
    if "category" not in frame:
        frame["category"] = "widgets"
    if "order_date" not in frame:
        frame["order_date"] = pd.Timestamp("2025-01-15")
    return frame


def make_warehouse(tmp_path: Path, fact: pd.DataFrame, customers=CUSTOMERS) -> Path:
    db = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db))
    for name, frame in {
        "dim_customers": customers,
        "dim_products": PRODUCTS,
        "fact_orders": fact,
    }.items():
        con.register(f"_{name}", frame)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _{name}")
    con.close()
    forget_all()  # a fresh warehouse must not inherit another one's totals
    return db


def analyst_over(db: Path):
    """The real topology, pointed at one purpose-built warehouse."""
    return compose(
        PlannerFilter(),
        SqlBuilderFilter(),
        GuardrailFilter(row_limit=1000),
        ExecutorFilter(db_path=db),
        ChartFilter(),
        ReconcilerFilter(db_path=db),
        NarratorFilter(),
    )


def four_regions() -> pd.DataFrame:
    return orders(
        [
            {"customer_id": 1, "region": "north", "revenue": 400.0},
            {"customer_id": 2, "region": "south", "revenue": 300.0},
            {"customer_id": 3, "region": "east", "revenue": 200.0},
            {"customer_id": 1, "region": "west", "revenue": 100.0},
        ]
    )


def test_truncated_top_n_quotes_the_population_not_the_visible_rows(tmp_path):
    """The bug that motivated the whole stage: LIMIT 3 hid a quarter of the money."""
    db = make_warehouse(tmp_path, four_regions())
    env = analyst_over(db)("Top 3 regions by revenue")
    assert not env.halted

    narrative = env.get("narrative")
    # north is 400 of a 1000 population, not 400 of the 900 that survived the LIMIT.
    assert "40% of the 1,000 warehouse total" in narrative
    assert round(400 / 900 * 100) == 44  # what quoting the shown rows would have said
    assert "cover 90% of that total" in narrative

    population = env.get("evidence")["population"]
    assert population == {
        "population_total": 1000.0,
        "shown_total": 900.0,
        "members_total": 4,
        "members_shown": 3,
        "covered_share": 0.9,
        "truncated": True,
    }
    assert env.get("verdict") == "verified"


def test_join_that_drops_facts_is_caught_and_quantified(tmp_path):
    """Customer 4 is not in dim_customers, so the segment join silently loses 200."""
    fact = orders(
        [
            {"customer_id": 1, "region": "north", "revenue": 500.0},
            {"customer_id": 2, "region": "south", "revenue": 300.0},
            {"customer_id": 4, "region": "east", "revenue": 200.0},
        ]
    )
    db = make_warehouse(tmp_path, fact)
    env = analyst_over(db)("Show revenue by segment")

    assert env.get("verdict") == "qualified"
    findings = {check["check"]: check for check in env.get("checks")}
    assert findings["join_integrity"]["status"] == "warn"
    assert findings["join_integrity"]["evidence"] == {
        "fact_rows": 3,
        "orphan_rows": 1,
        "orphan_share": 0.3333,
    }
    assert findings["population"]["status"] == "warn"
    assert findings["population"]["evidence"]["missing_total"] == 200.0
    # the answer still ships - it just stops pretending 800 is the whole story
    assert env.get("data")[0]["revenue"] == 500.0
    assert "Caveat" in env.get("narrative")
    assert "not in dim_customers" in env.get("narrative")
    assert "never reached the answer" in env.get("narrative")


def test_null_revenue_deflates_aov_and_the_check_says_by_how_much(tmp_path):
    """SUM skips a NULL revenue; COUNT(*) counts the row anyway, so aov drifts."""
    fact = orders(
        [
            {"customer_id": 1, "region": "north", "revenue": 100.0},
            {"customer_id": 2, "region": "south", "revenue": None},
            {"customer_id": 3, "region": "east", "revenue": 200.0},
            {"customer_id": 1, "region": "west", "revenue": None},
        ]
    )
    db = make_warehouse(tmp_path, fact)
    env = analyst_over(db)("Average order value")

    assert env.get("data")[0]["aov"] == 75.0  # 300 / 4 rows
    finding = next(c for c in env.get("checks") if c["check"] == "null_metric")
    assert finding["status"] == "warn"
    assert finding["evidence"]["null_metric_rows"] == 2
    assert finding["evidence"]["reported_aov"] == 75.0
    assert finding["evidence"]["aov_over_priced_rows"] == 150.0  # 300 / 2 priced rows
    assert env.get("verdict") == "qualified"


def test_ratio_metric_is_refused_rather_than_reconciled(tmp_path):
    """Averages do not add up, so the population check declines and says why."""
    db = make_warehouse(tmp_path, four_regions())
    env = analyst_over(db)("Average order value by region")

    skipped = {item["check"]: item["reason"] for item in env.get("unchecked")}
    assert "ratio" in skipped["population"]
    assert [c["check"] for c in env.get("checks")] == ["null_metric"]
    # and the narrator must not quote a share it has no denominator for
    assert "%" not in env.get("narrative")
    assert "highest aov" in env.get("narrative")


def test_trend_with_a_missing_month_is_not_reported_as_continuous(tmp_path):
    """Three rows spanning four months is a series with a hole in it."""
    fact = orders(
        [
            {"customer_id": 1, "region": "north", "revenue": 100.0, "order_date": "2025-01-10"},
            {"customer_id": 2, "region": "south", "revenue": 200.0, "order_date": "2025-02-10"},
            {"customer_id": 3, "region": "east", "revenue": 300.0, "order_date": "2025-04-10"},
        ]
    )
    fact["order_date"] = pd.to_datetime(fact["order_date"])
    db = make_warehouse(tmp_path, fact)
    env = analyst_over(db)("Show monthly revenue")

    finding = next(c for c in env.get("checks") if c["check"] == "continuity")
    assert finding["status"] == "warn"
    assert finding["evidence"] == {
        "grain": "month",
        "periods_expected": 4,
        "periods_returned": 3,
        "periods_missing": 1,
    }
    assert env.get("verdict") == "qualified"
    assert "3 months" in env.get("narrative")  # the headline still counts what it has
    assert "Caveat" in env.get("narrative")


def test_an_answer_nothing_can_check_is_reported_as_unverified(tmp_path):
    """ "Unverified" is a real state: no check refused *and* none of them applied."""
    db = make_warehouse(tmp_path, four_regions())
    env = analyst_over(db)("How many orders were placed?")

    assert env.get("checks") == []
    assert env.get("verdict") == "unverified"
    reasons = {item["check"]: item["reason"] for item in env.get("unchecked")}
    assert set(reasons) == {"null_metric", "join_integrity"}
    assert "counts rows" in reasons["null_metric"]


def test_baselines_are_reused_across_answers_until_the_warehouse_changes(tmp_path):
    """Population figures do not depend on the question, so they are paid for once."""
    db = make_warehouse(tmp_path, four_regions())
    analyst = analyst_over(db)

    first = analyst("Top 3 regions by revenue").trace[-2].output
    assert first["queries"] == 4 and first["cache_hits"] == 0
    assert first["connections"] == 1

    # a different question needing the same totals scans nothing at all - and
    # never opens the database, which is where the time actually went
    second = analyst("Show revenue by region").trace[-2].output
    assert second["queries"] == 0 and second["cache_hits"] == 4
    assert second["connections"] == 0

    forget_all()
    third = analyst("Show revenue by region").trace[-2].output
    assert third["queries"] == 4 and third["connections"] == 1


def test_a_reconciler_that_blows_up_becomes_a_warning_not_a_pass(tmp_path):
    class Exploding(Reconciler):
        name = "exploding"
        claim = "nothing, it never gets that far"

        def check(self, ctx):
            raise RuntimeError("boom")

    db = make_warehouse(tmp_path, four_regions())
    auditor = AuditExecutor(db_path=db, reconcilers=[Exploding()])
    result = auditor.run(
        {"intent": "breakdown", "metric": "revenue", "dimension": "region", "grain": "day"},
        pd.DataFrame({"region": ["north"], "revenue": [400.0]}),
    )
    assert result.verdict == "qualified"
    assert result.findings[0].status == "warn"
    assert "boom" in result.findings[0].detail


def test_top_n_without_a_dimension_is_rejected_with_a_readable_reason(tmp_path):
    """It used to interpolate a literal None and die inside the DuckDB binder."""
    db = make_warehouse(tmp_path, four_regions())
    env = analyst_over(db)("top 5")

    assert env.halted
    assert "needs a dimension to group by" in env.error
    assert [entry.name for entry in env.trace] == ["planner", "sql_builder"]
    assert "Binder Error" not in env.error


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Show revenue by region", "verified"),
        ("Top 3 regions by revenue", "verified"),
        ("How many orders were placed?", "unverified"),
    ],
)
def test_every_answer_carries_a_verdict(tmp_path, question, expected):
    db = make_warehouse(tmp_path, four_regions())
    env = analyst_over(db)(question)
    assert env.get("verdict") == expected
