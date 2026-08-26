from db.connection import db
from geoalchemy2 import Geometry


class User(db.Model):
    __tablename__ = 'users'
    id              = db.Column(db.Integer, primary_key=True)
    github_id       = db.Column(db.BigInteger, unique=True, nullable=False, index=True)
    github_login    = db.Column(db.String(255), unique=True, nullable=False)
    avatar_url      = db.Column(db.Text, nullable=True)
    email           = db.Column(db.String(255), nullable=True)
    chat_count      = db.Column(db.Integer, nullable=False, default=0)
    chat_count_date = db.Column(db.Date, nullable=True)
    created_at      = db.Column(db.DateTime, server_default=db.func.now())


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
