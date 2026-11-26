"""
Read-only log analysis for the Solivox dashboard's "Log Analysis" page.

Why this reads journalctl and not TradeLog (models.py): TradeLog only
receives logger.* calls from autosnipe.py, autosnipe_sell_script.py,
autosnipe_buy_new_token.py, long_sell_trade.py and menual_sell_token.py
(each attaches its own DBLogHandler to its own module logger). It never
receives sniper_stream.py's detection logs, the print()-based
"[Buy Condition]" lines in the buy flow, or latency_log.py's structured
timing lines - that module's own docstring says it deliberately has no DB
handler, to avoid adding latency to the hot path. journalctl -u solivox is
this project's own established primary diagnostic tool (see CLAUDE.md,
solivox-investigate) and is the only source that actually has all of it.

This module is pure, read-only, and never touches trading logic or
production config - it only shells out to `journalctl` (no write flags) and
pattern-matches the text it returns.
"""

from __future__ import annotations

import re
import subprocess
from typing import Optional

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

log_analysis_bp = Blueprint("log_analysis_bp", __name__)

JOURNAL_UNIT = "solivox"
DEFAULT_HOURS = 24
MIN_HOURS = 1
MAX_HOURS = 72
# Heuristic only (not a trading threshold - purely how long a buy/sell
# attempt took to flag as worth a human's attention in this report).
DEFAULT_LATENCY_WARN_MS = 3000.0


def fetch_journal_logs(hours: int = DEFAULT_HOURS, timeout_sec: int = 20) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (log_text, error). Exactly one of the two is non-None.

    hours is clamped server-side and turned into a fixed, safe --since
    string here - never pass user-supplied text straight into the command.
    subprocess.run is called with a list (not shell=True), so there is no
    shell-injection surface regardless of the hours value.
    """
    hours = max(MIN_HOURS, min(MAX_HOURS, int(hours)))
    try:
        result = subprocess.run(
            ["journalctl", "-u", JOURNAL_UNIT, "--since", f"{hours} hours ago", "--no-pager"],
            capture_output=True, text=True, timeout=timeout_sec,
        )
    except FileNotFoundError:
        return None, (
            "journalctl is not available in this environment (expected on the live "
            "Linux server; not present on a local dev machine)."
        )
    except subprocess.TimeoutExpired:
        return None, f"journalctl did not return within {timeout_sec}s."
    except Exception as exc:  # pragma: no cover - defensive, see module docstring
        return None, f"Unexpected error running journalctl: {exc}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "permission" in stderr.lower() or "denied" in stderr.lower():
            return None, (
                "journalctl permission denied - the app's service user likely needs "
                "to be in the systemd-journal group (or run as a user with journal "
                f"read access) to read the '{JOURNAL_UNIT}' unit's logs."
            )
        return None, f"journalctl exited {result.returncode}: {stderr or '(no stderr)'}"

    return result.stdout, None


# (category, severity, compiled pattern, likely cause). Order matters - first
# match wins per line, so the generic ERROR catch-all at the bottom only
# picks up what nothing more specific already explained. Every pattern below
# is a real, confirmed string from this codebase (autosnipe_buy_new_token.py,
# autosnipe_sell_script.py, sniper_stream.py, yellowstone_cache.py), not
# invented - see this file's own tests for the exact source citations.
_PATTERNS: list[tuple[str, str, "re.Pattern[str]", str]] = [
    (
        "Buy skipped - insufficient balance",
        "info",
        re.compile(r"Insufficient balance\. Wallet has"),
        "Wallet's SOL balance was below the configured buy amount + reserve at the moment a buy was attempted.",
    ),
    (
        "Tokens skipped - buy conditions not met",
        "info",
        re.compile(r"Buy conditions NOT MET for"),
        "A detected token's scan window closed without reaching the configured qualifying-transaction count/range.",
    ),
    (
        "Detector errors",
        "warning",
        re.compile(r"\[Detector\] Error processing transaction|\[Tx Fetcher\] Error"),
        "A transaction couldn't be parsed/fetched while scanning for new tokens - usually a transient RPC issue.",
    ),
    (
        # Checked before "Buy failures" below - these are autosnipe_sell_script.py's
        # own strings (trade_ref-suffixed: "...for <trade_ref>: ..."), which are a
        # superset of the shorter buy-side strings and must be matched first or
        # the buy pattern would wrongly claim them as substring matches.
        "Sell failures",
        "error",
        re.compile(
            r"Error performing swap for|Error fetching quote for|Error sending transaction for|"
            r"Auto-snipe sell error for|Rate limited (performing swap|fetching quote) for|"
            r"Unexpected error sending a sell in this cycle|Unexpected error in auto_trade loop|"
            r"Sale sent for .* but DB update failed"
        ),
        "A sell's swap/quote/transaction-send failed, or its result couldn't be persisted - check the cited trade.",
    ),
    (
        # autosnipe_buy_new_token.py's own (shorter, no trade_ref) strings.
        "Buy failures",
        "error",
        re.compile(
            r"Error performing swap|Error fetching quote|Buy transaction for .* rejected:|"
            r"Failed to fetch wallet for user|Failed to fetch private key|"
            r"No active AutoSnipeConfig found"
        ),
        "A buy's swap/quote failed, was rejected, or couldn't find a usable wallet/key - check the cited mint.",
    ),
    (
        "Pricing failures",
        "warning",
        re.compile(r"Yellowstone price fetch failed for trade|Failed to fetch price for trade"),
        "A sell-decision cycle couldn't get a current price for a trade and had to skip that tick.",
    ),
    (
        "Yellowstone connection issues",
        "warning",
        re.compile(r"\[YellowstoneCache\] stream error"),
        "The persistent Geyser subscription dropped and is reconnecting - sell pricing falls back to RPC until it's back.",
    ),
]

_GENERIC_ERROR = re.compile(r"\s-\s*ERROR\s*-\s")
_LATENCY_LINE = re.compile(r"\[latency\]\s+path=(?P<path>\w+).*?\btotal=(?P<total>[\d.]+)")


def analyze_log_text(text: str, latency_warn_ms: float = DEFAULT_LATENCY_WARN_MS) -> dict:
    """
    Pure function - no I/O. Takes raw journalctl text, returns a structured
    report: one finding per category actually seen, each with a count, a
    small evidence sample (never the full set - keeps the report readable),
    and a plain-language likely cause. Sorted worst-first.
    """
    lines = text.splitlines() if text else []
    buckets: dict[str, dict] = {}

    def _bucket(category: str, severity: str, cause: str) -> dict:
        return buckets.setdefault(
            category, {"category": category, "severity": severity, "count": 0, "evidence": [], "likely_cause": cause}
        )

    latency_total = 0
    latency_slow: list[tuple[float, str]] = []

    for line in lines:
        matched = False
        for category, severity, pattern, cause in _PATTERNS:
            if pattern.search(line):
                b = _bucket(category, severity, cause)
                b["count"] += 1
                if len(b["evidence"]) < 5:
                    b["evidence"].append(line.strip())
                matched = True
                break
        if matched:
            continue

        lat = _LATENCY_LINE.search(line)
        if lat:
            latency_total += 1
            total_ms = float(lat.group("total"))
            if total_ms >= latency_warn_ms:
                latency_slow.append((total_ms, line.strip()))
            continue

        if _GENERIC_ERROR.search(line):
            b = _bucket(
                "Other errors", "error",
                "An ERROR-level log line that didn't match a more specific known pattern above - read the evidence directly.",
            )
            b["count"] += 1
            if len(b["evidence"]) < 5:
                b["evidence"].append(line.strip())

    if latency_slow:
        latency_slow.sort(key=lambda t: t[0], reverse=True)
        b = _bucket(
            "Abnormal latency",
            "warning",
            f"Buy/sell attempts took longer than {latency_warn_ms:.0f}ms end-to-end "
            f"(out of {latency_total} timed attempts seen in this window).",
        )
        b["count"] = len(latency_slow)
        b["evidence"] = [line for _, line in latency_slow[:5]]

    findings = list(buckets.values())
    _severity_rank = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (_severity_rank.get(f["severity"], 3), -f["count"]))

    return {
        "lines_analyzed": len(lines),
        "findings": findings,
    }


def run_log_analysis(hours: int = DEFAULT_HOURS, latency_warn_ms: float = DEFAULT_LATENCY_WARN_MS) -> dict:
    hours = max(MIN_HOURS, min(MAX_HOURS, int(hours)))
    text, error = fetch_journal_logs(hours=hours)
    if error is not None:
        return {"hours": hours, "source_error": error, "lines_analyzed": 0, "findings": []}
    result = analyze_log_text(text, latency_warn_ms=latency_warn_ms)
    result["hours"] = hours
    result["source_error"] = None
    return result


@log_analysis_bp.route("/api/logs/analyze", methods=["GET"])
@jwt_required()
def analyze_logs_endpoint():
    """Read-only - runs journalctl (no write flags) and pattern-matches its
    output. Never touches trading logic, config, or production settings."""
    try:
        hours = int(request.args.get("hours", DEFAULT_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_HOURS
    return jsonify(run_log_analysis(hours=hours)), 200
