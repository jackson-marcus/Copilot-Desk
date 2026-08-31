"""Report what reconciliation catches on the labeled question set, and what it costs.

Every number quoted in the README about the audit stage comes from here:

    uv run python scripts/audit_answers.py

For each question it prints the verdict, the checks that ran, and - where the
answer was truncated by a LIMIT - the share the narrator quotes now against the
share it would quote if it used the visible rows as its denominator, which is
what the pipeline did before the reconciler existed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from copilotdesk.orchestrator.memory import forget_all
from copilotdesk.pipeline import build_analyst_pipeline
from copilotdesk.settings import get_config, resolve_path


def run_set(analyst, questions: list[dict]) -> list[dict]:
    rows = []
    for item in questions:
        env = analyst(item["q"])
        stage = {entry.name: entry for entry in env.trace}
        audit = stage["reconciler"].output if "reconciler" in stage else {}
        row = {
            "question": item["q"],
            "intent": env.get("intent"),
            "verdict": env.get("verdict", "halted"),
            "checks": [c["check"] for c in env.get("checks", [])],
            "warnings": [c["check"] for c in env.get("checks", []) if c["status"] == "warn"],
            "skipped": [s["check"] for s in env.get("unchecked", [])],
            "queries": audit.get("queries", 0),
            "cache_hits": audit.get("cache_hits", 0),
            "connections": audit.get("connections", 0),
            "audit_ms": stage["reconciler"].duration_ms if "reconciler" in stage else 0.0,
            "total_ms": round(sum(e.duration_ms for e in env.trace), 3),
        }
        population = env.get("evidence", {}).get("population")
        if population and population["truncated"]:
            leader = env.get("data")[0][env.get("plan")["metric"]]
            row["share_quoted_pct"] = round(leader / population["population_total"] * 100, 1)
            row["share_from_visible_rows_pct"] = round(leader / population["shown_total"] * 100, 1)
            row["covered_share_pct"] = round(population["covered_share"] * 100, 1)
        rows.append(row)
    return rows


def main() -> None:
    cfg = get_config()
    questions = json.loads(resolve_path(cfg["agent"]["eval_path"]).read_text(encoding="utf-8"))
    analyst = build_analyst_pipeline()

    forget_all()
    cold = run_set(analyst, questions)
    warm = run_set(analyst, questions)

    print(f"{'question':38} {'intent':10} {'verdict':11} {'q':>2} {'ms':>6}  checks")
    for row in cold:
        print(
            f"{row['question'][:37]:38} {row['intent'] or '-':10} {row['verdict']:11} "
            f"{row['queries']:>2} {row['audit_ms']:>6.1f}  {','.join(row['checks']) or '-'}"
        )

    verdicts = {
        name: sum(1 for r in cold if r["verdict"] == name)
        for name in ("verified", "qualified", "unverified")
    }
    truncated = [r for r in cold if "share_quoted_pct" in r]
    summary = {
        "n_questions": len(cold),
        "verdicts": verdicts,
        "answers_with_a_truncated_denominator": len(truncated),
        "warehouse_queries_cold_total": sum(r["queries"] for r in cold),
        "connections_cold_total": sum(r["connections"] for r in cold),
        "connections_warm_total": sum(r["connections"] for r in warm),
        "warehouse_queries_warm_total": sum(r["queries"] for r in warm),
        "audit_ms_median_cold": round(median(r["audit_ms"] for r in cold), 2),
        "audit_ms_median_warm": round(median(r["audit_ms"] for r in warm), 2),
        "audit_share_of_answer_median_warm": round(
            median(r["audit_ms"] / r["total_ms"] for r in warm if r["total_ms"]), 4
        ),
    }
    for row in truncated:
        summary.setdefault("truncated_answers", []).append(
            {
                "question": row["question"],
                "share_quoted_pct": row["share_quoted_pct"],
                "share_from_visible_rows_pct": row["share_from_visible_rows_pct"],
                "overstatement_pp": round(
                    row["share_from_visible_rows_pct"] - row["share_quoted_pct"], 1
                ),
                "covered_share_pct": row["covered_share_pct"],
            }
        )
    print()
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
