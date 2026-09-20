"""Pure Auto Sniper buy-scan helpers (no DB/RPC imports)."""

from __future__ import annotations


def half_txn_threshold(min_txns) -> float:
    """Dynamic 50% of configured transaction threshold."""
    return float(min_txns) / 2.0


def should_start_half_txn_extended_scan(
    *,
    enabled: bool,
    qualifying_count: int,
    min_txns: int,
) -> bool:
    """
    After the normal scan window: extend only when enabled and
    half_threshold <= qualifying < min_txns.
    """
    if not enabled:
        return False
    if qualifying_count >= min_txns:
        return False
    return qualifying_count >= half_txn_threshold(min_txns)


def extended_scan_deadline(
    start_time: float,
    normal_scan_duration,
    half_txns_scan_duration,
) -> float:
    """
    Absolute deadline for the extended scan.

    half_txns_scan_duration is added in full on top of the initial
    normal_scan_duration (e.g. 10s + 30s → deadline at start + 40s).
    """
    return (
        float(start_time)
        + float(normal_scan_duration)
        + float(half_txns_scan_duration)
    )


def validate_half_txns_scan_duration(duration, *, enabled: bool) -> list[str]:
    """Return validation error strings for half-txn scan duration."""
    errors: list[str] = []
    if duration is None:
        if enabled:
            errors.append("half_txns_scan_duration is required when half_txns_scan_enabled is true")
        return errors
    try:
        value = float(duration)
    except (TypeError, ValueError):
        errors.append("half_txns_scan_duration must be numeric")
        return errors
    if enabled and value <= 0:
        errors.append("half_txns_scan_duration must be a positive number of seconds")
    return errors
