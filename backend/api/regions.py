"""Public region discovery endpoints."""

from flask import Blueprint, jsonify

from pipeline.regions import REGIONS


regions_bp = Blueprint("regions", __name__)


@regions_bp.route("/", methods=["GET"])
def list_regions():
    return jsonify([region.as_dict() for region in REGIONS.values()]), 200


@regions_bp.route("/<region_id>", methods=["GET"])
def get_region(region_id: str):
    region = REGIONS.get(region_id.lower())
    if region is None:
        return jsonify({"error": "region not found"}), 404
    return jsonify(region.as_dict()), 200

