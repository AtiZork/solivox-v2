"""Tests for half-transaction extended buy scan."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from autosnipe_buy_logic import (
    extended_scan_deadline,
    half_txn_threshold,
    should_start_half_txn_extended_scan,
    validate_half_txns_scan_duration,
)
from autosnipe_buy_new_token import autosnipe_buy_new_token, should_buy_token


def test_half_txn_threshold_is_dynamic():
    assert half_txn_threshold(80) == 40.0
    assert half_txn_threshold(100) == 50.0
    assert half_txn_threshold(5) == 2.5


def test_disabled_never_starts_extended_scan():
    assert should_start_half_txn_extended_scan(
        enabled=False, qualifying_count=40, min_txns=80
    ) is False


def test_enabled_below_half_no_extended_scan():
    assert should_start_half_txn_extended_scan(
        enabled=True, qualifying_count=39, min_txns=80
    ) is False
    # odd threshold: 5 → half 2.5, so 2 does not qualify
    assert should_start_half_txn_extended_scan(
        enabled=True, qualifying_count=2, min_txns=5
    ) is False


def test_enabled_exactly_half_starts_extended_scan():
    assert should_start_half_txn_extended_scan(
        enabled=True, qualifying_count=40, min_txns=80
    ) is True
    assert should_start_half_txn_extended_scan(
        enabled=True, qualifying_count=3, min_txns=5
    ) is True


def test_enabled_above_half_below_full_starts_extended_scan():
    assert should_start_half_txn_extended_scan(
        enabled=True, qualifying_count=60, min_txns=80
    ) is True


def test_full_threshold_does_not_start_extended_scan():
    assert should_start_half_txn_extended_scan(
        enabled=True, qualifying_count=80, min_txns=80
    ) is False


def test_extended_scan_adds_configured_duration_on_top_of_initial():
    # 10s initial + 30s additional → deadline at start + 40
    assert extended_scan_deadline(1000.0, 10, 30) == 1040.0
    # 10s initial + 40s additional → deadline at start + 50
    assert extended_scan_deadline(1000.0, 10, 40) == 1050.0
    assert extended_scan_deadline(1000.0, 5, 45) == 1050.0


@pytest.mark.parametrize(
    "duration,enabled,expect_error",
    [
        (0, True, True),
        (-5, True, True),
        ("abc", True, True),
        (None, True, True),
        (30, True, False),
        (0, False, False),
        (None, False, False),
    ],
)
def test_half_txns_scan_duration_validation(duration, enabled, expect_error):
    errors = validate_half_txns_scan_duration(duration, enabled=enabled)
    assert bool(errors) is expect_error


def _config(**kwargs):
    base = dict(
        user_id=1,
        active=True,
        launch_delay=5,
        min_txns=80,
        buy_txns_over_80_usd=80,
        half_txns_scan_enabled=False,
        half_txns_scan_duration=30,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _qualifying_txns(n: int, usd: float = 100.0):
    return [{"usd_value": usd} for _ in range(n)]


@patch("autosnipe_buy_new_token.time.sleep", return_value=None)
@patch("autosnipe_buy_new_token.get_token_specific_transactions")
def test_should_buy_disabled_stops_after_normal_window(mock_txns, _sleep):
    """Feature disabled → no extended scan even at half threshold."""
    config = _config(half_txns_scan_enabled=False, launch_delay=2, min_txns=80)
    mock_txns.return_value = _qualifying_txns(40)

    t0 = 1000.0
    times = [t0]  # start
    # loop while elapsed < 2: several iterations then exit
    times += [t0 + 0.5, t0 + 1.0, t0 + 1.5, t0 + 2.1]
    # if extend were wrongly entered, more times would be consumed
    with patch("autosnipe_buy_new_token.time.time", side_effect=times + [t0 + 99] * 5):
        with patch("autosnipe_buy_new_token.sniper_stream", create=True):
            pass
        # Force HTTP path by making WS inactive / ImportError path
        with patch.dict("sys.modules", {"sniper_stream": None}):
            # Re-import path: is_ws_active ImportError via failed import of attributes
            import builtins

            real_import = builtins.__import__

            def _import(name, *args, **kwargs):
                if name == "sniper_stream":
                    raise ImportError("forced")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=_import):
                result = should_buy_token("Mint", 1, config)

    assert result is False
    # Only normal-window polls; not extended
    assert mock_txns.call_count <= 4


@patch("autosnipe_buy_new_token.time.sleep", return_value=None)
def test_should_buy_enabled_below_half_no_extend(_sleep):
    config = _config(half_txns_scan_enabled=True, launch_delay=1, min_txns=80)
    t0 = 2000.0
    times = iter([t0, t0 + 0.5, t0 + 1.1, t0 + 1.2])

    with patch("autosnipe_buy_new_token.time.time", side_effect=lambda: next(times, t0 + 99)):
        with patch(
            "autosnipe_buy_new_token.get_token_specific_transactions",
            return_value=_qualifying_txns(20),
        ) as mock_txns:
            real_import = __import__

            def _import(name, *args, **kwargs):
                if name == "sniper_stream":
                    raise ImportError("forced")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=_import):
                result = should_buy_token("Mint", 1, config)

    assert result is False
    assert mock_txns.call_count >= 1


@patch("autosnipe_buy_new_token.time.sleep", return_value=None)
def test_should_buy_enabled_at_half_extends_with_configured_duration(_sleep):
    config = _config(
        half_txns_scan_enabled=True,
        launch_delay=5,
        half_txns_scan_duration=30,
        min_txns=80,
    )
    # Timeline: normal window 0..5, then additional 30s → total until start+35
    clock = {"t": 1000.0}

    def fake_time():
        return clock["t"]

    def advance_on_sleep(_s):
        clock["t"] += 1.0

    call_count = {"n": 0}

    def fake_txns(*_a, **_k):
        call_count["n"] += 1
        # Reach full threshold well into the additional window (past t=20)
        if clock["t"] - 1000.0 >= 20:
            return _qualifying_txns(80)
        return _qualifying_txns(40)

    with patch("autosnipe_buy_new_token.time.time", side_effect=fake_time):
        with patch("autosnipe_buy_new_token.time.sleep", side_effect=advance_on_sleep):
            with patch(
                "autosnipe_buy_new_token.get_token_specific_transactions",
                side_effect=fake_txns,
            ):
                real_import = __import__

                def _import(name, *args, **kwargs):
                    if name == "sniper_stream":
                        raise ImportError("forced")
                    return real_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=_import):
                    result = should_buy_token("Mint", 1, config)

    assert result is True
    # Must have scanned past the normal 5s window into the additional period
    assert clock["t"] - 1000.0 >= 20
    # Total allowed window is 5 + 30 = 35s from start
    assert clock["t"] - 1000.0 <= 35


@patch("autosnipe_buy_new_token.time.sleep", return_value=None)
def test_should_buy_full_threshold_during_normal_scan(_sleep):
    config = _config(half_txns_scan_enabled=True, launch_delay=5, min_txns=80)
    clock = {"t": 3000.0}

    with patch("autosnipe_buy_new_token.time.time", side_effect=lambda: clock["t"]):
        with patch(
            "autosnipe_buy_new_token.get_token_specific_transactions",
            return_value=_qualifying_txns(80),
        ) as mock_txns:
            real_import = __import__

            def _import(name, *args, **kwargs):
                if name == "sniper_stream":
                    raise ImportError("forced")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=_import):
                result = should_buy_token("Mint", 1, config)

    assert result is True
    assert mock_txns.call_count == 1


def test_autosnipe_buy_new_token_calls_buy_once():
    """Integration: meeting buy conditions triggers buy_token exactly once."""
    config = _config(active=True)
    with patch(
        "autosnipe_buy_new_token.should_buy_token", return_value=True
    ) as mock_should:
        with patch(
            "autosnipe_buy_new_token.buy_token", return_value=True
        ) as mock_buy:
            autosnipe_buy_new_token("MintABC", config)

    mock_should.assert_called_once()
    mock_buy.assert_called_once_with("MintABC", config)


def test_inactive_config_skips_buy_entirely():
    config = _config(active=False)
    with patch("autosnipe_buy_new_token.should_buy_token") as mock_should:
        with patch("autosnipe_buy_new_token.buy_token") as mock_buy:
            assert autosnipe_buy_new_token("MintABC", config) is False
    mock_should.assert_not_called()
    mock_buy.assert_not_called()


def test_validate_config_payload_rejects_non_positive_duration_when_enabled():
    # Mirrors autosnipe._validate_config_payload half-duration rules without importing Flask app deps.
    errors = validate_half_txns_scan_duration(0, enabled=True)
    assert any("positive" in e for e in errors)

    errors_ok = validate_half_txns_scan_duration(30, enabled=True)
    assert errors_ok == []
