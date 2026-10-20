import os


class Config:
    # Was lowercase `secret_key`, which Flask's `from_object` ignores (only
    # all-uppercase attributes are imported), so `app.secret_key` was never
    # actually set. Renamed to the name Flask looks for, and no fallback
    # value is committed - set SECRET_KEY in a local .env (see .env.example).
    SECRET_KEY = os.getenv("SECRET_KEY", "")
    # PostgreSQL Configuration
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL", "postgresql://solana:solana@localhost/solana_wallets"
    )
    # app.config["SQLALCHEMY_DATABASE_URI"] = "postgresql://solana:solana@db:5432/solana_wallets"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Default pool (5 + 10 overflow = 15 connections) is exhausted under
    # current trade volume: the sell cycle, buy/sell reconciliation jobs,
    # new-token callback, and price/sniper WebSocket listeners all compete
    # for connections from the same pool. Exhaustion was observed directly
    # in production - "QueuePool limit of size 5 overflow 10 reached" on
    # multiple components at once, including the sell-decision job getting
    # stuck long enough that every subsequent 1s-interval run was skipped
    # for over a minute straight.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_size": 20,
        "max_overflow": 30,
        "pool_timeout": 30,
        "pool_recycle": 1800,
    }
