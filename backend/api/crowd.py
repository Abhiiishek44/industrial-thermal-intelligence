"""
api/crowd.py
------------
Crowd intelligence: field reports, themes, likes, comments.

Routes (all registered under /api/events):
    POST  /<event_id>/field-reports
    GET   /<event_id>/field-reports
    GET   /<event_id>/themes
    POST  /<event_id>/themes/<theme_id>/like
    POST  /<event_id>/themes/<theme_id>/comments

Simulate route → crowd_simulate.py (registered on crowd_bp at bottom).
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from utils.auth_middleware import token_required, admin_required

from api.crowd_processing import bg_assess_and_cluster as _bg_assess_and_cluster

crowd_bp = Blueprint("crowd", __name__)

_UPLOAD_DIR = Path(__file__).resolve().parents[2] / "data" / "uploads"


def _extract_bearing(photo_path: Path) -> float | None:
    try:
        from PIL import Image
        img       = Image.open(photo_path)
        exif      = img._getexif()
        if not exif:
            return None
        gps       = exif.get(34853, {})
        direction = gps.get(17)
        if direction is None:
            return None
        if isinstance(direction, tuple) and len(direction) == 2:
            return direction[0] / direction[1] if direction[1] != 0 else None
        return float(direction)
    except Exception:
        return None


@crowd_bp.route("/<int:event_id>/field-reports", methods=["POST"])
@token_required
def submit_field_report(event_id: int):
    from db.connection import db
    from db.models import FieldReport
    from utils.background import run_in_background

    is_multipart = "multipart" in (request.content_type or "")
    if is_multipart:
        post_type   = request.form.get("post_type", "info")
        description = request.form.get("description", "")
        lat         = float(request.form.get("lat", 0))
        lon         = float(request.form.get("lon", 0))
        photo_file  = request.files.get("photo")
    else:
        data        = request.get_json(force=True) or {}
        post_type   = data.get("post_type", "info")
        description = data.get("description", "")
        lat         = float(data.get("lat", 0))
        lon         = float(data.get("lon", 0))
        photo_file  = None

    report = FieldReport(
        event_id    = event_id,
        user_id     = getattr(request, "_jwt_user_id", None),
        post_type   = post_type,
        description = description,
        lat         = lat,
        lon         = lon,
    )
    db.session.add(report)
    db.session.flush()

    bearing = photo_path = None
    if photo_file and photo_file.filename:
        _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        ext        = Path(photo_file.filename).suffix.lower() or ".jpg"
        dest       = _UPLOAD_DIR / f"{report.id}{ext}"
        photo_file.save(str(dest))
        photo_path = f"data/uploads/{report.id}{ext}"
        bearing    = _extract_bearing(dest)

    report.bearing    = bearing
    report.photo_path = photo_path
    db.session.commit()

    return jsonify({"id": report.id, "bearing_available": bearing is not None}), 201


@crowd_bp.route("/<int:event_id>/field-reports", methods=["GET"])
@token_required
def list_field_reports(event_id: int):
    from db.models import FieldReport
    from datetime import datetime, timedelta

    # ?before=<ISO8601>  →  return reports in the 24 h window ending at that time
    before_str = request.args.get("before")
    if before_str:
        try:
            before_dt = datetime.fromisoformat(before_str.replace("Z", "+00:00")).replace(tzinfo=None)
            cutoff_dt = before_dt - timedelta(hours=24)
            reports = (
                FieldReport.query
                .filter_by(event_id=event_id)
                .filter(FieldReport.created_at >= cutoff_dt,
                        FieldReport.created_at <= before_dt)
                .order_by(FieldReport.created_at.desc())
                .all()
            )
        except ValueError:
            return jsonify({"error": "invalid before param"}), 400
    else:
        reports = (
            FieldReport.query
            .filter_by(event_id=event_id)
            .order_by(FieldReport.created_at.desc())
            .all()
        )
    return jsonify([{
        "id":           r.id,
        "post_type":    r.post_type,
        "lat":          r.lat,
        "lon":          r.lon,
        "description":  r.description,
        "bearing":      r.bearing,
        "theme_id":     r.theme_id,
        "like_count":   r.like_count,
        "flag_count":   r.flag_count,
        "created_at":   r.created_at.isoformat() if r.created_at else None,
    } for r in reports])


@crowd_bp.route("/<int:event_id>/field-reports/<int:report_id>/like", methods=["POST"])
@token_required
def like_field_report(event_id: int, report_id: int):
    from db.connection import db
    from db.models import FieldReport
    report = db.session.get(FieldReport, report_id)
    if not report or report.event_id != event_id:
        return jsonify({"error": "Report not found"}), 404
    report.like_count += 1
    db.session.commit()
    return jsonify({"like_count": report.like_count})


@crowd_bp.route("/<int:event_id>/field-reports/<int:report_id>/flag", methods=["POST"])
@token_required
def flag_field_report(event_id: int, report_id: int):
    from db.connection import db
    from db.models import FieldReport
    report = db.session.get(FieldReport, report_id)
    if not report or report.event_id != event_id:
        return jsonify({"error": "Report not found"}), 404
    report.flag_count += 1
    db.session.commit()
    return jsonify({"flag_count": report.flag_count})


@crowd_bp.route("/<int:event_id>/field-reports/<int:report_id>/comments", methods=["GET"])
@token_required
def list_report_comments(event_id: int, report_id: int):
    from db.models import FieldReport, FieldReportComment
    report = FieldReport.query.filter_by(id=report_id, event_id=event_id).first()
    if not report:
        return jsonify({"error": "Report not found"}), 404
    comments = (
        FieldReportComment.query
        .filter_by(report_id=report_id)
        .order_by(FieldReportComment.created_at.asc())
        .all()
    )
    return jsonify([{
        "id":         c.id,
        "content":    c.content,
        "user_id":    c.user_id,
        "like_count": c.like_count,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    } for c in comments])


@crowd_bp.route("/<int:event_id>/field-reports/<int:report_id>/comments/<int:comment_id>/like", methods=["POST"])
@token_required
def like_report_comment(event_id: int, report_id: int, comment_id: int):
    from db.connection import db
    from db.models import FieldReportComment
    comment = db.session.get(FieldReportComment, comment_id)
    if not comment or comment.report_id != report_id:
        return jsonify({"error": "Comment not found"}), 404
    comment.like_count += 1
    db.session.commit()
    return jsonify({"like_count": comment.like_count})


@crowd_bp.route("/<int:event_id>/field-reports/<int:report_id>/comments/<int:comment_id>/unlike", methods=["POST"])
@token_required
def unlike_report_comment(event_id: int, report_id: int, comment_id: int):
    from db.connection import db
    from db.models import FieldReportComment
    comment = db.session.get(FieldReportComment, comment_id)
    if not comment or comment.report_id != report_id:
        return jsonify({"error": "Comment not found"}), 404
    comment.like_count = max(0, comment.like_count - 1)
    db.session.commit()
    return jsonify({"like_count": comment.like_count})


@crowd_bp.route("/<int:event_id>/field-reports/<int:report_id>/comments", methods=["POST"])
@token_required
def add_report_comment(event_id: int, report_id: int):
    from db.connection import db
    from db.models import FieldReport, FieldReportComment
    report = FieldReport.query.filter_by(id=report_id, event_id=event_id).first()
    if not report:
        return jsonify({"error": "Report not found"}), 404
    data    = request.get_json(force=True) or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "content required"}), 400
    comment = FieldReportComment(
        report_id = report_id,
        user_id   = getattr(request, "_jwt_user_id", None),
        content   = content,
    )
    db.session.add(comment)
    db.session.commit()
    return jsonify({"id": comment.id, "content": comment.content,
                    "created_at": comment.created_at.isoformat() if comment.created_at else None}), 201


@crowd_bp.route("/<int:event_id>/field-reports/clear", methods=["POST"])
@admin_required
def clear_field_reports(event_id: int):
    """DEV only — delete all field reports (and their comments) for this event,
    and remove all crowd-derived prediction files from disk."""
    import shutil
    from pathlib import Path
    from db.connection import db
    from db.models import FieldReport, FieldReportComment, FireEvent, EventTimestep
    import pandas as pd

    report_ids = [r.id for r in FieldReport.query.filter_by(event_id=event_id).with_entities(FieldReport.id).all()]
    if report_ids:
        FieldReportComment.query.filter(FieldReportComment.report_id.in_(report_ids)).delete(synchronize_session=False)
    FieldReport.query.filter_by(event_id=event_id).delete(synchronize_session=False)
    db.session.commit()

    # ── Purge crowd output files from every timestep ──────────────────────────
    event = FireEvent.query.get(event_id)
    if event:
        data_dir = Path(__file__).resolve().parents[2] / "data"
        timesteps = EventTimestep.query.filter_by(event_id=event_id).all()
        for ts in timesteps:
            ts_str  = pd.Timestamp(ts.slot_time).strftime("%Y-%m-%dT%H%M")
            ts_base = data_dir / "events" / f"{event.year}_{event_id:04d}" / "timesteps" / ts_str
            # Remove crowd-specific files/dirs
            for target in [
                ts_base / "hotspot" / "hotspots_crowd.geojson",
                ts_base / "perimeter" / "perimeter_crowd.geojson",
                ts_base / "prediction" / "ML_crowd",
                ts_base / "spatial_analysis_crowd",
                ts_base / "AI_report" / "crowd.json",
                ts_base / "AI_report" / "summary.json",  # invalidate cached summary
            ]:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists():
                    target.unlink()

    return jsonify({"deleted": len(report_ids)})


# Register simulate route on this blueprint (self-contained sim_ai module)
import sim_ai  # noqa: E402, F401
