from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from solders.pubkey import Pubkey
from settings import solana_client
from werkzeug.security import generate_password_hash, check_password_hash


db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Wallet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)  # ✅ new field
    user = db.relationship('User', backref=db.backref('wallets', lazy=True))  #
    public_key = db.Column(db.String(100), unique=True, nullable=False)
    title = db.Column(db.String(100), nullable=True)
    private_key = db.Column(db.Text, nullable=False)
    balance = db.Column(db.Integer, nullable=False, default=0)

    # def __init__(self, public_key, private_key, balance=0):
    #     self.public_key = public_key
    #     self.private_key = json.dumps(private_key)  # Store as JSON string
    #     self.balance = balance
    def __init__(self, public_key, private_key_path, title, balance=0, user_id=None):
        self.public_key = public_key
        self.title = title
        self.private_key = private_key_path  # Store as JSON string
        self.balance = balance
        self.user_id =user_id

    def get_available_balance(self):
        """Fetch available SOL balance for the wallet."""
        try:
            response = solana_client.get_balance(Pubkey.from_string(self.public_key))
            if "result" in response and "value" in response["result"]:
                # Convert lamports to SOL (1 SOL = 10^9 lamports)
                return response["result"]["value"] / 10 ** 9
        except Exception as e:
            print(f"Error fetching balance for {self.public_key}: {e}")
        return 0.0

    def to_dict(self):
        """Convert wallet object to dictionary."""
        balance = solana_client.get_balance(Pubkey.from_string(self.public_key))
        return {
            "id": self.id,
            "public_key": self.public_key,
            "title": self.title,
            "name": "",
            "balance": balance.value if balance else 0,
        }

class AutoSnipeConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)  # ✅ new field
    user = db.relationship('User', backref=db.backref('autosnipe_configs', lazy=True))
    # Which of the user's wallets this sniper buys with. Nullable: when unset,
    # the buy flow falls back to the user's first wallet (by id) - previously
    # it picked via Wallet.query.filter_by(user_id=...).first() with no
    # explicit order, which Postgres doesn't guarantee is stable, so a user
    # with more than one wallet could have a snipe buy land on the wrong one.
    wallet_id = db.Column(db.Integer, db.ForeignKey('wallet.id'), nullable=True)
    wallet = db.relationship('Wallet', foreign_keys=[wallet_id])
    # New field for Buy Txns > 80 USD
    buy_txns_over_80_usd = db.Column(db.Integer, nullable=False, default=80)
    # Basic fields with defaults
    min_txns = db.Column(db.Integer, nullable=False, default=5)
    launch_delay = db.Column(db.Integer, nullable=False, default=5)
    # Conditional extended scan: if normal window hits >= 50% of min_txns,
    # continue for half_txns_scan_duration additional seconds.
    half_txns_scan_enabled = db.Column(db.Boolean, nullable=False, default=False)
    half_txns_scan_duration = db.Column(db.Integer, nullable=False, default=30)
    buy_amount = db.Column(db.Float, nullable=False, default=1.0)
    # Was 100 (=100% slippage, i.e. accepts any output amount - sandwichable/
    # front-runnable for nearly the whole buy). 20% is a sane sniper default;
    # only applies to new configs, existing saved configs keep their value.
    slippage = db.Column(db.Float, nullable=False, default=20)
    priority_fee = db.Column(db.Float, nullable=False, default=0.01)  # buy priority fee (SOL)
    # Sell priority fee (SOL). Previously there was no way to configure this
    # at all - sells always used Trade.default_gas_fee's column default
    # (0.01), which nothing ever set from the sniper config, so every snipe
    # sell's priority fee was permanently stuck at that one hardcoded value.
    sell_priority_fee = db.Column(db.Float, nullable=False, default=0.01)

    # AutoSell settings with defaults
    drop_cutoff = db.Column(db.Float, default=30)
    drop_cutoff_enabled = db.Column(db.Boolean, nullable=False, default=True)
    drop_until_profit = db.Column(db.Float, default=99)
    drop_after_100 = db.Column(db.Float, default=50)
    drop_after_100_enabled = db.Column(db.Boolean, nullable=False, default=True)
    drop_after_400 = db.Column(db.Float, default=30)
    drop_after_400_enabled = db.Column(db.Boolean, nullable=False, default=True)

    # Sell at target profit % fields with defaults
    sell_at_100 = db.Column(db.Float, default=10)
    sell_at_200 = db.Column(db.Float, default=10)
    sell_at_300 = db.Column(db.Float, default=10)
    sell_at_400 = db.Column(db.Float, default=10)
    sell_at_600 = db.Column(db.Float, default=10)
    sell_at_800 = db.Column(db.Float, default=10)
    sell_at_1000 = db.Column(db.Float, default=10)
    sell_at_1500 = db.Column(db.Float, default=10)
    sell_at_2500 = db.Column(db.Float, default=10)
    sell_at_4000 = db.Column(db.Float, default=10)
    sell_at_10000 = db.Column(db.Float, default=10)
    # Momentum-exit rule: sell a position in full once its token has had no
    # detected trade for this long. Copied onto each Trade at buy time (same
    # pattern as drop_cutoff/drop_cutoff_enabled), so it can vary per sniper
    # config even though the global SNIPER_SELL_INACTIVITY_* settings remain
    # the master on/off switch and fallback default.
    inactivity_sell_enabled = db.Column(db.Boolean, nullable=False, default=True)
    # 5s is an explicit client requirement, not a default to be tuned up.
    inactivity_sell_seconds = db.Column(db.Float, nullable=False, default=5.0)
    # Fail-safe (client request): once a sell decision has fired (take-profit,
    # stop-loss, trailing-stop, inactivity - whichever), if it hasn't sold
    # within this many seconds, force a retry on the next cycle tick instead
    # of waiting out a cooldown left over from an earlier, unrelated failed
    # attempt. Per-config and enable/disable like every other sell rule here,
    # not a fixed global setting - copied onto each Trade at buy time.
    critical_failsafe_enabled = db.Column(db.Boolean, nullable=False, default=True)
    critical_failsafe_seconds = db.Column(db.Float, nullable=False, default=5.0)
    active = db.Column(db.Boolean, nullable=False, default=True)  # New field
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "active": bool(self.active),
            "wallet_id": self.wallet_id,
            "buy_txns_over_80_usd": self.buy_txns_over_80_usd,
            "min_txns": self.min_txns,
            "launch_delay": self.launch_delay,
            "half_txns_scan_enabled": bool(self.half_txns_scan_enabled),
            "half_txns_scan_duration": self.half_txns_scan_duration,
            "buy_amount": self.buy_amount,
            "slippage": self.slippage,
            "priority_fee": self.priority_fee,
            "sell_priority_fee": getattr(self, "sell_priority_fee", 0.01),
            "drop_cutoff": self.drop_cutoff,
            "drop_cutoff_enabled": bool(self.drop_cutoff_enabled),
            "drop_until_profit": self.drop_until_profit,
            "drop_after_100": self.drop_after_100,
            "drop_after_100_enabled": bool(self.drop_after_100_enabled),
            "drop_after_400": self.drop_after_400,
            "drop_after_400_enabled": bool(self.drop_after_400_enabled),
            "sell_at_100": self.sell_at_100,
            "sell_at_200": self.sell_at_200,
            "sell_at_300": getattr(self, "sell_at_300", 10),
            "sell_at_400": self.sell_at_400,
            "sell_at_600": getattr(self, "sell_at_600", 10),
            "sell_at_800": getattr(self, "sell_at_800", 10),
            "sell_at_1000": self.sell_at_1000,
            "sell_at_1500": self.sell_at_1500,
            "sell_at_2500": self.sell_at_2500,
            "sell_at_4000": self.sell_at_4000,
            "sell_at_10000": self.sell_at_10000,
            "inactivity_sell_enabled": bool(getattr(self, "inactivity_sell_enabled", True)),
            "inactivity_sell_seconds": getattr(self, "inactivity_sell_seconds", 5.0),
            "critical_failsafe_enabled": bool(getattr(self, "critical_failsafe_enabled", True)),
            "critical_failsafe_seconds": getattr(self, "critical_failsafe_seconds", 5.0),
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


class TradeConfiguration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    config_data = db.Column(db.JSON, nullable=False, default={})  # Store all fields in JSON

    def __init__(self, name, config_data):
        self.name = name
        self.config_data = config_data


class Trade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    to_pubkey = db.Column(db.String(255), nullable=False)
    # private_key = db.Column(db.Text, nullable=False)  # Consider encryption for security
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)  # ✅ new field
    user = db.relationship('User', backref=db.backref('trades', lazy=True))
    amount = db.Column(db.Float, nullable=False)
    initial_price = db.Column(db.Float, nullable=False)
    token_address = db.Column(db.String(255), nullable=False)
    token_name = db.Column(db.String(100), nullable=True)
    token_symbol = db.Column(db.String(500), nullable=True)
    trade_type = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=db.func.now())
    new_migrated_token = db.Column(db.Boolean, default=False)
    customized_configuration = db.Column(db.Boolean, default=False)
    purchased_token_amount = db.Column(db.Float, default=0.0)
    # Foreign key reference to TradeConfiguration
    config_id = db.Column(db.Integer, db.ForeignKey('trade_configuration.id'), nullable=True)
    config = db.relationship('TradeConfiguration', backref=db.backref('trades', lazy=True))
    # Trading parameters
    sell_20_at_200_percent_profit = db.Column(db.Float, default=3.0)
    sell_10_at_300_percent_profit = db.Column(db.Float, default=4.0)
    sell_10_at_400_percent_profit = db.Column(db.Float, default=5.0)
    sell_20_at_400_percent_profit = db.Column(db.Float, default=5.0)  # Newly added
    sell_30_at_150_percent_profit = db.Column(db.Float, default=2.5)  # Newly added
    sell_20_at_1000_percent_profit = db.Column(db.Float, default=11.0)
    sell_75_at_1500_percent_profit = db.Column(db.Float, default=16.0)
    sell_all_if_profit_drops_33_percent = db.Column(db.Float, default=0.67)
    rebuy_150_percent_after_sell = db.Column(db.Float, default=1.5)
    rebuy_window = db.Column(db.Integer, default=90.0)
    sell_within_seconds = db.Column(db.Integer, default=45.0)
    executed = db.Column(db.Boolean, default=False)
    # rad gas fee/priority fee
    default_gas_fee = db.Column(db.Float, default=0.01)  # buy gas fee
    default_priority_fee = db.Column(db.Float, default=0.01)  # buy priority fee
    rad_slippage = db.Column(db.Float, default=0.50)  # buy slippage
    rad_sell_slippage = db.Column(db.Float, default=0.50)  # sell slippage
    rad_sell_gas_fee = db.Column(db.Float, default=0.01)  # sell gas fee

    # LONG Auto-sell conditions
    sell_100_at_30_percent_drop = db.Column(db.Float, default=0.70)
    sell_100_after_100_percent_profit_drop = db.Column(db.Float, default=30.00)  # Sell 100% after 100% profit drop
    sell_at_200_percent_profit = db.Column(db.Float, default=30.00)
    sell_at_300_percent_profit = db.Column(db.Float, default=30.00)
    sell_at_500_percent_profit = db.Column(db.Float, default=10.00)
    sell_at_1000_percent_profit = db.Column(db.Float, default=10.00)
    sell_at_2000_percent_profit = db.Column(db.Float, default=10.00)
    sell_at_10000_percent_profit = db.Column(db.Float, default=10.00)
    # long gas fee/ slippage - percent (e.g. 20 = 20%), matching the UI's
    # slippage inputs. Was 0.10 (= 0.10%), which converted to 0 bps wherever
    # slippageBps was built directly from this column.
    long_slippage = db.Column(db.Float, default=20)  # buy slippage
    long_sell_slippage = db.Column(db.Float, default=20)
    long_buy_gas_fee = db.Column(db.Float, default=0.01)  # long buy gas fee
    long_sell_gas_fee = db.Column(db.Float, default=0.01)  # sell gas fee

    # LONG Buy conditions
    buy_now = db.Column(db.Boolean, default=False)  # Newly added
    buy_token_if_price = db.Column(db.Boolean, default=False)  # Newly added
    auto_sell = db.Column(db.Boolean, default=False)  # Newly added
    buy_if_price_up = db.Column(db.Numeric(18, 6))  # Buy if price increases to this level
    buy_if_price_down = db.Column(db.Numeric(18, 6))  # Buy if price drops to this level

    # autosnipe settings
    auto_snipe = db.Column(db.Boolean, default=False, nullable=False)  # ✅ new field
    trade_kind = db.Column(db.String(20), nullable=False, default="LONG")  # <-- NEW FIELD: "LONG" or "AUTOSNIPE"
    # Percent (e.g. 20 = 20%), same unit as AutoSnipeConfig.slippage. Was
    # 0.30 (= 0.30%, converts to ~0 bps wherever this is read as a percent).
    autosnipe_sell_slippage = db.Column(db.Float, default=20)
    drop_cutoff = db.Column(db.Float, default=30)
    drop_cutoff_enabled = db.Column(db.Boolean, nullable=False, default=True)
    drop_until_profit = db.Column(db.Float, default=99)
    drop_after_100 = db.Column(db.Float, default=50)
    drop_after_100_enabled = db.Column(db.Boolean, nullable=False, default=True)
    drop_after_400 = db.Column(db.Float, default=30)
    drop_after_400_enabled = db.Column(db.Boolean, nullable=False, default=True)
    sell_at_100 = db.Column(db.Float, default=10)
    sell_at_200 = db.Column(db.Float, default=10)
    sell_at_300 = db.Column(db.Float, default=10)
    sell_at_400 = db.Column(db.Float, default=10)
    sell_at_600 = db.Column(db.Float, default=10)
    sell_at_800 = db.Column(db.Float, default=10)
    sell_at_1000 = db.Column(db.Float, default=10)
    sell_at_1500 = db.Column(db.Float, default=10)
    sell_at_2500 = db.Column(db.Float, default=10)
    sell_at_4000 = db.Column(db.Float, default=10)
    sell_at_10000 = db.Column(db.Float, default=10)
    # Highest take-profit tier (price multiplier, e.g. 2.0 = 100% profit)
    # already sold for this trade; 0 = none yet.
    last_tp_tier = db.Column(db.Float, nullable=False, default=0)
    # Token amount at buy time; take-profit tier % are shares of this.
    initial_token_amount = db.Column(db.Float, nullable=True)
    # Cumulative % of initial_token_amount already sold by take-profit tiers.
    tp_pct_sold = db.Column(db.Float, nullable=False, default=0)
    # Highest price seen since entry, persisted so the trailing-stop rules
    # (drop_after_100/400) survive a restart and still evaluate correctly
    # once the price has pulled back under the 2x/5x that originally armed
    # them. 0/None = no peak recorded yet (falls back to initial_price).
    peak_price = db.Column(db.Float, nullable=True)
    # Momentum-exit rule: sell this position in full once its token has had
    # no detected trade for this long. Captured from AutoSnipeConfig at buy
    # time, same as drop_cutoff/drop_cutoff_enabled above.
    inactivity_sell_enabled = db.Column(db.Boolean, nullable=False, default=True)
    inactivity_sell_seconds = db.Column(db.Float, nullable=False, default=5.0)
    # Captured from AutoSnipeConfig at buy time, same as inactivity_sell_*
    # above - see AutoSnipeConfig.critical_failsafe_enabled's own comment.
    critical_failsafe_enabled = db.Column(db.Boolean, nullable=False, default=True)
    critical_failsafe_seconds = db.Column(db.Float, nullable=False, default=5.0)

    # Durable "a sell send was attempted but the DB write recording it failed"
    # marker (action-plan 1.4b). Set True right before send_transaction, in
    # its own small commit, and cleared again on whichever commit records the
    # outcome (success, or the send itself being rejected). It stays True
    # only when a send actually succeeded but the bigger TradeHistory/
    # bookkeeping commit right after it failed - exactly the window where a
    # blind retry would risk sending a second real transaction for tokens
    # already sold. A periodic reconcile pass (not the sell-decision cycle)
    # resolves it against the wallet's real on-chain balance.
    sell_pending = db.Column(db.Boolean, nullable=False, default=False)
    sell_pending_since = db.Column(db.DateTime, nullable=True)

    # Backs the B10 buy-dedup check (autosnipe_buy_new_token.py) with a real
    # DB constraint (action-plan 1.3) instead of relying only on a query
    # check + WALLET_BUY_LOCK serialization. Scoped to open positions
    # (executed=False), not "ever" - closed out of a mint, the bot is
    # allowed to buy it again later. partial/PostgreSQL-only: fine here,
    # the app already requires Postgres (see config.py).
    __table_args__ = (
        db.Index(
            "ix_trade_open_autosnipe_user_mint", "user_id", "token_address",
            unique=True,
            postgresql_where=db.and_(auto_snipe == True, executed == False),  # noqa: E712
        ),
    )

    def __repr__(self):
        return f"<Trade {self.id} - {self.to_pubkey}>"


class TokenPrice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # Foreign key to Trade
    trade_id = db.Column(db.Integer, db.ForeignKey('trade.id', ondelete='CASCADE'), nullable=False)
    trade = db.relationship('Trade', backref=db.backref('token_prices', lazy=True, cascade='all, delete-orphan'))
    token_address = db.Column(db.String(150), nullable=True)  # ✅ New field
    token_name = db.Column(db.String(100), nullable=True)
    symbol = db.Column(db.String(50), nullable=True)
    price = db.Column(db.Numeric, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<TokenPrice {self.symbol} @ {self.timestamp} for Trade {self.trade_id}>"


class PresignedTrade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token_address = db.Column(db.String(100), nullable=False)
    to_pubkey = db.Column(db.String(255), nullable=True)
    amount = db.Column(db.Float, nullable=False)
    initial_price = db.Column(db.Float, nullable=False)

    # Foreign key reference to TradeConfiguration
    config_id = db.Column(db.Integer, db.ForeignKey('trade_configuration.id'), nullable=False)
    config = db.relationship('TradeConfiguration', backref=db.backref('presigned_trades', lazy=True))
    estimated_amount = db.Column(db.Float, nullable=False)
    signed_transaction = db.Column(db.Text, nullable=False)  # Store the Base64 encoded transaction
    live_time = db.Column(db.Integer, nullable=False)  # UNIX timestamp for execution
    executed = db.Column(db.Boolean, default=False)  # Track if trade was executed

    # New parameters
    sell_100_at_30_percent_drop = db.Column(db.Float, default=0.70)
    sell_20_at_200_percent_profit = db.Column(db.Float, default=3.0)
    sell_10_at_300_percent_profit = db.Column(db.Float, default=4.0)
    sell_10_at_400_percent_profit = db.Column(db.Float, default=5.0)
    sell_20_at_1000_percent_profit = db.Column(db.Float, default=11.0)
    sell_75_at_1500_percent_profit = db.Column(db.Float, default=16.0)
    sell_all_if_profit_drops_33_percent = db.Column(db.Float, default=0.67)
    rebuy_150_percent_after_sell = db.Column(db.Float, default=1.5)
    rebuy_window = db.Column(db.Float, default=90.0)
    sell_within_seconds = db.Column(db.Float, default=45.0)
    default_gas_fee = db.Column(db.Float, default=0.01)
    default_priority_fee = db.Column(db.Float, default=0.01)
    rad_slippage = db.Column(db.Float, default=0.50)
    rad_sell_slippage = db.Column(db.Float, default=0.50)  # sell slippage
    rad_sell_gas_fee = db.Column(db.Float, default=0.01)  # sell gas fee

    # Timestamp columns
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class TradeHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # ✅ Correctly define trade_id as a foreign key
    trade_id = db.Column(db.Integer, db.ForeignKey('trade.id', ondelete="CASCADE"), nullable=True)

    # ✅ Establish relationship for easier querying
    trade = db.relationship('Trade', backref=db.backref('history', lazy=True, cascade="all, delete-orphan"))
    token_address = db.Column(db.String(255), nullable=False)
    trade_type = db.Column(db.String(10), nullable=False)  # "BUY" or "SELL"
    trade_kind = db.Column(db.String(20), nullable=False, default="LONG")  # <-- NEW FIELD: "LONG" or "AUTOSNIPE"
    amount = db.Column(db.Float, nullable=False)
    execution_price = db.Column(db.Float, nullable=False)
    tx_id = db.Column(db.String(255), nullable=False)  # Transaction ID
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    # "sent" (submitted, not yet confirmed) -> "confirmed" or "failed"/"failed_timeout".
    # A send_transaction response only means the RPC node accepted the tx for
    # broadcast, not that it landed; the reconciliation job
    # (autosnipe_sell_script.reconcile_pending_sells) resolves "sent" rows and
    # restores the position on a failure. Existing rows predate this tracking
    # and are assumed settled, hence the "confirmed" default.
    status = db.Column(db.String(20), nullable=False, default="confirmed")


# Create Log Model
class TradeLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    level = db.Column(db.String(10))  # INFO, WARNING, ERROR
    message = db.Column(db.Text)

    def to_dict(self):
        return {"timestamp": self.timestamp, "level": self.level, "message": self.message}