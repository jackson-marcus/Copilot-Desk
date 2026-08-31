"""What a reconciler is.

The analyst pipe proves its SQL is *safe* (single read-only SELECT) and writes a
takeaway from the rows that came back. Neither of those makes the takeaway
*true*: the rows are a sample of the warehouse - truncated by a ``LIMIT``,
narrowed by a join, or thinned by NULLs - and a claim computed over them can be
confidently wrong about the population they came from.

A reconciler settles one such claim by going back to the warehouse for the
figure the answer implies, and reports what it found. It never edits the
answer; it produces evidence the narrator is allowed to quote and a finding the
caller can read.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import pandas as pd

#: Metrics whose per-group values sum to the population figure. ``aov`` is a
#: ratio: the average of averages is not the average, so any check that adds
#: group values up must refuse to run on it rather than raise a false alarm.
ADDITIVE_METRICS = frozenset({"revenue", "orders"})

OK = "ok"
WARN = "warn"
SKIP = "skip"


@runtime_checkable
class BaselineSource(Protocol):
    """Read-only access to population figures, cached across requests."""

    def scalar(self, sql: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class ReconcileContext:
    """Everything a reconciler may look at: the plan, the rows, the warehouse."""

    plan: Mapping[str, Any]
    frame: pd.DataFrame
    baselines: BaselineSource

    @property
    def metric(self) -> str:
        return str(self.plan["metric"])

    @property
    def dimension(self) -> str | None:
        dim = self.plan.get("dimension")
        return str(dim) if dim else None


@dataclass(frozen=True, slots=True)
class Finding:
    """One reconciler's verdict on one claim, with the numbers behind it."""

    check: str
    status: str
    claim: str
    detail: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    queries: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "status": self.status,
            "claim": self.claim,
            "detail": self.detail,
            "evidence": dict(self.evidence),
            "queries": self.queries,
        }


class Reconciler(ABC):
    """A single, independently runnable cross-check on a finished answer."""

    name: str = "reconciler"
    #: Plain-English statement of what this check settles; published in the trace.
    claim: str = ""
    #: Intents this check has anything to say about. Empty means "all of them".
    intents: frozenset[str] = frozenset()

    def handles(self, intent: str) -> bool:
        return not self.intents or intent in self.intents

    def skip_reason(self, ctx: ReconcileContext) -> str | None:
        """Why this check cannot settle *this* answer, or ``None`` if it can.

        Saying why a check was skipped is part of the output: an answer nothing
        could check is not the same as an answer that passed.
        """
        return None

    @abstractmethod
    def check(self, ctx: ReconcileContext) -> Finding:
        """Compare the answer against the warehouse and report."""

    def _finding(self, status: str, detail: str, **evidence: Any) -> Finding:
        return Finding(
            check=self.name, status=status, claim=self.claim, detail=detail, evidence=evidence
        )
