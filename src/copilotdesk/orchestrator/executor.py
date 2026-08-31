"""Run the selected cross-checks against the warehouse and fold them into a verdict.

One pass, one connection at most, no concurrency: every check here is a small
aggregate over the same DuckDB file, and once the baselines are cached most
audits never open the database at all. What the audit cost - connections,
scans, cache hits - is reported alongside the findings rather than guessed at.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from copilotdesk.agents.base import OK, WARN, Finding, ReconcileContext, Reconciler
from copilotdesk.agents.reconcilers import build_reconcilers
from copilotdesk.orchestrator.memory import Baselines, fingerprint
from copilotdesk.orchestrator.planner import AuditPlanner
from copilotdesk.settings import get_config, resolve_path

VERIFIED = "verified"
QUALIFIED = "qualified"
UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What the audit concluded about one answer."""

    verdict: str
    findings: tuple[Finding, ...]
    skipped: tuple[dict[str, str], ...]
    queries: int
    cache_hits: int
    connections: int

    @property
    def evidence(self) -> dict[str, dict[str, Any]]:
        """Verified figures the narrator is allowed to quote, keyed by check."""
        return {f.check: dict(f.evidence) for f in self.findings}

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.status == WARN)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "findings": [f.as_dict() for f in self.findings],
            "skipped": list(self.skipped),
            "queries": self.queries,
            "cache_hits": self.cache_hits,
            "connections": self.connections,
        }


class AuditExecutor:
    """Reconcile a finished answer against the warehouse it came from."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        reconcilers: Sequence[Reconciler] | None = None,
    ) -> None:
        self._db_path = db_path
        chosen = build_reconcilers() if reconcilers is None else reconcilers
        self.reconcilers = tuple(chosen)
        self.planner = AuditPlanner()

    @property
    def db_path(self) -> Path:
        if self._db_path is not None:
            return Path(self._db_path)
        return resolve_path(get_config()["data"]["db_path"])

    def run(self, plan: dict[str, Any], frame: pd.DataFrame) -> Reconciliation:
        path = self.db_path
        baselines = Baselines(lambda: duckdb.connect(str(path), read_only=True), fingerprint(path))
        try:
            ctx = ReconcileContext(plan=plan, frame=frame, baselines=baselines)
            audit = self.planner.plan(ctx, self.reconcilers)
            findings = [self._run_one(reconciler, ctx) for reconciler in audit.selected]
        finally:
            baselines.close()

        if any(f.status == WARN for f in findings):
            verdict = QUALIFIED
        elif findings:
            verdict = VERIFIED
        else:
            verdict = UNVERIFIED
        return Reconciliation(
            verdict=verdict,
            findings=tuple(findings),
            skipped=tuple(audit.skipped_as_dicts()),
            queries=baselines.queries,
            cache_hits=baselines.hits,
            connections=baselines.connections,
        )

    @staticmethod
    def _run_one(reconciler: Reconciler, ctx: ReconcileContext) -> Finding:
        before = ctx.baselines.queries
        try:
            finding = reconciler.check(ctx)
        except Exception as exc:  # a broken check must not pass silently as "ok"
            return Finding(
                check=reconciler.name,
                status=WARN,
                claim=reconciler.claim,
                detail=f"check could not run: {exc}",
                queries=ctx.baselines.queries - before,
            )
        queries = ctx.baselines.queries - before
        return Finding(
            check=finding.check,
            status=finding.status if finding.status in (OK, WARN) else WARN,
            claim=finding.claim,
            detail=finding.detail,
            evidence=finding.evidence,
            queries=queries,
        )
