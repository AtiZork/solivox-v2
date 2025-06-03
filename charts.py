import json

from flask import Flask, render_template, request, abort, jsonify
import requests
from datetime import datetime
import time
import pandas as pd
import plotly.graph_objs as go
from plotly.offline import plot
from flask import Blueprint

charts_bp = Blueprint('charts_bp', __name__)

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/pairs/{chain}/{address}"


@charts_bp.route('/')
def index():
    return """
      <h1>DEX Chart</h1>
      <form action="/chart">
        Chain (e.g. ethereum, bsc, solana): <input name="chain" /><br/>
        Pair address (contract or pool ID): <input name="address" /><br/>
        <button>Show Chart</button>
      </form>
    """


@charts_bp.route('/chart')
def chart():
    chain = request.args.get('chain')
    address = request.args.get('address')
    if not chain or not address:
        return abort(400, "Missing `chain` or `address` query parameters")

    # 1) Fetch data from DEXScreener
    url = DEXSCREENER_URL.format(chain=chain, address=address)
    resp = requests.get(url)
    if resp.status_code != 200:
        return abort(502, "Failed to fetch from DEXScreener")
    data = resp.json()

    # 2) Extract historical candles
    #    (the endpoint returns a `pair` object with a `priceHistory` array)
    history = data['pair'].get('priceHistory') or []
    if not history:
        return "<p>No historical data available for this pair.</p>"

    # 3) Build a DataFrame
    df = pd.DataFrame(history)
    df['time'] = pd.to_datetime(df['time'], unit='ms')

    # 4) Create Plotly candlestick
    fig = go.Figure(
        data=[go.Candlestick(
            x=df['time'],
            open=df['open'],
            high=df['high'],
            low=df['low'],
            close=df['close'],
        )]
    )
    fig.update_layout(
        title=f"{data['pair']['baseToken']['symbol']}/{data['pair']['quoteToken']['symbol']} on {chain}",
        xaxis_title="Time",
        yaxis_title=f"Price ({data['pair']['quoteToken']['symbol']})",
        xaxis_rangeslider_visible=False,
        template="plotly_dark"
    )

    # 5) Generate the HTML <div> containing the chart
    chart_div = plot(fig, output_type='div', include_plotlyjs=True)

    return render_template('chart.html', chart_div=chart_div)


OFFSETS = [
    ('1s',  1),
    ('30s', 30),
    ('1m',  60),
    ('15m', 15 * 60),
    ('1h',  60 * 60),
    ('1d',  24 * 60 * 60),
]

@charts_bp.route('/api/token/<string:address>/prices')
def token_prices(address):
    """
    Always returns JSON with six fixed buckets:
      labels: ["1s","30s","1m","15m","1h","1d"]
      prices: [price_1s_ago, price_30s_ago, …, price_24h_ago]
    """
    now = time.time()

    # 1) fetch current price once
    try:
        r0 = requests.get(
            "https://api.coingecko.com/api/v3/simple/token_price/solana",
            params={"contract_addresses": address, "vs_currencies": "usd"},
            timeout=3
        )
        r0.raise_for_status()
        current_usd = r0.json().get(address.lower(), {}).get("usd", None)
        payload = json.loads(r0.text)
    except Exception:
        current_usd = None

    # 2) fetch full past-day minute data
    url = f"https://api.coingecko.com/api/v3/coins/solana/contract/{address}/market_chart"
    params = {"vs_currency": "usd", "days": 1, "interval": "minutely"}
    try:
        r = requests.get(url, params=params, timeout=5)
        r.raise_for_status()
        history = r.json().get("prices", [])  # [ [ts_ms, price], … ]
    except Exception:
        history = []

    # build a helper to grab the closest point
    def get_price(seconds_ago):
        target = now - seconds_ago
        # scan from most recent backward:
        for ts_ms, price in reversed(history):
            if ts_ms / 1000 <= target:
                return price
        # fallback to current if history is empty or too sparse:
        return current_usd

    labels = [lbl for lbl, _ in OFFSETS]
    prices = []

    for lbl, sec in OFFSETS:
        if sec == 1:
            # 1-second bucket → current price
            prices.append(current_usd)
        else:
            prices.append(get_price(sec))

    return jsonify(labels=labels, prices=prices)