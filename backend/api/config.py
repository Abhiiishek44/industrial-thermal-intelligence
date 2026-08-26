"""
api/config.py — Public frontend feature flags.
GET /api/config
"""

import os
from flask import Blueprint, jsonify

config_bp = Blueprint("config", __name__)


@config_bp.route("/", methods=["GET"])
def get_config():
    has_sentinel = bool(
        os.environ.get("SENTINELHUB_CLIENT_ID", "").strip() and
        os.environ.get("SENTINELHUB_CLIENT_SECRET", "").strip()
    )
    return jsonify({
        "sentinel_enabled": has_sentinel,   # tells frontend whether to show Satellite button
    }), 200
