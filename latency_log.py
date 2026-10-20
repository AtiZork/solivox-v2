"""
Structured latency logging for the sniper buy and sell paths.

All durations use time.monotonic(). Lines go to the "solivox.latency" logger,
which has no DB handler (the module loggers commit every line to TradeLog,
which would itself add latency).

Two shapes:
  StageTimer  - one line per buy / sell attempt with every stage it went
                through, e.g.
                [latency] path=buy mint=Abc... outcome=sent lock_wait=0.1 price=210.4 ... total=1312.0
  StageStats  - rolling aggregates for high-volume work (one RPC per buy event
                on the stream), flushed as one summary line per interval.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from settings import LATENCY_SUMMARY_INTERVAL_SEC

logger = logging.getLogger("solivox.latency")


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


class StageTimer:
    """Collects per-stage durations (ms) for one buy or sell attempt."""

    def __init__(self, path: str, **fields) -> None:
        self.path = path
        self.fields = dict(fields)
        self.stages: dict[str, float] = {}
        self.outcome: Optional[str] = None
        self.reason: Optional[str] = None
        self._start = time.monotonic()

    def add(self, stage: str, ms: float) -> None:
        self.stages[stage] = self.stages.get(stage, 0.0) + ms

    @contextmanager
    def stage(self, name: str):
        start = time.monotonic()
        try:
            yield
        finally:
            self.add(name, (time.monotonic() - start) * 1000)

    def line(self) -> str:
        parts = [f"path={self.path}"]
        parts += [f"{k}={_fmt(v)}" for k, v in self.fields.items() if v is not None]
        parts.append(f"outcome={self.outcome or 'none'}")
        if self.reason:
            parts.append(f"reason={self.reason}")
        parts += [f"{k}={_fmt(v)}" for k, v in self.stages.items()]
        parts.append(f"total={_fmt((time.monotonic() - self._start) * 1000)}")
        return "[latency] " + " ".join(parts)

    def finish(self, outcome: Optional[str] = None, reason: Optional[str] = None) -> None:
        if outcome is not None:
            self.outcome = outcome
        if reason is not None:
            self.reason = reason
        logger.info(self.line())
        # Feeds the throttled aggregators below (dashboard 3.3) in addition to
        # the log line above, not instead of it - every buy/sell attempt
        # already builds this breakdown, so this is just also recording it
        # somewhere queryable instead of only a text log.
        stats = _PATH_STATS.get(self.path)
        if stats is not None:
            for stage, ms in self.stages.items():
                stats.record(stage, ms)
            # "total" is computed for the log line above but never added to
            # self.stages, so it has to be recorded separately here - the
            # dashboard's "current buy/sell total avg" figure (3.5) reads
            # this exact stage name back out.
            stats.record("total", (time.monotonic() - self._start) * 1000)


class StageStats:
    """Thread-safe count / mean / max per stage, logged every interval."""

    def __init__(self, name: str, interval_sec: float = LATENCY_SUMMARY_INTERVAL_SEC) -> None:
        self.name = name
        self.interval_sec = interval_sec
        self._lock = threading.Lock()
        self._durations: dict[str, list] = {}  # stage -> [count, total_ms, max_ms]
        self._counts: dict[str, int] = {}
        self._last_flush = time.monotonic()
        self._last_flush_wall = datetime.utcnow()

    def record(self, stage: str, ms: float) -> None:
        with self._lock:
            entry = self._durations.setdefault(stage, [0, 0.0, 0.0])
            entry[0] += 1
            entry[1] += ms
            entry[2] = max(entry[2], ms)
        self.maybe_flush()

    def count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + n
        self.maybe_flush()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "durations": {k: list(v) for k, v in self._durations.items()},
                "counts": dict(self._counts),
            }

    def flush_and_return(self, force: bool = False) -> Optional[dict]:
        """Same threshold/reset logic maybe_flush always had, but returns the
        flushed snapshot (with wall-clock window bounds) instead of only
        logging it - lets a caller persist it (dashboard 3.3) without this
        module itself depending on a DB. maybe_flush below is now just the
        logging caller of this."""
        if self.interval_sec <= 0 and not force:
            return None
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_flush < self.interval_sec:
                return None
            durations, counts = self._durations, self._counts
            self._durations, self._counts = {}, {}
            window_start, window_end = self._last_flush_wall, datetime.utcnow()
            window_sec = now - self._last_flush
            self._last_flush = now
            self._last_flush_wall = window_end
        if not durations and not counts:
            return None
        return {
            "window_sec": window_sec,
            "window_start": window_start,
            "window_end": window_end,
            "durations": durations,
            "counts": counts,
        }

    def maybe_flush(self, force: bool = False) -> None:
        result = self.flush_and_return(force=force)
        if result is None:
            return
        parts = [f"[latency-summary] {self.name} window={result['window_sec']:.0f}s"]
        for stage, (n, total, peak) in sorted(result["durations"].items()):
            parts.append(f"{stage}(n={n} avg={total / n:.1f} max={peak:.1f})")
        for key, n in sorted(result["counts"].items()):
            parts.append(f"{key}={n}")
        logger.info(" ".join(parts))


# Fed by every buy/sell StageTimer.finish() (dashboard 3.3) - throttled,
# in-memory aggregation reused as-is from the sniper WS stream's own
# rpc_stats, rather than writing one DB row per attempt (busy sniper periods
# evaluate many tokens/sec, which a per-event write would not handle well).
buy_stats = StageStats("buy")
sell_stats = StageStats("sell")
_PATH_STATS = {"buy": buy_stats, "sell": sell_stats}
