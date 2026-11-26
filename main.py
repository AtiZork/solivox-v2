from flask_jwt_extended import JWTManager

from autosnipe import autosnipe_bp
from autosnipe_buy_new_token import autosnipe_trade_bp
from charts import charts_bp
# from long_sell_trade import sell_trade_bp
from models import db, TradeLog
from flask import Flask, render_template
from flask_cors import CORS
from config import Config
from monitor import monitor_bp
from token_price import  token_price_bp
from trade import solana_bp
from user_register import user_register_bp
from wallet_managment import wallet_bp
from live_pricing import live_pricing_bp
from menual_sell_token import manual_sell_trade_bp
from flask_socketio import SocketIO
from trade_history import trade_history_bp
from trades_api import trades_api_bp
from dashboard_api import dashboard_api_bp
from log_analysis import log_analysis_bp
import os
# for live
# app = Flask(__name__, static_folder='/home/ubuntu/solivox-v2/static')
# for development
app = Flask(__name__)

# for development
socketio = SocketIO(app, cors_allowed_origins="*")
# for production
# socketio = SocketIO(app, async_mode="eventlet")
_jwt_secret = os.getenv("JWT_SECRET_KEY")
if not _jwt_secret:
    raise RuntimeError(
        "JWT_SECRET_KEY is not set. Add it to your local .env (see .env.example) - "
        "a hardcoded value used to be committed here, which let anyone with the "
        "source forge a login token for any user."
    )
app.config['JWT_SECRET_KEY'] = _jwt_secret
jwt = JWTManager(app)

CORS(app)  # This allows all origins
app.config.from_object(Config)
db.init_app(app)

with app.app_context():
    db.create_all()

    def _ensure_sniper_enable_columns():
        """Add sniper enable/disable columns on existing Postgres tables if missing."""
        from sqlalchemy import inspect, text

        specs = {
            "auto_snipe_config": [
                ("drop_cutoff_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("drop_after_100_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("drop_after_400_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("half_txns_scan_enabled", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("half_txns_scan_duration", "INTEGER NOT NULL DEFAULT 30"),
                ("sell_at_100", "DOUBLE PRECISION DEFAULT 10"),
                ("sell_at_300", "DOUBLE PRECISION DEFAULT 10"),
                ("sell_at_600", "DOUBLE PRECISION DEFAULT 10"),
                ("sell_at_800", "DOUBLE PRECISION DEFAULT 10"),
                ("inactivity_sell_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("inactivity_sell_seconds", "DOUBLE PRECISION NOT NULL DEFAULT 5"),
                ("critical_failsafe_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("critical_failsafe_seconds", "DOUBLE PRECISION NOT NULL DEFAULT 5"),
                ("wallet_id", "INTEGER"),
                ("sell_priority_fee", "DOUBLE PRECISION NOT NULL DEFAULT 0.01"),
                ("buy_txns_max_usd", "DOUBLE PRECISION"),
            ],
            "trade": [
                ("drop_cutoff_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("drop_after_100_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("drop_after_400_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("sell_at_100", "DOUBLE PRECISION DEFAULT 10"),
                ("sell_at_300", "DOUBLE PRECISION DEFAULT 10"),
                ("sell_at_600", "DOUBLE PRECISION DEFAULT 10"),
                ("sell_at_800", "DOUBLE PRECISION DEFAULT 10"),
                ("last_tp_tier", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
                ("initial_token_amount", "DOUBLE PRECISION"),
                ("tp_pct_sold", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
                ("inactivity_sell_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("inactivity_sell_seconds", "DOUBLE PRECISION NOT NULL DEFAULT 5"),
                ("critical_failsafe_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("critical_failsafe_seconds", "DOUBLE PRECISION NOT NULL DEFAULT 5"),
                ("peak_price", "DOUBLE PRECISION"),
                ("sell_pending", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("sell_pending_since", "TIMESTAMP"),
            ],
            "trade_history": [
                ("status", "VARCHAR(20) NOT NULL DEFAULT 'confirmed'"),
            ],
            "trade_history": [
                ("status", "VARCHAR(20) NOT NULL DEFAULT 'confirmed'"),
            ],
        }
        try:
            insp = inspect(db.engine)
            existing_tables = set(insp.get_table_names())
            with db.engine.begin() as conn:
                for table, columns in specs.items():
                    if table not in existing_tables:
                        continue
                    have = {c["name"] for c in insp.get_columns(table)}
                    for col_name, col_type in columns:
                        if col_name in have:
                            continue
                        conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {col_name} {col_type}'))
        except Exception as exc:
            print(f"[DB] ensure sniper enable columns skipped: {exc}")

    def _ensure_open_autosnipe_dedup_index():
        """
        Creates the partial unique index backing the open-position buy-dedup
        check (action-plan 1.3) on an already-existing "trade" table -
        db.create_all() above only adds indexes for a brand new table, not a
        missing one on a table that already exists. Matches models.py's
        Trade.__table_args__ exactly, so a fresh DB gets the same index via
        create_all() and this is a no-op there.

        Guarded against pre-existing duplicate (user_id, token_address) open
        AUTOSNIPE rows: there was exactly one in production (from before the
        B10 dedup check existed - see NOT_DONE_FIXES.md 1.3), which this
        index's own open-positions-only scope already resolves since one of
        that pair was already sold. If duplicates are ever found anyway,
        this logs loudly and skips creating the index rather than crashing
        startup - an unenforced dedup is the pre-existing risk, not a new one.
        """
        from sqlalchemy import inspect, text

        index_name = "ix_trade_open_autosnipe_user_mint"
        try:
            insp = inspect(db.engine)
            if "trade" not in insp.get_table_names():
                return
            if index_name in {ix["name"] for ix in insp.get_indexes("trade")}:
                return
            with db.engine.begin() as conn:
                dupes = conn.execute(text(
                    'SELECT user_id, token_address, COUNT(*) FROM trade '
                    'WHERE auto_snipe = true AND executed = false '
                    'GROUP BY user_id, token_address HAVING COUNT(*) > 1'
                )).fetchall()
                if dupes:
                    print(
                        f"[DB] skipping {index_name}: {len(dupes)} pre-existing duplicate "
                        f"open-AUTOSNIPE group(s) found: {dupes}"
                    )
                    return
                conn.execute(text(
                    f'CREATE UNIQUE INDEX {index_name} ON "trade" (user_id, token_address) '
                    'WHERE auto_snipe = true AND executed = false'
                ))
        except Exception as exc:
            print(f"[DB] ensure {index_name} skipped: {exc}")

    _ensure_sniper_enable_columns()
    _ensure_open_autosnipe_dedup_index()


def get_logs_from_db():
    return TradeLog.query.order_by(TradeLog.timestamp.desc()).all()


@app.route('/')
def index():
    return render_template('base.html', trades_filter='confirmed')


@app.route('/pending-trades')
def pending_trades():
    return render_template('base.html', trades_filter='pending')

@app.route('/auto-snipe')
def autosnipe():
    return render_template("autosnipe.html")
@app.route('/my-wallets')  # Define a route for my-wallets.html
def my_wallets():
    return render_template('my-wallets.html')  # Ensure the template exists


@app.route('/transactions')
def transactions():
    return render_template('transactions.html')


@app.route('/page-login')
def page_login():
    return render_template('page-login.html')


@app.route('/page-register')
def page_register():
    return render_template('page-register.html')


@app.route('/logs')
def logs():
    # Fetch logs from your database or source
    logs_data = get_logs_from_db()  # Replace with your actual function

    return render_template('logs.html', logs=logs_data)

@app.route('/log-analysis')
def log_analysis_page():
    return render_template('log-analysis.html')

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/performance")
def performance():
    return render_template("performance.html")


app.register_blueprint(solana_bp)
# app.register_blueprint(sell_trade_bp)
app.register_blueprint(manual_sell_trade_bp)
app.register_blueprint(wallet_bp)
app.register_blueprint(monitor_bp)
app.register_blueprint(trade_history_bp)
app.register_blueprint(charts_bp)
app.register_blueprint(autosnipe_bp)

app.register_blueprint(token_price_bp)
app.register_blueprint(user_register_bp)
app.register_blueprint(autosnipe_trade_bp)
app.register_blueprint(live_pricing_bp)
app.register_blueprint(trades_api_bp)
app.register_blueprint(dashboard_api_bp)
app.register_blueprint(log_analysis_bp)

# app.register_blueprint(solana_auto_snipe_bp)

from trades_ws import *
# # 🚀 Import and initialize scheduler AFTER app is ready
# from init_scheduler import create_scheduler
# create_scheduler(app)
# from  autosnipe_sell_script import auto_snipe_auto_sell_schedular
# auto_snipe_auto_sell_schedular(app)
# from long_sell_trade import long_auto_sell_schedular
# long_auto_sell_schedular(app)

# Press the green button in the gutter to run the script.
if __name__ == "__main__":
    # for production
   # socketio.run(app, host="0.0.0.0", port=8000)
    # for development
   # Only run schedulers in the child process
   if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
       from init_scheduler import create_scheduler

       create_scheduler(app)

       from autosnipe_sell_script import auto_snipe_auto_sell_schedular

       auto_snipe_auto_sell_schedular(app)

       from long_sell_trade import long_auto_sell_schedular

       long_auto_sell_schedular(app)

       from autosnipe_buy_new_token import start_sniper_ingestion

       start_sniper_ingestion()

       from yellowstone_cache import start_yellowstone_cache

       start_yellowstone_cache()

       from dashboard_models import start_latency_snapshot_scheduler

       start_latency_snapshot_scheduler(app)
   # debug=False (NOT True) — debug=True wires in Werkzeug's interactive
   # debugger (evalex=True), which lets anyone who can trigger an unhandled
   # error on this publicly-bound (0.0.0.0) server execute arbitrary Python
   # code. use_reloader stays True so WERKZEUG_RUN_MAIN still gets set in the
   # child process, which the scheduler/sniper startup block above depends on.
   socketio.run(app, host="0.0.0.0", port=5000, debug=False, use_reloader=True, allow_unsafe_werkzeug=True)
    # app.run(host="0.0.0.0", port=8000, debug=True)
