"""Composition: glue a sequence of filters into one callable pipe.

The runner is deliberately tiny. It owns no analytics knowledge - it only knows
that filters are applied left to right and that each one returns the envelope
the next one receives. All the interesting behaviour (halting, tracing, timing)
lives in the envelope and the filters, so adding, removing or reordering a stage
is a change to this one tuple.
"""

from __future__ import annotations

from dataclasses import dataclass

from copilotdesk.pipeline.envelope import Envelope
from copilotdesk.pipeline.filters import Filter


@dataclass(frozen=True, slots=True)
class Pipeline:
    """An immutable left-to-right composition of filters."""

    filters: tuple[Filter, ...]

    def __call__(self, question: str) -> Envelope:
        return self.run(Envelope(question=question))

    def run(self, envelope: Envelope) -> Envelope:
        for filt in self.filters:
            envelope = filt.apply(envelope)
        return envelope

    @property
    def stage_names(self) -> tuple[str, ...]:
        return tuple(filt.name for filt in self.filters)

    def then(self, *extra: Filter) -> Pipeline:
        """Return a new pipeline with more filters appended."""
        return Pipeline((*self.filters, *extra))


def compose(*filters: Filter) -> Pipeline:
    """Build a pipeline from filters given in execution order."""
    return Pipeline(tuple(filters))
