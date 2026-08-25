"""The immutable unit of work that flows down the pipe.

Everything a filter knows about an in-flight question lives in one frozen
``Envelope``. Filters never mutate it; they return a *new* envelope derived from
the old one. Two properties fall out of that for free:

* the ``trace`` is a structural consequence of the run, not manual bookkeeping -
  it can only grow, one entry per filter, in execution order;
* failure is a value, not an exception. A halted envelope (``error`` set) is
  waved through untouched by every downstream filter, so a guardrail rejection
  still arrives at the caller carrying the full trace of what happened first.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """One filter's contribution to the audit trail."""

    name: str
    output: Any
    duration_ms: float

    def as_dict(self) -> dict[str, Any]:
        # ``agent`` (not ``name``) is the published wire key: the /ask trace is
        # part of the public contract and the Streamlit UI reads it.
        return {"agent": self.name, "output": self.output, "duration_ms": self.duration_ms}


@dataclass(frozen=True, slots=True)
class Envelope:
    """An immutable question-in-flight: inputs, accumulated payload, trace, error."""

    question: str
    payload: Mapping[str, Any] = _EMPTY
    trace: tuple[TraceEntry, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        # Defend the payload against accidental in-place edits by a filter.
        if not isinstance(self.payload, MappingProxyType):
            object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @property
    def halted(self) -> bool:
        return self.error is not None

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def require(self, key: str) -> Any:
        """Read a payload key an upstream filter is contracted to have set."""
        try:
            return self.payload[key]
        except KeyError as exc:  # pragma: no cover - signals a mis-composed pipeline
            raise KeyError(f"envelope is missing required payload key {key!r}") from exc

    def with_payload(self, **updates: Any) -> Envelope:
        """Return a new envelope whose payload is this one merged with ``updates``."""
        if not updates:
            return self
        return replace(self, payload={**self.payload, **updates})

    def with_trace_entry(self, entry: TraceEntry) -> Envelope:
        """Return a new envelope with ``entry`` appended to the trace."""
        return replace(self, trace=(*self.trace, entry))

    def halted_with(self, reason: str) -> Envelope:
        """Return a new, halted envelope. Downstream filters will pass it through."""
        return replace(self, error=reason)


@dataclass(frozen=True, slots=True)
class Emission:
    """What a filter hands back to the machinery: payload delta + trace output.

    Setting ``error`` halts the pipe *after* the filter's own trace entry is
    recorded, so the rejection is always visible in the audit trail.
    """

    trace: Any
    payload: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
