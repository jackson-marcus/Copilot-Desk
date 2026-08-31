"""Choose which cross-checks can settle a given answer - and say why the rest cannot.

Not every check applies to every answer, and the reasons are domain reasons, not
plumbing: you cannot add group averages up, a query that joins nothing cannot
lose rows to a join, a KPI has no series to have gaps in. Recording the refusals
matters as much as running the checks: an answer that *nothing* could verify is
a different thing from an answer that passed everything, and the caller has to
be able to tell them apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from copilotdesk.agents.base import ReconcileContext, Reconciler


@dataclass(frozen=True, slots=True)
class AuditPlan:
    """The checks to run on one answer, plus the ones that declined and why."""

    selected: tuple[Reconciler, ...]
    skipped: tuple[tuple[str, str], ...]

    @property
    def verifiable(self) -> bool:
        return bool(self.selected)

    def skipped_as_dicts(self) -> list[dict[str, str]]:
        return [{"check": name, "reason": reason} for name, reason in self.skipped]


class AuditPlanner:
    """Route an answer to the checks that have something to say about it."""

    def plan(self, ctx: ReconcileContext, reconcilers: Sequence[Reconciler]) -> AuditPlan:
        intent = str(ctx.plan["intent"])
        selected: list[Reconciler] = []
        skipped: list[tuple[str, str]] = []
        for reconciler in reconcilers:
            if not reconciler.handles(intent):
                continue  # not a candidate at all - nothing to report
            reason = reconciler.skip_reason(ctx)
            if reason:
                skipped.append((reconciler.name, reason))
            else:
                selected.append(reconciler)
        return AuditPlan(tuple(selected), tuple(skipped))
