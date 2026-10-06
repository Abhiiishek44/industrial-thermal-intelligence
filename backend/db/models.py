from db.connection import db
from geoalchemy2 import Geometry
import bcrypt


class User(db.Model):
    __tablename__ = 'users'
    id              = db.Column(db.Integer, primary_key=True)
    username        = db.Column(db.String(50), unique=True, nullable=False, index=True)
    password_hash   = db.Column(db.String(128), nullable=False)
    email           = db.Column(db.String(255), unique=True, nullable=True, index=True)
    is_admin        = db.Column(db.Boolean, nullable=False, default=False)
    chat_count      = db.Column(db.Integer, nullable=False, default=0)
    chat_count_date = db.Column(db.Date, nullable=True)
    created_at      = db.Column(db.DateTime, server_default=db.func.now())

    def set_password(self, password: str) -> None:
        self.password_hash = bcrypt.hashpw(
            password.encode('utf-8'), bcrypt.gensalt()
        ).decode('utf-8')

    def check_password(self, password: str) -> bool:
        try:
            return bcrypt.checkpw(
                password.encode('utf-8'), self.password_hash.encode('utf-8')
            )
        except (TypeError, ValueError):
            # Migrated provider-only identities have a deliberately unusable hash.
            return False


class RefreshToken(db.Model):
    __tablename__ = 'refresh_tokens'
    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    token_hash      = db.Column(db.String(64), unique=True, nullable=False, index=True)
    jti             = db.Column(db.String(36), unique=True, nullable=False)
    family_id       = db.Column(db.String(36), nullable=False, index=True)
    expires_at      = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    revoked_at      = db.Column(db.DateTime(timezone=True), nullable=True)
    replaced_by_id = db.Column(db.Integer, db.ForeignKey('refresh_tokens.id'), nullable=True)
    created_at      = db.Column(db.DateTime(timezone=True), server_default=db.func.now())

    user = db.relationship('User', backref=db.backref('refresh_tokens', lazy=True, cascade='all, delete-orphan'))


class FireEvent(db.Model):
    __tablename__ = "fire_events"
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.Text, nullable=False)
    year        = db.Column(db.Integer)
    bbox        = db.Column(Geometry("POLYGON", srid=4326))
    start_date  = db.Column(db.Date, nullable=False)
    description = db.Column(db.Text)

    # Pipeline mode control:
    #   NULL  → Realtime: pipeline fetches latest data on each run
    #   value → Replay:   historical analysis up to this date (inclusive)
    end_date    = db.Column(db.Date, nullable=True)

    # Admin-controlled shared replay clock (ms since epoch). NULL = start of event.
    replay_ms   = db.Column(db.BigInteger, nullable=True)

    timesteps   = db.relationship("EventTimestep", backref="event", lazy=True)

    @property
    def is_realtime(self) -> bool:
        return self.end_date is None


class EventTimestep(db.Model):
    __tablename__ = "event_timesteps"
    __table_args__ = (
        db.UniqueConstraint(
            "event_id", "slot_time", name="uq_event_timesteps_event_slot"
        ),
    )

    id         = db.Column(db.Integer, primary_key=True)
    event_id   = db.Column(db.Integer, db.ForeignKey("fire_events.id"), nullable=False)

    # Canonical 3-hour slot on the regular time grid
    slot_time  = db.Column(db.DateTime(timezone=True), nullable=False)

    # Most recent satellite overpass at or before slot_time (the T1 used for prediction)
    nearest_t1 = db.Column(db.DateTime(timezone=True), nullable=False)

    # Hours between slot_time and nearest_t1 (always >= 0)
    gap_hours      = db.Column(db.Float, nullable=False, default=0.0)

    # True when gap_hours > 12 — prediction may be stale
    data_gap_warn  = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime, server_default=db.func.now())


# Near-real-time FIRMS persistence.  These tables are intentionally separate
# from ``FireEvent`` above: that entity represents configured analysis regions,
# while a monitoring event represents one physical thermal incident.
class MonitoringFireEvent(db.Model):
    __tablename__ = "monitoring_fire_events"
    __table_args__ = (
        db.Index("ix_monitoring_fire_events_last_detected", "last_detected_at"),
        db.Index("ix_monitoring_fire_events_status", "status"),
        db.Index("ix_monitoring_fire_events_region", "monitoring_region_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    monitoring_region_id = db.Column(
        db.Integer, db.ForeignKey("fire_events.id"), nullable=True
    )
    event_key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    centroid_latitude = db.Column(db.Float, nullable=False)
    centroid_longitude = db.Column(db.Float, nullable=False)
    location = db.Column(Geometry("POINT", srid=4326), nullable=True)
    first_detected_at = db.Column(db.DateTime(timezone=True), nullable=False)
    last_detected_at = db.Column(db.DateTime(timezone=True), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="NEW")
    detection_count = db.Column(db.Integer, nullable=False, default=1)
    max_frp = db.Column(db.Float, nullable=True)
    average_frp = db.Column(db.Float, nullable=True)
    predicted_class = db.Column(db.String(64), nullable=True)
    confidence = db.Column(db.Float, nullable=True)
    alert_status = db.Column(db.String(24), nullable=False, default="NOT_EVALUATED")
    alert_sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())
    updated_at = db.Column(
        db.DateTime(timezone=True), server_default=db.func.now(), onupdate=db.func.now()
    )


class FireDetection(db.Model):
    __tablename__ = "fire_detections"
    __table_args__ = (
        db.UniqueConstraint("detection_key", name="uq_fire_detections_detection_key"),
        db.Index("ix_fire_detections_acquisition_time", "acquisition_time"),
        db.Index("ix_fire_detections_event_id", "fire_event_id"),
        db.Index("ix_fire_detections_predicted_class", "predicted_class"),
        db.Index("ix_fire_detections_region", "monitoring_region_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    monitoring_region_id = db.Column(
        db.Integer, db.ForeignKey("fire_events.id"), nullable=True
    )
    detection_key = db.Column(db.String(64), nullable=False)
    fire_event_id = db.Column(
        db.Integer, db.ForeignKey("monitoring_fire_events.id"), nullable=True
    )
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    location = db.Column(Geometry("POINT", srid=4326), nullable=True)
    acquisition_time = db.Column(db.DateTime(timezone=True), nullable=False)
    received_at = db.Column(db.DateTime(timezone=True), nullable=False)
    data_latency_seconds = db.Column(db.Integer, nullable=False, default=0)
    satellite = db.Column(db.String(32), nullable=False)
    instrument = db.Column(db.String(32), nullable=False)
    source_product = db.Column(db.String(64), nullable=True)
    brightness = db.Column(db.Float, nullable=True)
    bright_ti4 = db.Column(db.Float, nullable=True)
    bright_ti5 = db.Column(db.Float, nullable=True)
    frp = db.Column(db.Float, nullable=True)
    firms_confidence = db.Column(db.String(32), nullable=True)
    day_night = db.Column(db.String(8), nullable=True)
    land_cover = db.Column(db.String(64), nullable=True)
    landcover_group = db.Column(db.String(32), nullable=True)
    inside_industrial_polygon = db.Column(db.Boolean, nullable=True)
    near_industrial_facility = db.Column(db.Boolean, nullable=True)
    model_features = db.Column(db.JSON, nullable=True)
    predicted_class = db.Column(db.String(64), nullable=True)
    model_confidence = db.Column(db.Float, nullable=True)
    model_version = db.Column(db.String(64), nullable=False)
    alert_status = db.Column(db.String(24), nullable=False, default="NOT_EVALUATED")
    alert_sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())
    updated_at = db.Column(
        db.DateTime(timezone=True), server_default=db.func.now(), onupdate=db.func.now()
    )

    fire_event = db.relationship(
        "MonitoringFireEvent",
        backref=db.backref("detections", lazy=True),
    )


class FireAlert(db.Model):
    __tablename__ = "fire_alerts"
    __table_args__ = (
        db.UniqueConstraint("deduplication_key", name="uq_fire_alerts_deduplication_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    fire_event_id = db.Column(
        db.Integer, db.ForeignKey("monitoring_fire_events.id"), nullable=False, index=True
    )
    detection_id = db.Column(
        db.Integer, db.ForeignKey("fire_detections.id"), nullable=True
    )
    alert_type = db.Column(db.String(32), nullable=False)
    severity = db.Column(db.String(16), nullable=False)
    message = db.Column(db.Text, nullable=False)
    deduplication_key = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())


class FirmsSyncState(db.Model):
    __tablename__ = "firms_sync_state"

    id = db.Column(db.Integer, primary_key=True, default=1)
    status = db.Column(db.String(24), nullable=False, default="never")
    last_attempt_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_successful_sync = db.Column(db.DateTime(timezone=True), nullable=True)
    records_received = db.Column(db.Integer, nullable=False, default=0)
    new_records = db.Column(db.Integer, nullable=False, default=0)
    duplicates = db.Column(db.Integer, nullable=False, default=0)
    prediction_failures = db.Column(db.Integer, nullable=False, default=0)
    error = db.Column(db.Text, nullable=True)


# 2. Crowd intelligence ────────────────────────────────────────────────────────

class Theme(db.Model):
    __tablename__ = 'themes'
    id         = db.Column(db.Integer, primary_key=True)
    event_id   = db.Column(db.Integer, db.ForeignKey('fire_events.id'), nullable=True)

    center_lat = db.Column(db.Float, nullable=False)
    center_lon = db.Column(db.Float, nullable=False)
    radius_m   = db.Column(db.Float, nullable=False, default=1000.0)

    title      = db.Column(db.Text, nullable=False)
    summary    = db.Column(db.Text, nullable=False)

    like_count   = db.Column(db.Integer, nullable=False, default=0)
    generated_at = db.Column(db.DateTime, nullable=True)
    created_at   = db.Column(db.DateTime, server_default=db.func.now())

    reports  = db.relationship('FieldReport', backref='theme', lazy=True,
                               foreign_keys='FieldReport.theme_id')
    comments = db.relationship('ThemeComment', backref='theme', lazy=True)


class FieldReport(db.Model):
    __tablename__ = 'field_reports'
    id       = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('fire_events.id'), nullable=True)
    user_id  = db.Column(db.Integer, db.ForeignKey('users.id'),       nullable=True)

    # 'fire_report' | 'info' | 'request_help' | 'offer_help'
    post_type   = db.Column(db.Text, nullable=False)
    lat         = db.Column(db.Float, nullable=False)
    lon         = db.Column(db.Float, nullable=False)
    bearing     = db.Column(db.Float, nullable=True)   # degrees from EXIF (fire_report only)
    photo_path  = db.Column(db.Text,  nullable=True)
    description = db.Column(db.Text,  nullable=True)

    # AI-assessed intensity (background, set after insert)
    # 'low' | 'mid' | 'high' | null
    ai_intensity = db.Column(db.Text, nullable=True)

    # Community interaction
    like_count = db.Column(db.Integer, nullable=False, default=0)
    flag_count = db.Column(db.Integer, nullable=False, default=0)

    # Set when report is absorbed into a theme cluster
    theme_id   = db.Column(db.Integer, db.ForeignKey('themes.id'), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    comments = db.relationship('FieldReportComment', backref='report', lazy=True)


class FieldReportComment(db.Model):
    __tablename__ = 'field_report_comments'
    id        = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('field_reports.id'), nullable=False)
    user_id   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    content    = db.Column(db.Text, nullable=False)
    like_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class ThemeComment(db.Model):
    __tablename__ = 'theme_comments'
    id       = db.Column(db.Integer, primary_key=True)
    theme_id = db.Column(db.Integer, db.ForeignKey('themes.id'), nullable=False)
    user_id  = db.Column(db.Integer, db.ForeignKey('users.id'),  nullable=True)

    content    = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
