"""
Persistence for the buy/sell latency numbers latency_log.py already computes
but only ever logs (action-plan 3.3). Kept out of models.py, which is still
being actively changed elsewhere in this project.
"""

import atexit
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from models import db
from latency_log import buy_stats, sell_stats
from settings import LATENCY_SUMMARY_INTERVAL_SEC

logger = logging.getLogger(__name__)

_scheduler = None


class LatencySnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    path = db.Column(db.String(10), nullable=False)  # "buy" or "sell"
    stage = db.Column(db.String(40), nullable=False)  # e.g. "quote", "swap", "send", "total"
    window_start = db.Column(db.DateTime, nullable=False)
    window_end = db.Column(db.DateTime, nullable=False)
    count = db.Column(db.Integer, nullable=False)
    avg_ms = db.Column(db.Float, nullable=False)
    max_ms = db.Column(db.Float, nullable=False)


def _flush_one(app, path: str, stats) -> None:
    result = stats.flush_and_return(force=True)
    if result is None:
        return
    with app.app_context():
        try:
            for stage, (n, total, peak) in result["durations"].items():
                db.session.add(LatencySnapshot(
                    path=path,
                    stage=stage,
                    window_start=result["window_start"],
                    window_end=result["window_end"],
                    count=n,
                    avg_ms=total / n,
                    max_ms=peak,
                ))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.warning(f"[LatencySnapshot] failed to persist {path} snapshot: {exc}")


def flush_latency_snapshots(app) -> None:
    """Writes whatever buy_stats/sell_stats have accumulated since the last
    flush into LatencySnapshot - called on its own schedule, forced (not
    gated by StageStats' own interval, since this job's own interval is
    already the throttle)."""
    _flush_one(app, "buy", buy_stats)
    _flush_one(app, "sell", sell_stats)


def start_latency_snapshot_scheduler(app) -> None:
    """Starts the periodic job persisting latency_log.py's buy/sell
    aggregates. Mirrors init_scheduler.create_scheduler's own
    BackgroundScheduler + atexit pattern."""
    global _scheduler
    if _scheduler and _scheduler.running:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        lambda: flush_latency_snapshots(app),
        trigger="interval",
        seconds=LATENCY_SUMMARY_INTERVAL_SEC,
        id="latency_snapshot_flush",
        replace_existing=True,
    )
    _scheduler.start()
    atexit.register(lambda: _scheduler.shutdown(wait=False))
    print(f"[Dashboard] Latency snapshot scheduler running every {LATENCY_SUMMARY_INTERVAL_SEC}s.")
