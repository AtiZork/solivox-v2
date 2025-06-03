import os


class Config:
    secret_key = "6e3c52cfac7b881f39926c9407be755307c702ff9be1712a786c5c8b05f23cca"  # Required for session management
    # PostgreSQL Configuration
    SQLALCHEMY_DATABASE_URI = "postgresql://solana:solana@localhost/solana_wallets"
    # app.config["SQLALCHEMY_DATABASE_URI"] = "postgresql://solana:solana@db:5432/solana_wallets"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
