import secrets

from flask_jwt_extended import get_jwt_identity, jwt_required

from models import db, Wallet
from flask import request, jsonify, session, Blueprint
from mnemonic import Mnemonic
import os
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins
from settings import *
from solders.keypair import Keypair
from solders.pubkey import Pubkey

wallet_bp = Blueprint('wallet_bp', __name__)


@wallet_bp.route('/create_wallet', methods=['POST'])
@jwt_required()
def create_wallet():
    """Create a new Solana wallet and return mnemonic, private key file path, and public key."""
    try:
        user_id = get_jwt_identity()
        title = request.json['title']
        # Generate a new mnemonic phrase (12 words)
        mnemonic = Mnemonic("english")
        recovery_phrase = mnemonic.generate(strength=128)  # 128 bits = 12 words

        # Convert the mnemonic to a seed
        seed = mnemonic.to_seed(recovery_phrase, passphrase="")  # Optional passphrase

        # Derive the keypair from the seed
        keypair = Keypair.from_seed(seed[:32])  # ✅ Correct method

        # Get the private key and public key
        private_key = keypair.secret()  # Use secret_key instead of to_bytes_array()
        public_key = keypair.pubkey()  # Use public_key instead of pubkey()

        if not os.path.exists(secure_directory):
            os.makedirs(secure_directory, mode=0o700)  # Make directory with restricted permissions

        # Generate a unique filename for the private key
        private_key_filename = f"{secrets.token_hex(16)}.key"  # Generates a random 16-byte filename
        private_key_path = os.path.join(secure_directory, private_key_filename)

        # Store the private key in a file
        with open(private_key_path, 'wb') as key_file:
            key_file.write(private_key)

        # Store the path in the database instead of the private key
        new_wallet = Wallet(public_key=str(public_key), private_key_path=private_key_path, user_id=user_id, title=title)
        db.session.add(new_wallet)
        db.session.commit()

        return jsonify({
            "status": "success",
            "recovery_phrase": recovery_phrase,  # Store securely!
            "private_key_path": private_key_path,  # Return path of private key file
            "public_key": str(public_key),
            "user":user_id,
            "message": "wallet created successfully"
        }), 200

    except Exception as e:
        return jsonify({"status": "failed", "message": f"Error creating wallet: {str(e)}"}), 500


# Route to fetch all wallets with balance
@wallet_bp.route('/get_wallets', methods=['GET'])
@jwt_required()
def get_wallets():
    try:
        user_id = get_jwt_identity()
        # Fetch all wallets
        # wallets = Wallet.query.all()
        wallets = Wallet.query.filter_by(user_id=user_id).all()


        # Convert wallets to list of dictionaries
        wallets_list = [wallet.to_dict() for wallet in wallets]

        # Return the wallets data in the response
        return jsonify({"wallets": wallets_list}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@wallet_bp.route('/check_balance', methods=['POST'])
def check_balance():
    public_key = request.json['public_key']
    balance = solana_client.get_balance(Pubkey.from_string(public_key))
    return jsonify({'balance': balance.value})


"""
add amount in wallet
"""


@wallet_bp.route("/fund_wallet", methods=["POST"])
def fund_wallet():
    """Funds a wallet using Solana's testnet airdrop with dynamic SOL amount."""
    try:
        data = request.get_json()
        public_key = data.get("public_key")
        amount = data.get("amount")

        if not public_key:
            return jsonify({"error": "Public key is required"}), 400

        if not amount or amount <= 0:
            return jsonify({"error": "Valid amount is required"}), 400

        # Convert the amount (SOL) to lamports (1 SOL = 1,000,000,000 lamports)
        lamports = int(amount * 1_000_000_000)

        pubkey = (Pubkey.from_string(public_key))
        response = solana_client.request_airdrop(pubkey, lamports)

        if "result" in response:
            existing_wallet = Wallet.query.filter_by(public_key=public_key).first()
            if existing_wallet:
                existing_wallet.balance += amount
                db.session.commit()
            return jsonify({"message": "Fund added successfully", "transaction_id": response["result"]})
        else:
            return jsonify({"error": "Airdrop failed", "details": response}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def wallet_exists_on_solana(public_key_str):
    """Check if a wallet exists on the Solana blockchain."""
    try:
        # public_key = PublicKey(public_key_str)
        public_key =(Pubkey.from_string(public_key_str))

        # Get account info
        response = solana_client.get_account_info(public_key)

        return response.value is not None  # Account exists if response has value
    except Exception as e:
        print(f"Error checking wallet existence: {str(e)}")
        return False  # Assume wallet doesn't exist if RPC fails


def get_private_key_from_mnemonic(mnemonic_phrase):
    """Convert a mnemonic phrase to a private key."""
    try:
        # Generate seed from mnemonic
        seed_bytes = Bip39SeedGenerator(mnemonic_phrase).Generate()

        # Derive the correct Solana private key
        bip44 = Bip44.FromSeed(seed_bytes, Bip44Coins.SOLANA).DeriveDefaultPath()
        private_key_bytes = bip44.PrivateKey().Raw().ToBytes()

        return private_key_bytes
    except Exception as e:
        raise ValueError(f"Invalid mnemonic: {str(e)}")


"""
attach wallet
"""


@wallet_bp.route('/attach_wallet', methods=['POST'])
@jwt_required()
def attach_wallet():
    """Attach a wallet, store the private key securely, and save in the database."""
    try:
        user_id = get_jwt_identity()
        data = request.json

        if not data.get("private_key") or not data.get("public_key"):
            return jsonify({"status": "failed", "message": "Missing private_key or public_key"}), 400

        private_key_str = data["private_key"]
        public_key_str = data["public_key"]

        # Convert private key (supports both mnemonic and hex formats)
        try:
            if " " in private_key_str:  # If it's a mnemonic phrase
                private_key_bytes = get_private_key_from_mnemonic(private_key_str)
            else:
                private_key_bytes = bytes.fromhex(private_key_str)  # If hex, use directly
        except Exception:
            return jsonify({"status": "failed", "message": "Invalid private key or mnemonic format"}), 400

        # Validate public key format
        try:
            public_key = (Pubkey.from_string(public_key_str))
        except Exception:
            return jsonify({"status": "failed", "message": "Invalid public key format"}), 400

        # Recreate the keypair from the private key
        keypair = Keypair.from_seed(private_key_bytes)

        # Ensure derived public key matches provided one
        if str(keypair.pubkey()) != public_key_str:
            return jsonify({"status": "failed", "message": "Provided private key does not match the public key"}), 400

        # Check if wallet exists on Solana mainnet
        if not wallet_exists_on_solana(public_key_str):
            return jsonify({"status": "failed", "message": "Wallet does not exist on Solana"}), 400

        # Check if the wallet already exists in the database
        # existing_wallet = Wallet.query.filter_by(public_key=public_key_str).first()
        # if existing_wallet:
        #     return jsonify({"status": "failed", "message": "Wallet already exists"}), 400

        # Generate a random filename for private key storage
        private_key_filename = f"{secrets.token_hex(16)}.key"
        private_key_path = os.path.join(secure_directory, private_key_filename)

        # Store the private key securely in a file
        with open(private_key_path, 'wb') as key_file:
            key_file.write(private_key_bytes)

        # Set file permissions to read/write only for the owner
        os.chmod(private_key_path, 0o600)

        # Save wallet to database
        new_wallet = Wallet(public_key=public_key_str, private_key_path=private_key_path, user_id=int(user_id), title="wallet")
        db.session.add(new_wallet)
        db.session.commit()

        return jsonify({
            "status": "success",
            "message": "Wallet attached successfully",
            "public_key": public_key_str,
            "private_key_path": private_key_path  # Only return path, not the actual key
        }), 200

    except Exception as e:
        return jsonify({"status": "failed", "message": f"Error attaching wallet: {str(e)}"}), 500


@wallet_bp.route('/recover_wallet', methods=['POST'])
def recover_wallet():
    """Recover wallet using recovery phrase, store private key securely in a file, and save details in the database."""
    try:
        data = request.json

        # Check if the recovery phrase is provided
        if not data.get("recovery_phrase"):
            return jsonify({"status": "failed", "message": "Missing recovery_phrase"}), 400

        recovery_phrase = data["recovery_phrase"]

        # Validate the recovery phrase
        if len(recovery_phrase.split()) not in [12, 24]:
            return jsonify(
                {"status": "failed", "message": "Invalid recovery phrase length (must be 12 or 24 words)"}), 400

        try:
            # Use the Mnemonic library to convert the recovery phrase to a seed
            mnemonic = Mnemonic("english")
            if not mnemonic.check(recovery_phrase):
                return jsonify({"status": "failed", "message": "Invalid recovery phrase"}), 400

            # Convert private key (supports both mnemonic and hex formats)
            try:
                if " " in recovery_phrase:  # If it's a mnemonic phrase
                    private_key_bytes = get_private_key_from_mnemonic(recovery_phrase)
                else:
                    private_key_bytes = bytes.fromhex(recovery_phrase)  # If hex, use directly
            except Exception:
                return jsonify({"status": "failed", "message": "Invalid private key or mnemonic format"}), 400

            keypair = Keypair.from_seed(private_key_bytes)

            # # Generate the seed from the recovery phrase
            # seed = mnemonic.to_seed(recovery_phrase, passphrase="")  # Passphrase is optional
            #
            # # Derive the private key (using bip44 for example, you can adjust the derivation path)
            # keypair = Keypair.from_seed(seed[:32])  # Using the first 32 bytes for the private key

            # Get the public key from the keypair
            public_key_str = str(keypair.pubkey())
            private_key_bytes = keypair.secret()

        except Exception as e:
            return jsonify({"status": "failed", "message": f"Error recovering wallet: {str(e)}"}), 400

        # Check if the wallet already exists
        existing_wallet = Wallet.query.filter_by(public_key=public_key_str).first()
        if existing_wallet:
            return jsonify({"status": "failed", "message": "Wallet already exists"}), 400

        # Generate a random filename for the private key
        private_key_filename = f"{secrets.token_hex(16)}.key"
        private_key_path = os.path.join(secure_directory, private_key_filename)

        # Store the private key securely in a file
        with open(private_key_path, 'wb') as key_file:
            key_file.write(private_key_bytes)

        # Set file permissions to read/write only for the owner
        os.chmod(private_key_path, 0o600)

        # Store the public key and private key file path in the database
        new_wallet = Wallet(public_key=public_key_str, private_key_path=private_key_path, title="")
        db.session.add(new_wallet)
        db.session.commit()

        return jsonify({
            "status": "success",
            "message": "Wallet recovered and attached successfully",
            "public_key": public_key_str,
            "private_key_path": private_key_path  # Only store the path, not the key itself
        }), 200

    except Exception as e:
        return jsonify({"status": "failed", "message": f"Error recovering wallet: {str(e)}"}), 500


""""
verify wallet attached
"""


@wallet_bp.route('/is_wallet_attached', methods=['GET'])
def is_wallet_attached():
    public_key = session.get('public_key')
    if public_key:
        return jsonify({"wallet_attached": True, "public_key": public_key})
    else:
        return jsonify({"wallet_attached": False, "message": "No wallet attached"})