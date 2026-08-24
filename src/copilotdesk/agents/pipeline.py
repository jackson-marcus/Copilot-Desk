"""The analyst pipeline: plan -> build SQL -> guard -> execute -> chart -> narrate.

Every stage appends to a trace so the whole answer is auditable end-to-end.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from copilotdesk.agents.planner import plan
from copilotdesk.agents.sqlbuilder import build_sql, guard_sql
from copilotdesk.settings import get_config, resolve_path


def recommend_chart(intent: str, columns: list[str]) -> str:
    if intent == "trend":
        return "line"
    if intent in ("breakdown", "top_n"):
        return "bar"
    return "metric"  # single KPI value


def narrate(question: str, plan_obj: dict, df: pd.DataFrame) -> str:
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


def answer(question: str) -> dict:
    trace = []
    plan_obj = plan(question)
    trace.append({"agent": "planner", "output": plan_obj})

    raw_sql = build_sql(plan_obj)
    trace.append({"agent": "sql_builder", "output": raw_sql})

    guard = guard_sql(raw_sql)
    trace.append({"agent": "guardrail", "output": guard})
    if not guard["ok"]:
        return {"question": question, "error": guard["reason"], "trace": trace}

    con = duckdb.connect(str(resolve_path(get_config()["data"]["db_path"])), read_only=True)
    try:
        df = con.execute(guard["sql"]).fetchdf()
    finally:
        con.close()
    trace.append({"agent": "executor", "output": {"rows": len(df), "columns": list(df.columns)}})

    chart = recommend_chart(plan_obj["intent"], list(df.columns))
    narrative = narrate(question, plan_obj, df)
    trace.append({"agent": "chart", "output": chart})
    trace.append({"agent": "narrator", "output": narrative})

    return {
        "question": question,
        "sql": guard["sql"],
        "intent": plan_obj["intent"],
        "chart": chart,
        "narrative": narrative,
        "data": df.head(50).to_dict(orient="records"),
        "columns": list(df.columns),
        "trace": trace,
    }
