import os


class Config:
    secret_key = "6e3c52cfac7b881f39926c9407be755307c702ff9be1712a786c5c8b05f23cca"  # Required for session management
    # PostgreSQL Configuration
    SQLALCHEMY_DATABASE_URI = "postgresql://solana:solana@localhost/solana_wallets"
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
