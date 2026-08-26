from flask import request, jsonify
import jwt
import os
from functools import wraps

SECRET_KEY = os.getenv('SECRET_KEY', 'wildfire-secret-key-change-in-production')


def _decode_token():
    """Return (payload, error_response). One of them is None."""
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
    elif request.args.get('token'):
        token = request.args.get('token')
    else:
        return None, (jsonify({'message': 'Token missing.'}), 401)
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        return payload, None
    except jwt.ExpiredSignatureError:
        return None, (jsonify({'message': 'Token expired. Please log in again.'}), 401)
    except jwt.InvalidTokenError:
        return None, (jsonify({'message': 'Invalid token.'}), 401)


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        payload, err = _decode_token()
        if err:
            return err
        request.current_user = payload
        request._jwt_user_id = payload.get('user_id')
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Like token_required, but also enforces is_admin == True."""
    @wraps(f)
    def decorated(*args, **kwargs):
        payload, err = _decode_token()
        if err:
            return err
        if not payload.get('is_admin'):
            return jsonify({'message': 'Admin access required.'}), 403
        request.current_user = payload
        request._jwt_user_id = payload.get('user_id')
        return f(*args, **kwargs)
    return decorated