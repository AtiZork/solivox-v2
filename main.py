from flask_jwt_extended import JWTManager

from autosnipe import autosnipe_bp
from autosnipe_buy_new_token import autosnipe_trade_bp
from autosnipe_logic import start_background_jobs
from charts import charts_bp
from long_sell_trade import sell_trade_bp
from models import db, TradeLog
from flask import Flask, render_template
from flask_cors import CORS
from config import Config
from monitor import monitor_bp
from token_price import  token_price_bp
from trade import solana_bp
from user_register import user_register_bp
from wallet_managment import wallet_bp
from menual_sell_token import manual_sell_trade_bp
from flask_socketio import SocketIO
from trade_history import trade_history_bp
# for live
app = Flask(__name__, static_folder='/home/ubuntu/solivox-v2/static')
# for development
# app = Flask(__name__)

# for development
socketio = SocketIO(app, cors_allowed_origins="*")
# for production
# socketio = SocketIO(app, async_mode="eventlet")
app.config['JWT_SECRET_KEY'] = 'kHadk1-fmayaXHlx3PmEdS_NKMAsqPsVNa6c-QzPgic'  # change to secure key
jwt = JWTManager(app)

CORS(app)  # This allows all origins
app.config.from_object(Config)
db.init_app(app)

with app.app_context():
    db.create_all()


def get_logs_from_db():
    return TradeLog.query.order_by(TradeLog.timestamp.desc()).all()


@app.route('/')
def index():
    return render_template('base.html')

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

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


app.register_blueprint(solana_bp)
app.register_blueprint(sell_trade_bp)
app.register_blueprint(manual_sell_trade_bp)
app.register_blueprint(wallet_bp)
app.register_blueprint(monitor_bp)
app.register_blueprint(trade_history_bp)
app.register_blueprint(charts_bp)
app.register_blueprint(autosnipe_bp)

app.register_blueprint(token_price_bp)
app.register_blueprint(user_register_bp)
app.register_blueprint(autosnipe_trade_bp)

# app.register_blueprint(solana_auto_snipe_bp)

from trades_ws import *
# 🚀 Import and initialize scheduler AFTER app is ready
from init_scheduler import create_scheduler
create_scheduler(app)
from  autosnipe_sell_script import auto_snipe_auto_sell_schedular
auto_snipe_auto_sell_schedular(app)
from  autosnipe_buy_script import  auto_buy_token
auto_buy_token(app)

# Press the green button in the gutter to run the script.
if __name__ == "__main__":
   # start_background_jobs()
    # for production
   # socketio.run(app, host="0.0.0.0", port=8000)
    # for development
   socketio.run(app, host="0.0.0.0", port=8000, debug=True, allow_unsafe_werkzeug=True)
    # app.run(host="0.0.0.0", port=8000, debug=True)
