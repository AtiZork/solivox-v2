from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from solders.pubkey import Pubkey
from settings import solana_client
from werkzeug.security import generate_password_hash, check_password_hash


db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(120), nullable=False)
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
            "public_key": self.public_key,
            "title": self.title,
            "name": "",
            "balance": balance.value if balance else 0,
        }

class AutoSnipeConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)  # ✅ new field
    user = db.relationship('User', backref=db.backref('autosnipe_configs', lazy=True))
    # New field for Buy Txns > 80 USD
    buy_txns_over_80_usd = db.Column(db.Integer, nullable=False, default=80)
    # Basic fields with defaults
    min_txns = db.Column(db.Integer, nullable=False, default=5)
    launch_delay = db.Column(db.Integer, nullable=False, default=5)
    buy_amount = db.Column(db.Float, nullable=False, default=1.0)
    slippage = db.Column(db.Float, nullable=False, default=100)
    priority_fee = db.Column(db.Float, nullable=False, default=0.01)

    # AutoSell settings with defaults
    drop_cutoff = db.Column(db.Float, default=30)
    drop_until_profit = db.Column(db.Float, default=99)
    drop_after_100 = db.Column(db.Float, default=50)
    drop_after_400 = db.Column(db.Float, default=30)

    # Sell at target profit % fields with defaults
    sell_at_200 = db.Column(db.Float, default=10)
    sell_at_400 = db.Column(db.Float, default=10)
    sell_at_1000 = db.Column(db.Float, default=10)
    sell_at_1500 = db.Column(db.Float, default=10)
    sell_at_2500 = db.Column(db.Float, default=10)
    sell_at_4000 = db.Column(db.Float, default=10)
    sell_at_10000 = db.Column(db.Float, default=10)
    active = db.Column(db.Boolean, nullable=False, default=True)  # New field
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)



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
    config_id = db.Column(db.Integer, db.ForeignKey('trade_configuration.id'), nullable=False)
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
    # long gas fee/ slippage
    long_slippage = db.Column(db.Float, default=0.10)  # buy slippage
    long_sell_slippage = db.Column(db.Float, default=0.10)
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
    autosnipe_sell_slippage = db.Column(db.Float, default=0.30)
    drop_cutoff = db.Column(db.Float, default=30)
    drop_until_profit = db.Column(db.Float, default=99)
    drop_after_100 = db.Column(db.Float, default=50)
    drop_after_400 = db.Column(db.Float, default=30)
    sell_at_200 = db.Column(db.Float, default=10)
    sell_at_400 = db.Column(db.Float, default=10)
    sell_at_1000 = db.Column(db.Float, default=10)
    sell_at_1500 = db.Column(db.Float, default=10)
    sell_at_2500 = db.Column(db.Float, default=10)
    sell_at_4000 = db.Column(db.Float, default=10)
    sell_at_10000 = db.Column(db.Float, default=10)


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


# Create Log Model
class TradeLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    level = db.Column(db.String(10))  # INFO, WARNING, ERROR
    message = db.Column(db.Text)

    def to_dict(self):
        return {"timestamp": self.timestamp, "level": self.level, "message": self.message}