"""Planner agent: route a natural-language question to an intent + dimensions.

Deterministic keyword/heuristic routing (no LLM needed for the demo). The four
intents map to different SQL shapes; getting the routing right is the first
measurable step of the pipeline.
"""

from __future__ import annotations

import re

DIMENSIONS = {
    "region": ["region", "regions"],
    "category": ["category", "categories"],
    "segment": ["segment", "segments"],
}
METRICS = {
    "revenue": ["revenue", "sales", "income"],
    "orders": ["orders", "order count", "how many orders"],
    "aov": ["average order value", "aov", "average order"],
}
TREND_WORDS = ["trend", "over time", "monthly", "daily", "by month", "by day", "over the year"]
TOPN_RE = re.compile(r"top\s+(\d+)")


def plan(question: str) -> dict:
    q = question.lower()
    metric = next((m for m, kws in METRICS.items() if any(k in q for k in kws)), "revenue")
    dimension = next((d for d, kws in DIMENSIONS.items() if any(k in q for k in kws)), None)

    top_match = TOPN_RE.search(q)
    if top_match or (("top" in q or "most" in q) and dimension):
        intent = "top_n"
        limit = int(top_match.group(1)) if top_match else 5
    elif any(w in q for w in TREND_WORDS):
        intent = "trend"
        limit = None
    elif dimension:
        intent = "breakdown"
        limit = None
    else:
        intent = "kpi"
        limit = None

    grain = "month" if ("month" in q or "trend" in q or "over time" in q) else "day"
    return {
        "intent": intent,
        "metric": metric,
        "dimension": dimension,
        "top_n": limit,
        "grain": grain,
    }
