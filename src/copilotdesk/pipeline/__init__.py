"""Pipes and filters over an immutable typed envelope.

Composition root for the analyst pipe plus the projection that turns a finished
envelope into the ``/ask`` response body.
"""

from __future__ import annotations

from typing import Any

from copilotdesk.pipeline.envelope import Emission, Envelope, TraceEntry
from copilotdesk.pipeline.filters import (
    BaseFilter,
    ChartFilter,
    ExecutorFilter,
    Filter,
    GuardrailFilter,
    NarratorFilter,
    PlannerFilter,
    SqlBuilderFilter,
)
from copilotdesk.pipeline.runner import Pipeline, compose

#: Payload keys the HTTP contract publishes. ``plan`` and ``frame`` stay internal.
ANSWER_KEYS = ("sql", "intent", "chart", "narrative", "data", "columns")


def build_analyst_pipeline() -> Pipeline:
    """The analyst topology, in execution order."""
    return compose(
        PlannerFilter(),
        SqlBuilderFilter(),
        GuardrailFilter(),
        ExecutorFilter(),
        ChartFilter(),
        NarratorFilter(),
    )


def as_answer(envelope: Envelope) -> dict[str, Any]:
    """Project a finished envelope onto the ``/ask`` response body."""
    trace = [entry.as_dict() for entry in envelope.trace]
    if envelope.halted:
        return {"question": envelope.question, "error": envelope.error, "trace": trace}
    return {
        "question": envelope.question,
        **{key: envelope.require(key) for key in ANSWER_KEYS},
        "trace": trace,
    }


__all__ = [
    "ANSWER_KEYS",
    "BaseFilter",
    "ChartFilter",
    "Emission",
    "Envelope",
    "ExecutorFilter",
    "Filter",
    "GuardrailFilter",
    "NarratorFilter",
    "Pipeline",
    "PlannerFilter",
    "SqlBuilderFilter",
    "TraceEntry",
    "as_answer",
    "build_analyst_pipeline",
    "compose",
]
