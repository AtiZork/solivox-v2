"""Unit tests for ShyftPricingService (mocked HTTP/RPC)."""

from __future__ import annotations

import base64
import struct
from unittest.mock import MagicMock, patch

import pytest
import requests

from shyft_pricing import (
    ShyftPricingError,
    ShyftPricingService,
    TokenPriceSnapshot,
    _redact_url,
    is_valid_mint,
)


PUMP_MINT = "6BFPDdf7VdkFdzePjWzVENzgigzs1DJmZJhKtjiTpump"


def _bonding_raw(vt: int = 1_000_000_000_000, vs: int = 30_000_000_000) -> bytes:
    raw = bytearray(64)
    raw[8:16] = int(vt).to_bytes(8, "little")
    raw[16:24] = int(vs).to_bytes(8, "little")
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
