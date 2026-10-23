"""
Yellowstone (Geyser) gRPC live Solana token pricing.

Used exclusively by the Sniper sell flow (autosnipe_sell_script.py) to fetch
the latest Pump.fun bonding-curve price right before a sell decision/execution.
The Sniper buy flow and all other pricing consumers keep using shyft_pricing.

Reuses existing project logic instead of duplicating it:
  - Bonding-curve PDA derivation + virtual-reserve parsing:
    shyft_pricing.ShyftPricingService (static helpers).
  - SOL/USD conversion: the shared ShyftPricingService's Pyth-based
    get_sol_usd() (same source live_pricing/price_stream/shyft_pricing use).

Project-wide helper:
  from yellowstone_pricing import get_yellowstone_price
  price_data = get_yellowstone_price(token_mint)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from settings import GEYSER_GRPC_URL, USE_GEYSER
from shyft_pricing import ShyftPricingService, get_shyft_pricing_service
from yellowstone_cache import get_cached_curve_raw, get_cached_pool_metadata, set_cached_curve_raw

logger = logging.getLogger(__name__)


class YellowstonePricingError(Exception):
    """Raised when a Yellowstone price cannot be resolved for the sell flow."""


def is_yellowstone_enabled() -> bool:
    """USE_GEYSER=false or missing endpoint => Sniper sell stays on Shyft pricing."""
    return bool(USE_GEYSER and GEYSER_GRPC_URL)


def _fetch_bonding_curve_account(bonding_curve_pubkey: str) -> bytes:
    """
    Latest bonding-curve raw bytes from the always-on yellowstone_cache
    (one persistent, program-wide subscription - see yellowstone_cache.py)
    instead of opening a new short-lived subscription per call.

    On a cache miss (a brand-new mint the cache hasn't seen a trade for
    yet), seeds it with a single RPC read and lets the live stream
    overwrite it the next time this curve trades.
    """
    cached = get_cached_curve_raw(bonding_curve_pubkey)
    if cached:
        return cached

    raw = get_shyft_pricing_service()._get_account_bytes(bonding_curve_pubkey)
    if not raw:
        raise YellowstonePricingError(
            f"No cached or on-chain bonding-curve data found for {bonding_curve_pubkey}"
        )
    set_cached_curve_raw(bonding_curve_pubkey, raw)
    return raw


def _pumpswap_fallback(mint: str) -> dict[str, Any]:
    """
    Price a migrated token from its PumpSwap pool. Pool *discovery*
    (address/vaults) comes from yellowstone_cache when available - the
    always-on subscription knows a pool exists the instant it's created
    on-chain, closing the multi-second gap getProgramAccounts has right
    after a migration before the pool is indexed/queryable. The two vault
    balances that actually determine price are still a live RPC read either
    way (shyft_pricing.get_pumpswap_pool_price) - that part was never slow.
    """
    cached_pool = get_cached_pool_metadata(mint)
    try:
        price = get_shyft_pricing_service().get_pumpswap_pool_price(
            mint, cached_pool_metadata=cached_pool
        )
    except Exception as exc:
        raise YellowstonePricingError(
            f"token {mint} has migrated off the Pump.fun bonding curve and its "
            f"PumpSwap price could not be resolved: {exc}"
        ) from exc

    now = time.time()
    result = dict(price)
    result.setdefault("timestamp", datetime.fromtimestamp(now, tz=timezone.utc).isoformat())
    result.setdefault("timestamp_unix", now)
    result["usdPrice"] = result.get("usd_price")
    result["tokenAddress"] = result.get("mint")
    return result


def get_yellowstone_price(token_mint: str) -> dict[str, Any]:
    """
    Fetch the latest live price for a mint via Yellowstone gRPC.

    While the token is still on its Pump.fun bonding curve, this reads its
    latest price from the always-on yellowstone_cache. Once the token has
    migrated (graduated) to PumpSwap, the bonding curve is permanently
    frozen, so this instead reads the live price from the PumpSwap pool.

    Returns a dict shaped like shyft_pricing.get_token_price()'s output
    (mint, price_in_sol, usd_price/usdPrice, sol_price_usd, source, timestamp, ...)
    so callers can swap the price source without changing their price-consumption
    code.

    Raises:
        YellowstonePricingError: disabled, misconfigured, unresolved mint,
            unavailable/timeout, or an invalid/zero price (bonding-curve or
            PumpSwap).
    """
    if not is_yellowstone_enabled():
        raise YellowstonePricingError(
            "Yellowstone is disabled (USE_GEYSER=false or GEYSER_GRPC_URL unset)"
        )

    mint = (token_mint or "").strip()
    if not mint:
        raise YellowstonePricingError("mint address is required")

    try:
        bonding_curve = ShyftPricingService.bonding_curve_pda(mint)
    except Exception as exc:
        raise YellowstonePricingError(f"Could not resolve mint {mint}: {exc}") from exc

    # Cheap pre-check: a migrated bonding curve will never deliver another
    # account update, so go straight to the PumpSwap path instead of reading
    # its now-permanently-frozen final cached/on-chain state.
    if get_shyft_pricing_service().is_bonding_curve_migrated(mint):
        return _pumpswap_fallback(mint)

    raw = _fetch_bonding_curve_account(str(bonding_curve))

    if ShyftPricingService._bonding_curve_is_complete(raw):
        # Migrated during our subscription wait — fall back instead of
        # returning the now-frozen final bonding-curve price.
        return _pumpswap_fallback(mint)

    price_sol = ShyftPricingService._price_sol_from_bonding_raw(raw)
    if not price_sol or price_sol <= 0:
        raise YellowstonePricingError(
            f"Yellowstone returned an invalid/zero price for mint {mint}"
        )

    try:
        sol_usd = get_shyft_pricing_service().get_sol_usd()
    except Exception as exc:
        raise YellowstonePricingError(f"SOL/USD conversion failed: {exc}") from exc

    if not sol_usd or sol_usd <= 0:
        raise YellowstonePricingError("SOL/USD conversion returned an invalid/zero price")

    usd_price = price_sol * sol_usd
    now = time.time()
    result = {
        "mint": mint,
        "bonding_curve": str(bonding_curve),
        "price_in_sol": price_sol,
        "usd_price": usd_price,
        "sol_price_usd": sol_usd,
        "source": "yellowstone_grpc_cached",
        "timestamp": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "timestamp_unix": now,
    }
    # Alias used by existing callers (mirrors shyft_pricing.TokenPriceSnapshot.to_dict()).
    result["usdPrice"] = result["usd_price"]
    result["tokenAddress"] = result["mint"]
    return result
