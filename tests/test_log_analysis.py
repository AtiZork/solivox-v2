"""
Tests for the read-only Log Analysis feature (log_analysis.py).

Every synthetic log line below is copied from this codebase's actual
logger.error/logger.warning/print() call sites (autosnipe_buy_new_token.py,
autosnipe_sell_script.py, sniper_stream.py, yellowstone_cache.py,
latency_log.py) with just the interpolated values changed - not invented
text - so a pattern match here is a real guarantee it'll match production
output in the same shape.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from log_analysis import (
    analyze_log_text,
    fetch_journal_logs,
    log_analysis_bp,
    run_log_analysis,
)
from models import db, User


# --- analyze_log_text: pure function, one real log line per category ---

def test_insufficient_balance_is_categorized_as_info():
    line = (
        "Oct 23 16:48:15 ahmad-colo python[1]: 2026-10-23 16:48:15,945 - ERROR - "
        "Insufficient balance. Wallet has 0.0874 SOL, but 0.1030 SOL is required "
        "(0.1000 buy + 0.0030 reserve)."
    )
    result = analyze_log_text(line)
    assert result["lines_analyzed"] == 1
    assert len(result["findings"]) == 1
    f = result["findings"][0]
    assert f["category"] == "Buy skipped - insufficient balance"
    assert f["severity"] == "info"
    assert f["count"] == 1


def test_buy_conditions_not_met_ws_and_http_both_categorized():
    text = "\n".join([
        "[Buy Condition][WS] Buy conditions NOT MET for AbcMintpump within scan window. "
        "(candidate txns seen=3, excluded=1 [sell/failed/unresolved])",
        "[Buy Condition][HTTP] Buy conditions NOT MET for XyzMintpump within scan window.",
    ])
    result = analyze_log_text(text)
    f = next(x for x in result["findings"] if x["category"] == "Tokens skipped - buy conditions not met")
    assert f["count"] == 2
    assert f["severity"] == "info"


def test_detector_and_tx_fetcher_errors():
    text = "\n".join([
        "[Detector] Error processing transaction 5xyz...: KeyError('meta')",
        "    [Tx Fetcher] Error parsing transaction 5abc...: ValueError",
        "[Tx Fetcher] Error fetching signatures for MintAddr: RPCException",
    ])
    result = analyze_log_text(text)
    f = next(x for x in result["findings"] if x["category"] == "Detector errors")
    assert f["count"] == 3
    assert f["severity"] == "warning"


def test_sell_failures_matched_before_buy_failures_despite_substring_overlap():
    """Regression guard: 'Error performing swap for X' must land in Sell
    failures, never double-counted or mis-bucketed into Buy failures, even
    though 'Error performing swap' is a literal substring of both."""
    text = "\n".join([
        "2026-10-23 - ERROR - Error performing swap for trade#42: swap_error details",
        "2026-10-23 - ERROR - Error fetching quote for trade#43: timeout",
        "2026-10-23 - ERROR - Error sending transaction for trade#44: RPC down",
        "2026-10-23 - ERROR - Auto-snipe sell error for trade#45: unexpected",
        "2026-10-23 - WARNING - Rate limited performing swap for trade#46: 429",
        "Unexpected error sending a sell in this cycle: boom",
        "Unexpected error in auto_trade loop: boom2",
        "2026-10-23 - ERROR - Sale sent for trade#47 (tx abc123) but DB update failed: IntegrityError",
    ])
    result = analyze_log_text(text)
    sell = next(x for x in result["findings"] if x["category"] == "Sell failures")
    assert sell["count"] == 8
    assert sell["severity"] == "error"
    assert not any(x["category"] == "Buy failures" for x in result["findings"])


def test_buy_failures_shorter_strings_still_match_on_their_own():
    text = "\n".join([
        "2026-10-23 - ERROR - Error performing swap)",
        "2026-10-23 - ERROR - Error performing swap (non-JSON response)",
        "2026-10-23 - ERROR - Error fetching quote",
        "2026-10-23 - ERROR - Error fetching quote (non-JSON response)",
        "2026-10-23 - ERROR - Buy transaction for AbcMintpump rejected: slippage exceeded",
        "2026-10-23 - ERROR - Failed to fetch wallet for user.",
        "2026-10-23 - ERROR - Failed to fetch private key",
    ])
    result = analyze_log_text(text)
    buy = next(x for x in result["findings"] if x["category"] == "Buy failures")
    assert buy["count"] == 7
    assert buy["severity"] == "error"


def test_pricing_and_yellowstone_connection_findings():
    text = "\n".join([
        "Yellowstone price fetch failed for trade 42: TimeoutError",
        "Failed to fetch price for trade 43: ConnectionError",
        "[YellowstoneCache] stream error: grpc.RpcError - reconnecting in 3.0s",
    ])
    result = analyze_log_text(text)
    pricing = next(x for x in result["findings"] if x["category"] == "Pricing failures")
    assert pricing["count"] == 2
    yc = next(x for x in result["findings"] if x["category"] == "Yellowstone connection issues")
    assert yc["count"] == 1


def test_abnormal_latency_flags_only_slow_attempts_and_cites_them():
    text = "\n".join([
        "[latency] path=buy mint=Fast1pump outcome=sent lock_wait=0.1 total=450.0",
        "[latency] path=buy mint=Slow1pump outcome=sent decision=200.0 quote=1800.0 swap=900.0 total=3200.0",
        "[latency] path=sell mint=Slow2pump outcome=sent total=5000.0",
    ])
    result = analyze_log_text(text, latency_warn_ms=3000.0)
    f = next(x for x in result["findings"] if x["category"] == "Abnormal latency")
    assert f["count"] == 2
    assert all("Slow" in line for line in f["evidence"])
    assert not any("Fast1pump" in line for line in f["evidence"])


def test_unmatched_error_line_falls_into_other_errors_catch_all():
    text = "2026-10-23 10:00:00,000 - ERROR - Something totally unexpected happened"
    result = analyze_log_text(text)
    f = next(x for x in result["findings"] if x["category"] == "Other errors")
    assert f["count"] == 1


def test_clean_logs_produce_no_findings():
    text = "\n".join([
        "[WS] Sniper WebSocket connected to wss://example",
        "[Buy Condition][WS] qualifying=3 / required=10 (total buys=3, range=$80-$150)",
    ])
    result = analyze_log_text(text)
    assert result["findings"] == []
    assert result["lines_analyzed"] == 2


def test_empty_text_handled_cleanly():
    result = analyze_log_text("")
    assert result == {"lines_analyzed": 0, "findings": []}


def test_findings_sorted_error_before_warning_before_info():
    text = "\n".join([
        "Buy conditions NOT MET for Mint1",                               # info
        "[YellowstoneCache] stream error: x - reconnecting in 3.0s",      # warning
        "Error performing swap)",                                         # error (buy)
    ])
    result = analyze_log_text(text)
    severities = [f["severity"] for f in result["findings"]]
    assert severities == ["error", "warning", "info"]


# --- fetch_journal_logs: subprocess handling, mocked (no real journalctl needed) ---

def test_fetch_journal_logs_missing_binary_returns_clean_error():
    with patch("log_analysis.subprocess.run", side_effect=FileNotFoundError):
        text, error = fetch_journal_logs(hours=24)
    assert text is None
    assert "journalctl is not available" in error


def test_fetch_journal_logs_timeout_returns_clean_error():
    with patch("log_analysis.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="journalctl", timeout=20)):
        text, error = fetch_journal_logs(hours=24)
    assert text is None
    assert "did not return within" in error


def test_fetch_journal_logs_permission_denied_returns_clean_error():
    mock_result = MagicMock(returncode=1, stdout="", stderr="Failed to read: Permission denied")
    with patch("log_analysis.subprocess.run", return_value=mock_result):
        text, error = fetch_journal_logs(hours=24)
    assert text is None
    assert "permission denied" in error.lower()
    assert "systemd-journal" in error


def test_fetch_journal_logs_success_returns_stdout():
    mock_result = MagicMock(returncode=0, stdout="log line 1\nlog line 2\n", stderr="")
    with patch("log_analysis.subprocess.run", return_value=mock_result) as mock_run:
        text, error = fetch_journal_logs(hours=24)
    assert error is None
    assert text == "log line 1\nlog line 2\n"
    args = mock_run.call_args[0][0]
    assert args == ["journalctl", "-u", "solivox", "--since", "24 hours ago", "--no-pager"]


def test_fetch_journal_logs_clamps_hours_into_safe_bounds():
    mock_result = MagicMock(returncode=0, stdout="", stderr="")
    with patch("log_analysis.subprocess.run", return_value=mock_result) as mock_run:
        fetch_journal_logs(hours=9999)
    args = mock_run.call_args[0][0]
    assert args[args.index("--since") + 1] == "72 hours ago"  # clamped to MAX_HOURS


def test_run_log_analysis_surfaces_source_error_without_crashing():
    with patch("log_analysis.subprocess.run", side_effect=FileNotFoundError):
        result = run_log_analysis(hours=24)
    assert result["source_error"] is not None
    assert result["findings"] == []
    assert result["lines_analyzed"] == 0


# --- /api/logs/analyze: real Flask test client, JWT-gated like every other API route ---

@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["JWT_SECRET_KEY"] = "test-secret-key-at-least-32-bytes-long"
    db.init_app(app)
    JWTManager(app)
    app.register_blueprint(log_analysis_bp)
    with app.app_context():
        db.create_all()
        user = User(username="log_tester", email="lt@test.local")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
        token = create_access_token(identity=str(user.id))
    app.test_token = token
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def test_endpoint_requires_auth(client):
    resp = client.get("/api/logs/analyze")
    assert resp.status_code == 401


def test_endpoint_returns_analysis_with_valid_token(client, app):
    mock_result = {"hours": 24, "source_error": None, "lines_analyzed": 2, "findings": [
        {"category": "Buy failures", "severity": "error", "count": 1, "evidence": ["..."], "likely_cause": "..."}
    ]}
    with patch("log_analysis.run_log_analysis", return_value=mock_result):
        resp = client.get(
            "/api/logs/analyze?hours=24",
            headers={"Authorization": f"Bearer {app.test_token}"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["lines_analyzed"] == 2
    assert body["findings"][0]["category"] == "Buy failures"


def test_endpoint_defaults_hours_on_bad_input(client, app):
    with patch("log_analysis.run_log_analysis") as mock_run:
        mock_run.return_value = {"hours": 24, "source_error": None, "lines_analyzed": 0, "findings": []}
        client.get(
            "/api/logs/analyze?hours=not-a-number",
            headers={"Authorization": f"Bearer {app.test_token}"},
        )
    mock_run.assert_called_once_with(hours=24)
