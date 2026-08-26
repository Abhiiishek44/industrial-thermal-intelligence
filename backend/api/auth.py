"""Credential authentication with short-lived access and rotating refresh JWTs."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from flask import Blueprint, jsonify, request
from sqlalchemy.exc import IntegrityError

from db.connection import db
from db.models import RefreshToken, User
from utils.auth_middleware import token_required


auth_bp = Blueprint("auth", __name__)

_JWT_ALGORITHM = "HS256"
_USERNAME_RE = re.compile(r"^[a-z0-9_-]{3,50}$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET_KEY", "")
    if not secret:
        raise RuntimeError("JWT_SECRET_KEY must be configured.")
    return secret


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except ValueError:
        return default


def _access_lifetime() -> timedelta:
    return timedelta(minutes=_positive_int_env("JWT_ACCESS_TOKEN_MINUTES", 15))


def _refresh_lifetime() -> timedelta:
    return timedelta(days=_positive_int_env("JWT_REFRESH_TOKEN_DAYS", 30))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_admin": bool(user.is_admin),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _encode_token(user: User, token_type: str, lifetime: timedelta, jti: str) -> tuple[str, datetime]:
    now = datetime.now(timezone.utc)
    expires_at = now + lifetime
    claims = {
        "sub": str(user.id),
        "type": token_type,
        "jti": jti,
        "iat": now,
        "exp": expires_at,
    }
    if token_type == "access":
        claims.update({"username": user.username, "is_admin": bool(user.is_admin)})
    return jwt.encode(claims, _jwt_secret(), algorithm=_JWT_ALGORITHM), expires_at


def _create_token_pair(user: User, family_id: str | None = None) -> tuple[dict, RefreshToken]:
    access_token, _ = _encode_token(user, "access", _access_lifetime(), str(uuid.uuid4()))
    refresh_jti = str(uuid.uuid4())
    refresh_token, refresh_expires_at = _encode_token(
        user, "refresh", _refresh_lifetime(), refresh_jti
    )
    record = RefreshToken(
        user_id=user.id,
        token_hash=_token_hash(refresh_token),
        jti=refresh_jti,
        family_id=family_id or str(uuid.uuid4()),
        expires_at=refresh_expires_at,
    )
    db.session.add(record)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "access_expires_in": int(_access_lifetime().total_seconds()),
        "user": _serialize_user(user),
    }, record


def _decode_refresh_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(
            token,
            _jwt_secret(),
            algorithms=[_JWT_ALGORITHM],
            options={"require": ["sub", "type", "jti", "iat", "exp"]},
        )
    except jwt.InvalidTokenError:
        return None
    return payload if payload.get("type") == "refresh" else None


def _credentials() -> tuple[str, str, str | None, str | None]:
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", ""))
    email_value = data.get("email")
    email = str(email_value).strip().lower() if email_value else None

    if not _USERNAME_RE.fullmatch(username):
        return username, password, email, "Username must be 3-50 lowercase letters, numbers, '-' or '_'."
    if len(password) < 8:
        return username, password, email, "Password must be at least 8 characters."
    if len(password.encode("utf-8")) > 72:
        return username, password, email, "Password must be at most 72 UTF-8 bytes."
    if email and (len(email) > 255 or not _EMAIL_RE.fullmatch(email)):
        return username, password, email, "A valid email address is required."
    return username, password, email, None


@auth_bp.post("/register")
def register():
    username, password, email, validation_error = _credentials()
    if validation_error:
        return jsonify({"message": validation_error}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({"message": "Username is already registered."}), 409
    if email and User.query.filter_by(email=email).first():
        return jsonify({"message": "Email is already registered."}), 409

    user = User(username=username, email=email, is_admin=False)
    user.set_password(password)
    db.session.add(user)
    try:
        db.session.flush()
        response, _ = _create_token_pair(user)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"message": "Username or email is already registered."}), 409

    return jsonify(response), 201


@auth_bp.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", ""))
    user = User.query.filter_by(username=username).first() if username else None

    if not user or not user.check_password(password):
        return jsonify({"message": "Invalid username or password."}), 401

    response, _ = _create_token_pair(user)
    db.session.commit()
    return jsonify(response), 200


@auth_bp.post("/refresh")
def refresh():
    data = request.get_json(silent=True) or {}
    raw_token = str(data.get("refresh_token", ""))
    payload = _decode_refresh_token(raw_token)
    if not payload:
        return jsonify({"message": "Invalid or expired refresh token."}), 401

    record = (
        RefreshToken.query.filter_by(token_hash=_token_hash(raw_token))
        .with_for_update()
        .first()
    )
    if not record or record.jti != payload.get("jti") or str(record.user_id) != payload.get("sub"):
        return jsonify({"message": "Invalid or expired refresh token."}), 401

    now = datetime.now(timezone.utc)
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if record.revoked_at is not None:
        RefreshToken.query.filter_by(family_id=record.family_id, revoked_at=None).update(
            {"revoked_at": now}, synchronize_session=False
        )
        db.session.commit()
        return jsonify({"message": "Refresh token reuse detected; session revoked."}), 401
    if expires_at <= now:
        record.revoked_at = now
        db.session.commit()
        return jsonify({"message": "Invalid or expired refresh token."}), 401

    user = db.session.get(User, record.user_id)
    if not user:
        record.revoked_at = now
        db.session.commit()
        return jsonify({"message": "Invalid or expired refresh token."}), 401

    record.revoked_at = now
    response, replacement = _create_token_pair(user, family_id=record.family_id)
    db.session.flush()
    record.replaced_by_id = replacement.id
    db.session.commit()
    return jsonify(response), 200


@auth_bp.post("/logout")
def logout():
    data = request.get_json(silent=True) or {}
    raw_token = str(data.get("refresh_token", ""))
    if raw_token:
        record = RefreshToken.query.filter_by(token_hash=_token_hash(raw_token)).first()
        if record:
            now = datetime.now(timezone.utc)
            RefreshToken.query.filter_by(family_id=record.family_id, revoked_at=None).update(
                {"revoked_at": now}, synchronize_session=False
            )
            db.session.commit()
    return "", 204


@auth_bp.get("/me")
@token_required
def me():
    return jsonify({"user": _serialize_user(request.auth_user)}), 200
