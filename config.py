import os
from dotenv import load_dotenv

# Loads .env directly rather than relying on another module (settings.py)
# being imported first and loading it as a side effect - that worked by
# accident via main.py's current import order, but would silently break
# startup here if that order ever changed.
load_dotenv()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Add it to your local .env (see .env.example) - "
            "a hardcoded value used to be committed here."
        )
    return value


class Config:
    # Was lowercase `secret_key`, which Flask's `from_object` ignores (only
    # all-uppercase attributes are imported), so `app.secret_key` was never
    # actually set. Renamed to the name Flask looks for, and no fallback
    # value is committed - set SECRET_KEY in a local .env (see .env.example).
    SECRET_KEY = os.getenv("SECRET_KEY", "")
    # PostgreSQL Configuration. Was a hardcoded
    # "postgresql://solana:solana@localhost/solana_wallets" fallback - same
    # pattern as the JWT secret (main.py) and the Shyft/Moralis/Solscan keys
    # (settings.py) before those were moved to env-only. DATABASE_URL is
    # already set in .env, so this fails loud rather than silently.
    SQLALCHEMY_DATABASE_URI = _require_env("DATABASE_URL")
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
