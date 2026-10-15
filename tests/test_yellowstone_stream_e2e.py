"""
End-to-end tests for YellowstonePriceStream against a real (local,
in-process) gRPC server implementing the Geyser Subscribe RPC - exercises
the actual background threads, the real gRPC wire protocol, and the
reconnect/reseed logic over a real socket, instead of calling the stream's
internal methods directly like test_yellowstone_stream.py does.

Only get_token_price (the RPC seed/reseed read) is mocked, since that talks
to Shyft/the Solana RPC, not to Yellowstone - everything Yellowstone-side
goes over the real local server.
"""

from __future__ import annotations

import socket
import threading
import time
from concurrent import futures
from unittest.mock import MagicMock, patch

import grpc
import pytest

import yellowstone_pricing as yp
from yellowstone_grpc.generated import geyser_pb2, geyser_pb2_grpc

PUMP_MINT = "6BFPDdf7VdkFdzePjWzVENzgigzs1DJmZJhKtjiTpump"
BONDING_CURVE = "4nMhJXcDEJcAgkKsy4xPwMHdPqPPMnqMwE4Drse4u6P1"


def _bonding_raw(vt: int = 1_000_000_000_000, vs: int = 30_000_000_000, complete: bool = False) -> bytes:
    raw = bytearray(64)
    raw[8:16] = int(vt).to_bytes(8, "little")
    raw[16:24] = int(vs).to_bytes(8, "little")
    raw[48] = 1 if complete else 0
    return bytes(raw)


def _account_update(pubkey: bytes, data: bytes) -> geyser_pb2.SubscribeUpdate:
    update = geyser_pb2.SubscribeUpdate()
    update.account.account.pubkey = pubkey
    update.account.account.data = data
    update.account.slot = 1
    return update


def _bonding_curve_price(mint: str, usd: float = 1.0) -> dict:
    return {
        "mint": mint,
        "bonding_curve": BONDING_CURVE,
        "price_in_sol": 0.01,
        "usd_price": usd,
        "usdPrice": usd,
        "sol_price_usd": 200.0,
        "source": "shyft_rpc_pumpfun_bonding_curve",
    }


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _poll_until(predicate, timeout=5.0, interval=0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _FakeGeyserServicer(geyser_pb2_grpc.GeyserServicer):
    """Minimal fake of the Yellowstone Geyser Subscribe RPC: records every
    SubscribeRequest the client sends, and lets the test push
    SubscribeUpdate messages to whichever client is currently connected."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current_outgoing: "object | None" = None
        self.connect_count = 0
        self.all_received_requests: list = []

    def Subscribe(self, request_iterator, context):
        import queue as _queue

        my_queue: "_queue.Queue" = _queue.Queue()
        with self.lock:
            self.current_outgoing = my_queue
            self.connect_count += 1

        def _reader():
            try:
                for req in request_iterator:
                    with self.lock:
                        self.all_received_requests.append(req)
            except Exception:
                pass

        threading.Thread(target=_reader, daemon=True).start()

        while True:
            item = my_queue.get()
            if item is None:
                return
            yield item

    def push(self, update) -> None:
        with self.lock:
            q = self.current_outgoing
        if q is not None:
            q.put(update)


def _start_server(port: int):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    servicer = _FakeGeyserServicer()
    geyser_pb2_grpc.add_GeyserServicer_to_server(servicer, server)
    bound = server.add_insecure_port(f"127.0.0.1:{port}")
    assert bound == port, f"could not bind test gRPC server to port {port}"
    server.start()
    return server, servicer


@pytest.fixture
def grpc_server():
    port = _free_port()
    server, servicer = _start_server(port)
    yield port, servicer, server
    server.stop(None)


@pytest.fixture(autouse=True)
def _fast_reconnect_and_bonding_curve(monkeypatch):
    monkeypatch.setattr(yp, "USE_GEYSER", True)
    monkeypatch.setattr(yp, "GEYSER_USE_TLS", False)
    monkeypatch.setattr(yp, "GEYSER_GRPC_TOKEN", "")
    monkeypatch.setattr(yp, "_RECONNECT_DELAY_SEC", 0.2)
    monkeypatch.setattr(yp, "_MAX_RECONNECT_DELAY_SEC", 0.5)
    monkeypatch.setattr(yp.ShyftPricingService, "bonding_curve_pda", staticmethod(lambda mint: BONDING_CURVE))


def test_stream_connects_tracks_a_mint_and_receives_a_live_push(monkeypatch, grpc_server):
    port, servicer, _server = grpc_server
    monkeypatch.setattr(yp, "GEYSER_GRPC_URL", f"127.0.0.1:{port}")

    stream = yp.YellowstonePriceStream(get_open_mints=lambda: set(), sync_interval=0.2)
    try:
        with patch.object(yp, "get_token_price", return_value=_bonding_curve_price(PUMP_MINT, usd=1.0)):
            stream.start()
            assert _poll_until(lambda: stream.connected), "stream never connected to the test server"

            stream.ensure_tracked(PUMP_MINT)
            assert _poll_until(lambda: len(servicer.all_received_requests) >= 2), (
                "server never received the updated subscribe request for the tracked mint"
            )
            last_req = servicer.all_received_requests[-1]
            assert BONDING_CURVE in list(last_req.accounts["sell_price"].account)

            svc = MagicMock()
            svc.get_sol_usd.return_value = 123.0
            with patch.object(yp, "get_shyft_pricing_service", lambda: svc):
                servicer.push(_account_update(yp._pubkey_bytes(BONDING_CURVE), _bonding_raw()))

                def _pushed():
                    cached = stream.get_cached_price(PUMP_MINT)
                    return cached is not None and cached.get("sol_price_usd") == 123.0

                assert _poll_until(_pushed), "cache was never updated by the live gRPC push"
    finally:
        stream.stop()


def test_stream_reconnects_and_reseeds_tracked_mints_after_server_restart(monkeypatch, grpc_server):
    port, _servicer, server = grpc_server
    monkeypatch.setattr(yp, "GEYSER_GRPC_URL", f"127.0.0.1:{port}")

    call_count = {"n": 0}

    def _fake_get_token_price(mint, include_token_details=False):
        call_count["n"] += 1
        return _bonding_curve_price(mint, usd=float(call_count["n"]))

    # get_open_mints reports PUMP_MINT as open throughout (matching real
    # usage: a mint only gets ensure_tracked()'d while its position is
    # open) so the periodic sync thread doesn't race the reconnect reseed
    # by evicting it the moment it sees an "empty" open-position set.
    stream = yp.YellowstonePriceStream(get_open_mints=lambda: {PUMP_MINT}, sync_interval=0.2)
    try:
        with patch.object(yp, "get_token_price", side_effect=_fake_get_token_price):
            stream.start()
            assert _poll_until(lambda: stream.connected)

            stream.ensure_tracked(PUMP_MINT)
            assert call_count["n"] == 1

            server.stop(None)
            assert _poll_until(lambda: not stream.connected), "stream never noticed the server going away"

            new_server, _new_servicer = _start_server(port)
            try:
                assert _poll_until(lambda: stream.connected, timeout=5.0), "stream never reconnected"
                assert _poll_until(lambda: call_count["n"] >= 2, timeout=5.0), (
                    "already-tracked mint was never re-seeded after reconnect"
                )
            finally:
                new_server.stop(None)
    finally:
        stream.stop()


def test_stream_sync_loop_adds_newly_open_position_without_manual_ensure_tracked(monkeypatch, grpc_server):
    port, servicer, _server = grpc_server
    monkeypatch.setattr(yp, "GEYSER_GRPC_URL", f"127.0.0.1:{port}")

    open_mints = {"mints": set()}
    stream = yp.YellowstonePriceStream(get_open_mints=lambda: set(open_mints["mints"]), sync_interval=0.2)
    try:
        with patch.object(yp, "get_token_price", return_value=_bonding_curve_price(PUMP_MINT, usd=1.0)):
            stream.start()
            assert _poll_until(lambda: stream.connected)

            open_mints["mints"] = {PUMP_MINT}  # a position just opened
            assert _poll_until(lambda: stream.get_cached_price(PUMP_MINT) is not None, timeout=3.0), (
                "periodic sync never picked up the newly open position"
            )

            open_mints["mints"] = set()  # position closed
            assert _poll_until(lambda: stream.get_cached_price(PUMP_MINT) is None, timeout=3.0), (
                "periodic sync never dropped the closed position"
            )
    finally:
        stream.stop()
