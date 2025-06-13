from flask import  request, jsonify, Blueprint
from models import User, db
from flask_jwt_extended import (
     create_access_token, jwt_required, get_jwt_identity
)
import datetime

user_register_bp = Blueprint('user_register_bp', __name__)


@user_register_bp.route('/register', methods=['POST'])
def register():
    try:
        data = request.json
        if User.query.filter_by(username=data['username']).first():
            return jsonify({"msg": "Username already exists"}), 400
        user = User(username=data['username'], email=data.get('email'))
        user.set_password(data['password'])
        db.session.add(user)
        db.session.commit()
        return jsonify({"msg": "User registered successfully"}), 201
    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"message": str(e)}), 500

@user_register_bp.route('/login', methods=['POST'])
def login():
    try:
        data = request.json
        user = User.query.filter_by(username=data['username']).first()
        if not user or not user.check_password(data['password']):
            return jsonify({"msg": "wrong username or password"}), 401
        expires = datetime.timedelta(days=1)
        access_token = create_access_token(identity=str(user.id), expires_delta=expires)
        print(access_token)
        return jsonify(access_token=access_token), 200
    except Exception as e:
        print(f"Error during login: {e}")
        return jsonify({"message":e}), 500

@user_register_bp.route('/user', methods=['GET'])
@jwt_required()
def user():
    try:
        user_id = get_jwt_identity()
        user = User.query.get(user_id)
        if not user:
            return jsonify({"msg": "User not found"}), 404
        return jsonify({
            "username": user.username,
            "email": user.email
        })
    except Exception as e:
        print(f"Error fetching user data: {e}")
        return jsonify({"msg": "Internal server error"}), 500

