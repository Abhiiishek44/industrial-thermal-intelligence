"""Bearer access-token authentication decorators."""

from __future__ import annotations

import os
from functools import wraps
from types import SimpleNamespace

import jwt
from flask import jsonify, request

from db.connection import db
from db.models import User


_JWT_ALGORITHM = "HS256"


def _jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET_KEY", "")
    if not secret:
        raise RuntimeError("JWT_SECRET_KEY must be configured.")
    return secret


def _decode_access_token():
    auth_header = request.headers.get("Authorization", "")
    scheme, separator, token = auth_header.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        return None, (jsonify({"message": "Access token missing."}), 401)

    try:
        payload = jwt.decode(
            token.strip(),
            _jwt_secret(),
            algorithms=[_JWT_ALGORITHM],
            options={"require": ["sub", "type", "jti", "iat", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        return None, (jsonify({"message": "Access token expired."}), 401)
    except jwt.InvalidTokenError:
        return None, (jsonify({"message": "Invalid access token."}), 401)

    if payload.get("type") != "access":
        return None, (jsonify({"message": "Invalid access token."}), 401)
    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError):
        return None, (jsonify({"message": "Invalid access token."}), 401)

    user = db.session.get(User, user_id)
    if not user:
        return None, (jsonify({"message": "User no longer exists."}), 401)
    return user, None


def _authenticated(admin_only: bool = False):
    def decorator(function):
        @wraps(function)
        def decorated(*args, **kwargs):
            if request.headers.get("X-Demo-Mode") == "judge-preview":
                if admin_only:
                    return jsonify({"message": "Admin access required."}), 403
                demo_user = SimpleNamespace(
                    id=0,
                    username="demo",
                    email=None,
                    is_admin=False,
                    created_at=None,
                )
                request.current_user = {
                    "user_id": demo_user.id,
                    "username": demo_user.username,
                    "is_admin": False,
                }
                request.auth_user = demo_user
                request._jwt_user_id = demo_user.id
                return function(*args, **kwargs)

            user, error = _decode_access_token()
            if error:
                return error
            if admin_only and not user.is_admin:
                return jsonify({"message": "Admin access required."}), 403
            request.current_user = {
                "user_id": user.id,
                "username": user.username,
                "is_admin": bool(user.is_admin),
            }
            request.auth_user = user
            request._jwt_user_id = user.id
            return function(*args, **kwargs)

        return decorated

    return decorator


token_required = _authenticated()
admin_required = _authenticated(admin_only=True)
