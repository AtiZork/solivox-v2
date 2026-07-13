"""
Dashboard TokenPrice ingestion.

Primary: WebSocket accountSubscribe on Pump.fun bonding curves (price_stream.py).
Fallback: HTTP/Jupiter polling for open trades when WS is off or as backup.
"""

from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import logging

from models import db, Trade, TokenPrice
from settings import PRICE_HTTP_FALLBACK, PRICE_USE_WEBSOCKET
from utils import get_token_symbol_and_price

logger = logging.getLogger(__name__)

_http_scheduler = None


def create_scheduler(app):
    """Start live pricing: WebSocket first, optional HTTP fallback."""
    global _http_scheduler

    ws_started = False
    if PRICE_USE_WEBSOCKET:
        try:
            from price_stream import start_price_stream

            ws_started = start_price_stream(app)
            if ws_started:
                print("[Price] WebSocket live pricing started (bonding-curve accountSubscribe).")
        except Exception as exc:
            print(f"[Price] WebSocket price stream failed to start: {exc}")
            logger.exception("Price WS start failed")

    if not PRICE_HTTP_FALLBACK:
        if ws_started:
            print("[Price] HTTP fallback disabled — WebSocket only.")
        else:
            print("[Price] No pricing ingestion started (WS failed, HTTP fallback off).")
        return

    if _http_scheduler and _http_scheduler.running:
        return

    # WS primary → slow HTTP backup; WS off → faster HTTP primary
    http_interval_seconds = 120 if ws_started else 30

    scheduler = BackgroundScheduler(daemon=True)
    _http_scheduler = scheduler

    def fetch_and_store_prices():
        with app.app_context():
            try:
                trades = (
                    Trade.query.filter_by(executed=False)
                    .order_by(Trade.created_at.desc())
                    .limit(20)
                    .all()
                )
                if not trades:
                    trades = Trade.query.order_by(Trade.created_at.desc()).limit(5).all()

                for trade_ in trades:
                    address = trade_.token_address
                    if not address:
                        continue
                    try:
                        price_data = get_token_symbol_and_price(address)
                    except Exception as fetch_exc:
                        print(f"[×] Price fetch failed for {address[:8]}...: {fetch_exc}")
                        continue
                    if not price_data or not isinstance(price_data, dict):
                        continue
                    token_price = price_data.get("usdPrice")
                    if not token_price:
                        continue
                    tp = TokenPrice(
                        trade_id=trade_.id,
                        token_address=trade_.token_address,
                        token_name=trade_.token_name or price_data.get("name"),
                        symbol=trade_.token_symbol or price_data.get("symbol"),
                        price=token_price,
                    )
                    db.session.add(tp)
                    db.session.commit()
                    print(
                        f"[✓][HTTP] Stored price {token_price} for "
                        f"{trade_.token_name or address[:8]}"
                    )
            except Exception as e:
                print("[×] Scheduler Error:", e)
                try:
                    db.session.rollback()
                except Exception:
                    pass

    scheduler.add_job(
        fetch_and_store_prices,
        trigger="interval",
        seconds=http_interval_seconds,
        id="token_price_http_fallback",
        replace_existing=True,
    )
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    mode = "backup (WS primary)" if ws_started else "primary"
    print(f"[Price] HTTP price scheduler running every {http_interval_seconds}s ({mode}).")
