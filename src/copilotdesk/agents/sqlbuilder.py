"""SQL builder + AST guardrails (read-only, single SELECT, LIMIT injected)."""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from copilotdesk.settings import get_config

# metric expressions already qualified against fact_orders alias `o`
METRIC_SQL = {
    "revenue": "ROUND(SUM(o.revenue), 2) AS revenue",
    "orders": "COUNT(*) AS orders",
    "aov": "ROUND(SUM(o.revenue) / COUNT(*), 2) AS aov",
}
DIM_COL = {"region": "region", "category": "category", "segment": "segment"}

FORBIDDEN = {
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Command,
}


def build_sql(plan: dict) -> str:
    metric_expr = METRIC_SQL[plan["metric"]]
    base = "fact_orders o"
    join_segment = ""
    if plan["dimension"] == "segment":
        join_segment = " JOIN dim_customers c ON o.customer_id = c.customer_id"
        dim_ref = "c.segment"
    elif plan["dimension"]:
        dim_ref = f"o.{DIM_COL[plan['dimension']]}"
    else:
        dim_ref = None

    if plan["intent"] == "kpi":
        return f"SELECT {metric_expr} FROM {base}{join_segment}"
    if plan["intent"] == "trend":
        grain = "month" if plan["grain"] == "month" else "day"
        bucket = f"DATE_TRUNC('{grain}', o.order_date) AS period"
        return (
            f"SELECT {bucket}, {metric_expr} FROM {base}{join_segment} "
            f"GROUP BY period ORDER BY period"
        )
    # breakdown / top_n
    if dim_ref is None:
        # "top 5" with nothing to rank by used to interpolate the literal None
        # into the SELECT list and die inside DuckDB's binder. Refuse here, so
        # the pipe halts with a reason a caller can read.
        raise ValueError(
            f"a {plan['intent']} query needs a dimension to group by; "
            f"supported dimensions are {', '.join(sorted(DIM_COL))}"
        )
    order = f"ORDER BY {plan['metric']} DESC"
    limit = f" LIMIT {plan['top_n']}" if plan["intent"] == "top_n" and plan["top_n"] else ""
    return (
        f"SELECT {dim_ref} AS {plan['dimension']}, {metric_expr} FROM {base}{join_segment} "
        f"GROUP BY {dim_ref} {order}{limit}"
    )


def guard_sql(sql: str, row_limit: int | None = None) -> dict:
    """Parse + validate: single statement, SELECT only, no DDL/DML; inject a LIMIT."""
    if row_limit is None:
        row_limit = get_config()["agent"]["row_limit"]
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.ParseError as exc:
        return {"ok": False, "reason": f"parse error: {exc}", "sql": sql}
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        return {"ok": False, "reason": "exactly one statement required", "sql": sql}
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        return {"ok": False, "reason": "only SELECT statements allowed", "sql": sql}
    for node in tree.walk():
        if type(node) in FORBIDDEN:
            return {"ok": False, "reason": f"forbidden operation {type(node).__name__}", "sql": sql}

    if not tree.args.get("limit"):
        tree = tree.limit(row_limit)
    return {"ok": True, "reason": "validated", "sql": tree.sql(dialect="duckdb")}
