"""Pure Auto Sniper buy-scan helpers (no DB/RPC imports)."""

from __future__ import annotations


def usd_value_in_range(usd_value, min_usd_value, max_usd_value) -> bool:
    """
    True when a buy's USD value qualifies under the configured window.

    min_usd_value is the existing floor (always enforced). max_usd_value is
    the optional ceiling (client request: "instead of >100$, we need
    variable upper and lower limit") - None means no ceiling, preserving
    today's exact behavior for every config that doesn't set one.
    """
    if usd_value < min_usd_value:
        return False
    if max_usd_value is not None and usd_value > max_usd_value:
        return False
    return True


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


def validate_buy_amount_range(min_usd_value, max_usd_value) -> list[str]:
    """Return validation error strings for the min/max qualifying-buy window.

    Only called when both bounds are actually present together (see
    autosnipe.py) - a request that only touches one of the two fields has
    nothing here to cross-check against.
    """
    errors: list[str] = []
    if max_usd_value is None or min_usd_value is None:
        return errors
    try:
        min_value = float(min_usd_value)
        max_value = float(max_usd_value)
    except (TypeError, ValueError):
        return errors  # numeric-ness is already checked separately
    if max_value < min_value:
        errors.append("buy_txns_max_usd must be >= buy_txns_over_80_usd")
    return errors


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
