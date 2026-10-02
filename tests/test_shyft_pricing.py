"""Unit tests for ShyftPricingService (mocked HTTP/RPC)."""

from __future__ import annotations

import base64
import struct
from unittest.mock import MagicMock, patch

import pytest
import requests

from shyft_pricing import (
    BondingCurveMigratedError,
    ShyftPricingError,
    ShyftPricingService,
    TokenPriceSnapshot,
    WSOL_MINT,
    _redact_url,
    is_valid_mint,
)


PUMP_MINT = "6BFPDdf7VdkFdzePjWzVENzgigzs1DJmZJhKtjiTpump"


def _bonding_raw(vt: int = 1_000_000_000_000, vs: int = 30_000_000_000, complete: bool = False) -> bytes:
    raw = bytearray(64)
    raw[8:16] = int(vt).to_bytes(8, "little")
    raw[16:24] = int(vs).to_bytes(8, "little")
    raw[48] = 1 if complete else 0
    return bytes(raw)


def _pyth_raw(price: float = 150.0) -> bytes:
    # price_data * 10**exponent = price; use exponent=-8
    raw = bytearray(100)
    price_data = int(price * (10**8))
    exponent = -8
    struct.pack_into("<q", raw, 73, price_data)
    struct.pack_into("<i", raw, 89, exponent)
    return bytes(raw)


@pytest.fixture
def service() -> ShyftPricingService:
    return ShyftPricingService(
        api_key="test-key",
        rpc_url="https://rpc.shyft.to?api_key=test-key",
        ws_url="wss://rpc.shyft.to?api_key=test-key",
        api_base="https://api.shyft.to",
        network="mainnet-beta",
        session=MagicMock(),
    )


def test_redact_url_hides_api_key():
    url = "https://rpc.shyft.to?api_key=secret123&foo=1"
    redacted = _redact_url(url)
    assert "secret123" not in redacted
    assert "api_key=" in redacted
    assert "***" in redacted or "%2A%2A%2A" in redacted
    assert "foo=1" in redacted


def test_is_valid_mint():
    assert is_valid_mint(PUMP_MINT)
    assert not is_valid_mint("")
    assert not is_valid_mint("not-a-mint")


def test_price_sol_from_bonding_raw():
    raw = _bonding_raw(vt=1_000_000_000_000, vs=30_000_000_000)
    price = ShyftPricingService._price_sol_from_bonding_raw(raw)
    assert price == pytest.approx(30_000_000_000 / (1_000_000_000_000 * 1000.0))


def test_get_token_details_success(service: ShyftPricingService):
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "success": True,
        "result": {
            "name": "Test Token",
            "symbol": "TST",
            "decimals": 6,
            "image": "https://example.com/x.png",
            "current_supply": 1000,
            "description": "desc",
        },
    }
    service.session.get.return_value = mock_resp

    details = service.get_token_details(PUMP_MINT)
    assert details["name"] == "Test Token"
    assert details["symbol"] == "TST"
    assert details["mint"] == PUMP_MINT
    service.session.get.assert_called_once()
    _, kwargs = service.session.get.call_args
    assert kwargs["headers"]["x-api-key"] == "test-key"


def test_get_token_details_failure_falls_back_onchain(service: ShyftPricingService):
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock(side_effect=requests.HTTPError("417"))
    service.session.get.return_value = mock_resp

    with patch.object(
        service,
        "get_onchain_token_details",
        return_value={
            "mint": PUMP_MINT,
            "name": "Onchain",
            "symbol": "ONC",
            "decimals": 6,
            "image": None,
            "description": None,
            "current_supply": 1.0,
            "metadata_uri": None,
            "mint_authority": None,
            "freeze_authority": None,
            "raw": {},
        },
    ) as onchain:
        details = service.get_token_details(PUMP_MINT)

    assert details["name"] == "Onchain"
    assert details["symbol"] == "ONC"
    onchain.assert_called_once_with(PUMP_MINT)


def test_parse_metaplex_metadata():
    # Minimal fake metaplex layout: key(1)+auth(32)+mint(32)+name+symbol+uri
    name = b"Solcat"
    symbol = b"SOLCAT"
    uri = b"https://example.com/meta.json"
    raw = bytearray(1 + 32 + 32)
    raw += len(name).to_bytes(4, "little") + name
    raw += len(symbol).to_bytes(4, "little") + symbol
    raw += len(uri).to_bytes(4, "little") + uri
    parsed = ShyftPricingService._parse_metaplex_metadata(bytes(raw))
    assert parsed["name"] == "Solcat"
    assert parsed["symbol"] == "SOLCAT"
    assert parsed["metadata_uri"] == "https://example.com/meta.json"


def test_parse_token2022_metadata():
    # Synthetic mint blob with TokenMetadata TLV type=19 at offset 234-style layout
    name = b"Nasduck"
    symbol = b"Nasduck"
    uri = b"https://ipfs.io/ipfs/abc"
    payload = bytearray(64)  # update_authority + mint
    payload += len(name).to_bytes(4, "little") + name
    payload += len(symbol).to_bytes(4, "little") + symbol
    payload += len(uri).to_bytes(4, "little") + uri
    raw = bytearray(234)
    raw += (19).to_bytes(2, "little")
    raw += len(payload).to_bytes(2, "little")
    raw += payload
    parsed = ShyftPricingService._parse_token2022_metadata(bytes(raw))
    assert parsed["name"] == "Nasduck"
    assert parsed["symbol"] == "Nasduck"
    assert parsed["metadata_uri"] == "https://ipfs.io/ipfs/abc"


def test_to_dict_includes_usdPrice_alias():
    snap = TokenPriceSnapshot(mint=PUMP_MINT, usd_price=1.23, name="X", symbol="Y")
    data = snap.to_dict()
    assert data["usdPrice"] == 1.23
    assert data["tokenAddress"] == PUMP_MINT


def test_get_latest_price_combines_details_and_curve(service: ShyftPricingService):
    bonding = _bonding_raw()
    pyth = _pyth_raw(200.0)

    def fake_rpc(method, params):
        if method != "getAccountInfo":
            return None
        key = params[0]
        if key == str(ShyftPricingService.bonding_curve_pda(PUMP_MINT)):
            return {"value": {"data": [base64.b64encode(bonding).decode(), "base64"]}}
        if "7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE" in key:
            return {"value": {"data": [base64.b64encode(pyth).decode(), "base64"]}}
        return {"value": None}

    with patch.object(service, "_rpc", side_effect=fake_rpc), patch.object(
        service,
        "get_token_details",
        return_value={
            "name": "PumpCoin",
            "symbol": "PUMP",
            "decimals": 6,
            "image": None,
            "description": None,
            "current_supply": 1.0,
            "raw": {"name": "PumpCoin"},
        },
    ):
        snap = service.get_latest_price(PUMP_MINT)

    assert isinstance(snap, TokenPriceSnapshot)
    assert snap.symbol == "PUMP"
    assert snap.price_in_sol is not None and snap.price_in_sol > 0
    assert snap.usd_price is not None and snap.usd_price > 0
    assert snap.sol_price_usd == pytest.approx(200.0)
    assert snap.timestamp
    assert "pumpfun" in (snap.source or "")


def test_missing_mint_raises(service: ShyftPricingService):
    with pytest.raises(ShyftPricingError):
        service.get_latest_price("   ")


def test_bonding_curve_is_complete():
    assert ShyftPricingService._bonding_curve_is_complete(_bonding_raw(complete=True)) is True
    assert ShyftPricingService._bonding_curve_is_complete(_bonding_raw(complete=False)) is False
    assert ShyftPricingService._bonding_curve_is_complete(b"\x00" * 10) is False  # too short to tell


def test_get_bonding_curve_price_raises_on_migrated_token(service: ShyftPricingService):
    """A migrated (complete=true) bonding curve must never yield a price — it's frozen/stale."""
    migrated = _bonding_raw(complete=True)
    with patch.object(service, "_get_account_bytes", return_value=migrated):
        with pytest.raises(ShyftPricingError, match="migrated"):
            service.get_bonding_curve_price(PUMP_MINT)


def test_get_latest_price_raises_on_migrated_token(service: ShyftPricingService):
    """get_token_price()'s underlying call must not silently return a stale post-migration price."""
    migrated = _bonding_raw(complete=True)
    with patch.object(service, "_get_account_bytes", return_value=migrated), patch.object(
        service,
        "get_token_details",
        return_value={"name": "Graduated", "symbol": "GRAD", "decimals": 6, "raw": {}},
    ), patch.object(
        service, "get_pumpswap_pool_price", side_effect=ShyftPricingError("no pool")
    ):
        with pytest.raises(ShyftPricingError, match="no pool"):
            service.get_latest_price(PUMP_MINT)


# ---------------------------------------------------------------------------
# PumpSwap (post-migration) pricing
# ---------------------------------------------------------------------------

_POOL_BASE_MINT_OFFSET = 43
_POOL_QUOTE_MINT_OFFSET = 75
_POOL_BASE_VAULT_OFFSET = 139
_POOL_QUOTE_VAULT_OFFSET = 171
_POOL_VIRTUAL_QUOTE_RESERVES_OFFSET = 245


def _pumpswap_pool_raw(
    base_mint: str,
    quote_mint: str = WSOL_MINT,
    base_vault: str = None,
    quote_vault: str = None,
    virtual_quote_reserves: int = 0,
) -> bytes:
    import base58

    base_vault = base_vault or base58.b58encode(bytes([1]) * 32).decode()
    quote_vault = quote_vault or base58.b58encode(bytes([2]) * 32).decode()

    def decode32(s: str) -> bytes:
        b = base58.b58decode(s)
        assert len(b) == 32, f"test fixture pubkey {s!r} did not decode to 32 bytes"
        return b

    raw = bytearray(301)
    raw[_POOL_BASE_MINT_OFFSET : _POOL_BASE_MINT_OFFSET + 32] = decode32(base_mint)
    raw[_POOL_QUOTE_MINT_OFFSET : _POOL_QUOTE_MINT_OFFSET + 32] = decode32(quote_mint)
    raw[_POOL_BASE_VAULT_OFFSET : _POOL_BASE_VAULT_OFFSET + 32] = decode32(base_vault)
    raw[_POOL_QUOTE_VAULT_OFFSET : _POOL_QUOTE_VAULT_OFFSET + 32] = decode32(quote_vault)
    raw[_POOL_VIRTUAL_QUOTE_RESERVES_OFFSET : _POOL_VIRTUAL_QUOTE_RESERVES_OFFSET + 16] = (
        int(virtual_quote_reserves).to_bytes(16, "little", signed=True)
    )
    return bytes(raw)


def test_is_bonding_curve_migrated(service: ShyftPricingService):
    with patch.object(service, "_get_account_bytes", return_value=_bonding_raw(complete=True)):
        assert service.is_bonding_curve_migrated(PUMP_MINT) is True
    with patch.object(service, "_get_account_bytes", return_value=_bonding_raw(complete=False)):
        assert service.is_bonding_curve_migrated(PUMP_MINT) is False
    with patch.object(service, "_get_account_bytes", side_effect=Exception("rpc down")):
        assert service.is_bonding_curve_migrated(PUMP_MINT) is False


def test_find_pumpswap_pool_returns_first_match(service: ShyftPricingService):
    with patch.object(service, "_rpc", return_value=[{"pubkey": "PoolAddr111111111111111111111111111111111"}]) as rpc:
        pool = service.find_pumpswap_pool(PUMP_MINT)
    assert pool == "PoolAddr111111111111111111111111111111111"
    method, params = rpc.call_args[0]
    assert method == "getProgramAccounts"
    assert params[1]["filters"][0] == {"dataSize": 301}
    assert params[1]["filters"][1]["memcmp"]["bytes"] == PUMP_MINT


def test_find_pumpswap_pool_returns_none_when_not_found(service: ShyftPricingService):
    with patch.object(service, "_rpc", return_value=[]):
        assert service.find_pumpswap_pool(PUMP_MINT) is None


def test_get_pumpswap_pool_price_success(service: ShyftPricingService):
    import base58

    pool_addr = "PoolAddr111111111111111111111111111111111"
    base_vault = base58.b58encode(bytes([7]) * 32).decode()
    quote_vault = base58.b58encode(bytes([9]) * 32).decode()
    pool_raw = _pumpswap_pool_raw(
        base_mint=PUMP_MINT,
        quote_mint=WSOL_MINT,
        base_vault=base_vault,
        quote_vault=quote_vault,
        virtual_quote_reserves=1_000_000_000,  # 1 SOL of virtual reserves
    )

    def fake_rpc(method, params):
        if method == "getTokenAccountBalance":
            addr = params[0]
            if addr == base_vault:
                return {"value": {"amount": "1000000000000", "decimals": 6}}  # 1,000,000 tokens
            if addr == quote_vault:
                return {"value": {"amount": "9000000000", "decimals": 9}}  # 9 SOL
        return None

    with patch.object(service, "find_pumpswap_pool", return_value=pool_addr), patch.object(
        service, "_get_account_bytes", return_value=pool_raw
    ), patch.object(service, "_rpc", side_effect=fake_rpc), patch.object(
        service, "get_sol_usd", return_value=150.0
    ):
        price = service.get_pumpswap_pool_price(PUMP_MINT)

    # effective_quote = 9 + 1 = 10 SOL; base = 1,000,000 tokens -> price = 0.00001 SOL/token
    assert price["price_in_sol"] == pytest.approx(0.00001)
    assert price["usd_price"] == pytest.approx(0.00001 * 150.0)
    assert price["sol_price_usd"] == 150.0
    assert price["pumpswap_pool"] == pool_addr
    assert price["source"] == "shyft_rpc_pumpswap_pool"


def test_get_pumpswap_pool_price_raises_when_no_pool_found(service: ShyftPricingService):
    with patch.object(service, "find_pumpswap_pool", return_value=None):
        with pytest.raises(ShyftPricingError, match="no PumpSwap pool found"):
            service.get_pumpswap_pool_price(PUMP_MINT)


def test_get_pumpswap_pool_price_raises_on_base_mint_mismatch(service: ShyftPricingService):
    other_mint = "So11111111111111111111111111111111111111112"
    pool_raw = _pumpswap_pool_raw(base_mint=other_mint)  # mismatched vs PUMP_MINT
    with patch.object(service, "find_pumpswap_pool", return_value="PoolAddr111111111111111111111111111111111"), patch.object(
        service, "_get_account_bytes", return_value=pool_raw
    ):
        with pytest.raises(ShyftPricingError, match="mismatch"):
            service.get_pumpswap_pool_price(PUMP_MINT)


def test_get_latest_price_falls_back_to_pumpswap_on_migration(service: ShyftPricingService):
    """End-to-end (mocked): migrated bonding curve -> get_latest_price uses the PumpSwap price."""
    migrated = _bonding_raw(complete=True)
    fake_pumpswap_price = {
        "mint": PUMP_MINT,
        "bonding_curve": None,
        "pumpswap_pool": "PoolAddr111111111111111111111111111111111",
        "price_in_sol": 0.00001,
        "usd_price": 0.0015,
        "sol_price_usd": 150.0,
        "source": "shyft_rpc_pumpswap_pool",
    }
    with patch.object(service, "_get_account_bytes", return_value=migrated), patch.object(
        service, "get_pumpswap_pool_price", return_value=fake_pumpswap_price
    ) as pumpswap_call, patch.object(
        service,
        "get_token_details",
        return_value={"name": "Graduated", "symbol": "GRAD", "decimals": 6, "raw": {}},
    ):
        snap = service.get_latest_price(PUMP_MINT)

    pumpswap_call.assert_called_once_with(PUMP_MINT)
    assert snap.source == "shyft_rpc_pumpswap_pool"
    assert snap.usd_price == pytest.approx(0.0015)
    assert snap.price_in_sol == pytest.approx(0.00001)


def test_get_token_price_reusable_helper():
    from shyft_pricing import get_token_price

    fake = TokenPriceSnapshot(
        mint=PUMP_MINT,
        name="PumpCoin",
        symbol="PUMP",
        price_in_sol=0.001,
        usd_price=0.1,
        sol_price_usd=100.0,
        source="shyft_rpc_pumpfun_bonding_curve",
    )
    with patch("shyft_pricing.get_shyft_pricing_service") as mock_factory:
        svc = MagicMock()
        svc.get_latest_price.return_value = fake
        mock_factory.return_value = svc
        data = get_token_price(PUMP_MINT)

    assert data["mint"] == PUMP_MINT
    assert data["usd_price"] == 0.1
    assert data["price_in_sol"] == 0.001
    svc.get_latest_price.assert_called_once_with(PUMP_MINT, include_token_details=True)


def test_get_token_price_requires_mint():
    from shyft_pricing import get_token_price

    with pytest.raises(ShyftPricingError):
        get_token_price("")
