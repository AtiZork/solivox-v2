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


class StageStats:
    """Thread-safe count / mean / max per stage, logged every interval."""

    def __init__(self, name: str, interval_sec: float = LATENCY_SUMMARY_INTERVAL_SEC) -> None:
        self.name = name
        self.interval_sec = interval_sec
        self._lock = threading.Lock()
        self._durations: dict[str, list] = {}  # stage -> [count, total_ms, max_ms]
        self._counts: dict[str, int] = {}
        self._last_flush = time.monotonic()

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

    def maybe_flush(self, force: bool = False) -> None:
        if self.interval_sec <= 0 and not force:
            return
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_flush < self.interval_sec:
                return
            durations, counts = self._durations, self._counts
            self._durations, self._counts = {}, {}
            window = now - self._last_flush
            self._last_flush = now
        if not durations and not counts:
            return
        parts = [f"[latency-summary] {self.name} window={window:.0f}s"]
        for stage, (n, total, peak) in sorted(durations.items()):
            parts.append(f"{stage}(n={n} avg={total / n:.1f} max={peak:.1f})")
        for key, n in sorted(counts.items()):
            parts.append(f"{key}={n}")
        logger.info(" ".join(parts))
