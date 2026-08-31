"""Remembered population figures, so verification does not re-scan per request.

Every reconciler needs facts about the *whole* warehouse - the revenue total,
how many regions exist, how many order rows have no price. Those answers do not
depend on the question being asked, and recomputing them for every request would
make the audit cost more than the answer it is checking.

``Baselines`` memoises them per warehouse *file version*: the cache key carries
the database's size and modification time, so rebuilding the warehouse retires
every figure derived from the old one instead of serving a stale total.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb

#: Identity of one warehouse file version: path, size, modification time.
Key = tuple[str, int, int]
#: One entry per warehouse version: ``fingerprint -> {sql: value}``. A new
#: fingerprint replaces the map wholesale - that is the invalidation.
_CACHE: dict[Key, dict[str, Any]] = {}
#: A single warehouse only ever needs a handful of baselines; the bound is
#: insurance against an unexpected caller, not a tuning knob.
MAX_ENTRIES = 64


def fingerprint(db_path: Path | str) -> Key:
    """Identify the exact bytes on disk, so a rebuilt warehouse gets fresh facts."""
    path = Path(db_path)
    try:
        stat = path.stat()
    except OSError:
        return (str(path.resolve()), -1, -1)
    return (str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def forget_all() -> None:
    """Drop every memoised figure (used by tests that rewrite a warehouse in place)."""
    _CACHE.clear()


class Baselines:
    """Warehouse-wide figures for one request, backed by a cross-request cache.

    The connection is opened on the first cache *miss*, not on construction:
    once the figures are memoised, an audit that needs none of them must not pay
    to open the database. On this warehouse that connect call, not the scans, is
    what the audit actually costs.
    """

    def __init__(self, connect: Callable[[], duckdb.DuckDBPyConnection], key: Key) -> None:
        self._connect = connect
        self._con: duckdb.DuckDBPyConnection | None = None
        self._store = _CACHE.setdefault(key, {})
        if len(_CACHE) > 1:
            # A new warehouse version supersedes every older one.
            for stale in [k for k in _CACHE if k != key]:
                del _CACHE[stale]
        self.queries = 0
        self.hits = 0
        self.connections = 0

    def scalar(self, sql: str) -> Any:
        """First column of the first row of ``sql``, computed at most once per version."""
        if sql in self._store:
            self.hits += 1
            return self._store[sql]
        row = self._open().execute(sql).fetchone()
        value = row[0] if row else None
        self.queries += 1
        if len(self._store) >= MAX_ENTRIES:
            self._store.pop(next(iter(self._store)))
        self._store[sql] = value
        return value

    def _open(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            self._con = self._connect()
            self.connections += 1
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None
