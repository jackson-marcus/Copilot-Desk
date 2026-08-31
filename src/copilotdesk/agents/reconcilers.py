"""The cross-checks that turn a plausible answer into a verified one.

Each one takes a different route back to the warehouse, because each failure
mode hides in a different place:

* ``population`` - the rows you can see are not the population you asked about.
* ``null_metric`` - ``SUM`` skips NULLs, ``COUNT(*)`` does not, so a ratio built
  from both drifts as soon as the fact table has a hole in it.
* ``join_integrity`` - an inner join to a dimension silently deletes facts whose
  key is missing from it.
* ``continuity`` - "trended up across 12 periods" means nothing if four of the
  calendar's periods never made it into the result.
"""

from __future__ import annotations

from copilotdesk.agents.base import (
    ADDITIVE_METRICS,
    OK,
    WARN,
    Finding,
    ReconcileContext,
    Reconciler,
)

#: Where each dimension's full member list lives.
MEMBER_SOURCE = {
    "region": "SELECT COUNT(DISTINCT region) FROM fact_orders",
    "category": "SELECT COUNT(DISTINCT category) FROM fact_orders",
    "segment": "SELECT COUNT(DISTINCT segment) FROM dim_customers",
}
POPULATION_SQL = {
    "revenue": "SELECT SUM(revenue) FROM fact_orders",
    "orders": "SELECT COUNT(*) FROM fact_orders",
}


class PopulationReconciler(Reconciler):
    """Do the shown rows account for the whole population, and if not, how much?"""

    name = "population"
    claim = "the rows shown add up to the warehouse-wide figure for this metric"
    intents = frozenset({"breakdown", "top_n"})

    def skip_reason(self, ctx: ReconcileContext) -> str | None:
        if ctx.metric not in ADDITIVE_METRICS:
            return f"{ctx.metric} is a ratio; group values do not add up to a population figure"
        if ctx.dimension is None:
            return "no dimension to reconcile against"
        return None

    def check(self, ctx: ReconcileContext) -> Finding:
        dim, metric = ctx.dimension, ctx.metric
        population = float(ctx.baselines.scalar(POPULATION_SQL[metric]))
        members = int(ctx.baselines.scalar(MEMBER_SOURCE[dim]))
        shown = float(ctx.frame[metric].sum())
        shown_members = int(ctx.frame[dim].nunique())
        # Per-group figures are rounded to cents before they reach us.
        tolerance = 0.01 * max(len(ctx.frame), 1)
        covered = shown / population if population else 0.0
        # Only a top-N plan is *supposed* to leave members out. A breakdown that
        # comes back short has lost them, and the totals are what say so - a
        # member can also be missing simply because it has no rows.
        truncated = ctx.plan["intent"] == "top_n" and shown_members < members
        evidence = {
            "population_total": round(population, 2),
            "shown_total": round(shown, 2),
            "members_total": members,
            "members_shown": shown_members,
            "covered_share": round(covered, 4),
            "truncated": truncated,
        }

        if shown > population + tolerance:
            return self._finding(
                WARN,
                f"shown rows total {shown:,.2f}, more than the whole warehouse "
                f"({population:,.2f}) - the query is double counting",
                **evidence,
            )
        if truncated:
            # The point of the check: the shown rows are NOT the denominator.
            return self._finding(
                OK,
                f"{shown_members} of {members} {dim}s shown, covering "
                f"{covered:.1%} of the {population:,.2f} population total",
                **evidence,
            )
        if shown < population - tolerance:
            missing = population - shown
            evidence["missing_total"] = round(missing, 2)
            return self._finding(
                WARN,
                f"the {shown_members} {dim}s shown total {shown:,.2f} against a population "
                f"of {population:,.2f} - {missing:,.2f} ({missing / population:.1%}) "
                "never reached the answer",
                **evidence,
            )
        return self._finding(
            OK,
            f"{shown_members} of {members} {dim}s have rows and they reconcile to the "
            f"population total {population:,.2f}",
            **evidence,
        )


class NullMetricReconciler(Reconciler):
    """``SUM`` ignores NULL revenue; ``COUNT(*)`` counts the row anyway."""

    name = "null_metric"
    claim = "no fact rows carry a NULL revenue that the metric silently swallows"

    def skip_reason(self, ctx: ReconcileContext) -> str | None:
        if ctx.metric == "orders":
            return "orders counts rows, so a NULL revenue does not change it"
        return None

    def check(self, ctx: ReconcileContext) -> Finding:
        rows = int(ctx.baselines.scalar("SELECT COUNT(*) FROM fact_orders"))
        nulls = int(ctx.baselines.scalar("SELECT COUNT(*) FROM fact_orders WHERE revenue IS NULL"))
        evidence = {"fact_rows": rows, "null_metric_rows": nulls}
        if not nulls:
            return self._finding(OK, f"all {rows:,} fact rows carry a revenue", **evidence)

        share = nulls / rows if rows else 0.0
        evidence["null_share"] = round(share, 4)
        if ctx.metric == "aov":
            total = float(ctx.baselines.scalar("SELECT SUM(revenue) FROM fact_orders") or 0.0)
            reported = total / rows if rows else 0.0
            corrected = total / (rows - nulls) if rows - nulls else 0.0
            evidence["reported_aov"] = round(reported, 2)
            evidence["aov_over_priced_rows"] = round(corrected, 2)
            return self._finding(
                WARN,
                f"{nulls:,} of {rows:,} rows ({share:.1%}) have no revenue; aov divides by "
                f"all of them ({reported:,.2f}) instead of only the priced ones "
                f"({corrected:,.2f})",
                **evidence,
            )
        return self._finding(
            WARN,
            f"{nulls:,} of {rows:,} rows ({share:.1%}) have no revenue and contribute "
            "nothing to the total",
            **evidence,
        )


class JoinIntegrityReconciler(Reconciler):
    """An inner join to a dimension deletes facts whose key is not in it."""

    name = "join_integrity"
    claim = "every fact row survives the join this answer depends on"

    def skip_reason(self, ctx: ReconcileContext) -> str | None:
        if ctx.dimension != "segment":
            return "this answer reads the fact table directly and joins nothing"
        return None

    def check(self, ctx: ReconcileContext) -> Finding:
        rows = int(ctx.baselines.scalar("SELECT COUNT(*) FROM fact_orders"))
        orphans = int(
            ctx.baselines.scalar(
                "SELECT COUNT(*) FROM fact_orders o LEFT JOIN dim_customers c "
                "ON o.customer_id = c.customer_id WHERE c.customer_id IS NULL"
            )
        )
        evidence = {"fact_rows": rows, "orphan_rows": orphans}
        if not orphans:
            return self._finding(
                OK, f"all {rows:,} fact rows match a customer in dim_customers", **evidence
            )
        share = orphans / rows if rows else 0.0
        evidence["orphan_share"] = round(share, 4)
        return self._finding(
            WARN,
            f"{orphans:,} of {rows:,} fact rows ({share:.1%}) reference a customer that is "
            "not in dim_customers; the join drops them from this answer",
            **evidence,
        )


class ContinuityReconciler(Reconciler):
    """A trend over a calendar with holes in it is not the trend it looks like."""

    name = "continuity"
    claim = "the series covers every calendar period between its endpoints"
    intents = frozenset({"trend"})

    def check(self, ctx: ReconcileContext) -> Finding:
        grain = "month" if ctx.plan.get("grain") == "month" else "day"
        expected = int(
            ctx.baselines.scalar(
                f"SELECT DATE_DIFF('{grain}', DATE_TRUNC('{grain}', MIN(order_date)), "
                f"DATE_TRUNC('{grain}', MAX(order_date))) + 1 FROM fact_orders"
            )
        )
        returned = len(ctx.frame)
        evidence = {"grain": grain, "periods_expected": expected, "periods_returned": returned}
        if returned >= expected:
            return self._finding(
                OK, f"all {expected} {grain}s in the range are present", **evidence
            )
        missing = expected - returned
        evidence["periods_missing"] = missing
        return self._finding(
            WARN,
            f"{missing} of {expected} {grain}s have no rows at all; the series jumps over them",
            **evidence,
        )


def build_reconcilers() -> list[Reconciler]:
    """Every check the auditor knows how to run, in reporting order."""
    return [
        PopulationReconciler(),
        NullMetricReconciler(),
        JoinIntegrityReconciler(),
        ContinuityReconciler(),
    ]


__all__ = [
    "ContinuityReconciler",
    "Finding",
    "JoinIntegrityReconciler",
    "NullMetricReconciler",
    "PopulationReconciler",
    "build_reconcilers",
]
