"""SellStream (instant sell trigger) against a scripted fake WebSocket."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from sell_stream import SellStream
from shyft_pricing import ShyftPricingService


class FakeWS:
    """Answers subscribe requests and replays scripted notifications."""

    def __init__(self, stream, script=None, fail_mints=()):
        self.stream = stream
        self.sent = []
        self.inbox = []
        self.script = list(script or [])
        self.fail_mints = set(fail_mints)
        self.next_sub = 100

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["method"] == "logsSubscribe":
            mint = msg["params"][0]["mentions"][0]
            if mint in self.fail_mints:
                self.inbox.append({"jsonrpc": "2.0", "id": msg["id"], "error": {"message": "nope"}})
            else:
                self.next_sub += 1
                self.inbox.append({"jsonrpc": "2.0", "id": msg["id"], "result": self.next_sub})

    async def recv(self):
        await asyncio.sleep(0)
        if self.inbox:
            return json.dumps(self.inbox.pop(0))
        if self.script:
            step = self.script.pop(0)
            if callable(step):
                step(self)
                return "{}"
            return json.dumps(step)
        self.stream._running = False
        return "{}"


def _notification(sub_id, err=None):
    return {
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {"subscription": sub_id, "result": {"value": {"signature": "sig", "err": err, "logs": []}}},
    }


def _run(stream, ws):
    stream._running = True
    asyncio.run(stream._session(ws))


def test_subscribes_every_open_mint_at_confirmed_commitment():
    stream = SellStream(get_open_mints=lambda: {"MintA", "MintB"}, on_activity=MagicMock(), ws_urls=["ws://x"])
    ws = FakeWS(stream)
    _run(stream, ws)

    subs = [m for m in ws.sent if m["method"] == "logsSubscribe"]
    assert sorted(m["params"][0]["mentions"][0] for m in subs) == ["MintA", "MintB"]
    assert all(m["params"][1] == {"commitment": "confirmed"} for m in subs)
    assert set(stream.subscribed) == {"MintA", "MintB"}


def test_trade_notification_triggers_activity_for_that_mint_only():
    activity = MagicMock()
    stream = SellStream(get_open_mints=lambda: {"MintA"}, on_activity=activity, ws_urls=["ws://x"])
    ws = FakeWS(stream, script=[lambda ws: ws.inbox.append(_notification(stream.subscribed["MintA"])),
                                _notification(999)])  # unknown subscription: ignored
    _run(stream, ws)

    activity.assert_called_once_with("MintA")
    assert stream.notifications == 1


def test_failed_transaction_notification_is_ignored():
    activity = MagicMock()
    stream = SellStream(get_open_mints=lambda: {"MintA"}, on_activity=activity, ws_urls=["ws://x"])
    ws = FakeWS(stream, script=[
        lambda ws: ws.inbox.append(_notification(stream.subscribed["MintA"], err={"InstructionError": [0, 1]}))
    ])
    _run(stream, ws)

    activity.assert_not_called()


def test_closed_trades_are_unsubscribed_on_next_sync():
    open_mints = {"MintA", "MintB"}
    stream = SellStream(get_open_mints=lambda: set(open_mints), on_activity=MagicMock(),
                        sync_interval=0, ws_urls=["ws://x"])
    ws = FakeWS(stream, script=["{}", lambda ws: open_mints.discard("MintB"), "{}"])
    _run(stream, ws)

    unsubs = [m for m in ws.sent if m["method"] == "logsUnsubscribe"]
    assert len(unsubs) == 1
    assert set(stream.subscribed) == {"MintA"}


def test_new_buys_are_subscribed_on_next_sync_without_duplicates():
    open_mints = {"MintA"}
    stream = SellStream(get_open_mints=lambda: set(open_mints), on_activity=MagicMock(),
                        sync_interval=0, ws_urls=["ws://x"])
    ws = FakeWS(stream, script=["{}", lambda ws: open_mints.add("MintNew"), "{}", "{}"])
    _run(stream, ws)

    subs = [m["params"][0]["mentions"][0] for m in ws.sent if m["method"] == "logsSubscribe"]
    assert sorted(subs) == ["MintA", "MintNew"]


def test_subscribe_error_is_not_counted_as_subscribed():
    stream = SellStream(get_open_mints=lambda: {"MintA", "MintBad"}, on_activity=MagicMock(), ws_urls=["ws://x"])
    ws = FakeWS(stream, fail_mints={"MintBad"})
    _run(stream, ws)

    assert set(stream.subscribed) == {"MintA"}


def test_open_mints_failure_keeps_running():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        return {"MintA"}

    stream = SellStream(get_open_mints=flaky, on_activity=MagicMock(), sync_interval=0, ws_urls=["ws://x"])
    ws = FakeWS(stream, script=["{}", "{}"])
    _run(stream, ws)

    assert set(stream.subscribed) == {"MintA"}


def test_pumpswap_pool_lookup_is_cached():
    svc = ShyftPricingService.__new__(ShyftPricingService)
    svc._rpc = MagicMock(return_value=[{"pubkey": "Pool111"}])
    assert svc.find_pumpswap_pool("MintA") == "Pool111"
    assert svc.find_pumpswap_pool("MintA") == "Pool111"
    assert svc._rpc.call_count == 1


def test_pumpswap_pool_miss_is_not_cached():
    svc = ShyftPricingService.__new__(ShyftPricingService)
    svc._rpc = MagicMock(side_effect=[[], [{"pubkey": "Pool222"}]])
    assert svc.find_pumpswap_pool("MintB") is None
    assert svc.find_pumpswap_pool("MintB") == "Pool222"
