from __future__ import annotations

import time
from unittest.mock import patch

from latency_log import StageStats, StageTimer


def test_stage_timer_line_format():
    with patch("latency_log.logger") as log:
        timer = StageTimer("buy", mint="MintX", user=None)
        timer.add("lock_wait", 1.25)
        with timer.stage("quote"):
            time.sleep(0.02)
        timer.finish("sent", reason="ok_reason")
    line = log.info.call_args.args[0]
    assert line.startswith("[latency] path=buy mint=MintX outcome=sent reason=ok_reason lock_wait=1.2 quote=")
    assert "user=" not in line  # None fields are left out
    assert float(line.split("quote=")[1].split()[0]) >= 15


def test_stage_timer_records_stage_even_when_it_raises():
    timer = StageTimer("sell")
    try:
        with timer.stage("send"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert "send" in timer.stages


def test_stage_stats_summary_and_reset():
    stats = StageStats("sniper", interval_sec=3600)
    stats.record("get_transaction", 10.0)
    stats.record("get_transaction", 30.0)
    stats.count("err_other")
    with patch("latency_log.logger") as log:
        stats.maybe_flush()  # interval not reached
        log.info.assert_not_called()
        stats.maybe_flush(force=True)
    line = log.info.call_args.args[0]
    assert line.startswith("[latency-summary] sniper window=")
    assert "get_transaction(n=2 avg=20.0 max=30.0)" in line
    assert "err_other=1" in line
    assert stats.snapshot() == {"durations": {}, "counts": {}}


def test_stage_stats_disabled_interval_never_auto_flushes():
    stats = StageStats("sniper", interval_sec=0)
    with patch("latency_log.logger") as log:
        for _ in range(5):
            stats.record("x", 1.0)
    log.info.assert_not_called()
    assert stats.snapshot()["durations"]["x"][0] == 5
