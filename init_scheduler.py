from apscheduler.schedulers.background import BackgroundScheduler
from models import db, Trade, TokenPrice
from utils import get_token_symbol_and_price
import atexit


def create_scheduler(app):
    scheduler = BackgroundScheduler(daemon=True)

    def fetch_and_store_prices():
        with app.app_context():
            try:
                trades = Trade.query.order_by(Trade.created_at.desc()).limit(2).all()
                for trade_ in trades:
                    address = trade_.token_address
                    token_price = get_token_symbol_and_price(address)["usdPrice"]
                    if token_price:
                        tp = TokenPrice(
                            trade_id=trade_.id,
                            token_address=trade_.token_address,
                            token_name=trade_.token_name,
                            symbol=trade_.token_symbol,
                            price=token_price
                        )
                        db.session.add(tp)
                        db.session.commit()
                        print(f"[✓] Stored price {token_price} for {trade_.token_name}")
            except Exception as e:
                print("[×] Scheduler Error:", e)

    scheduler.add_job(fetch_and_store_prices, trigger='interval', minutes=15)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
