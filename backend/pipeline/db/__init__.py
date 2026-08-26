"""
pipeline/db/
------------
DB setup: create database, enable PostGIS, create tables, seed initial data.
Runs once at startup before checks and pipeline stages.
"""

from __future__ import annotations


# Canonical column set for event_timesteps — drop & recreate if stale.
_REQUIRED_COLS = {
    "id", "event_id", "slot_time", "nearest_t1", "gap_hours", "data_gap_warn",
    "created_at",
}


def setup_db(app) -> None:
    """Create DB + PostGIS, create all tables, seed fire events if empty."""
    from db.connection import ensure_db, seed_db, db

    ensure_db()
    with app.app_context():
        _migrate_event_timesteps(db)
        _migrate_users(db)        # must run before create_all (drops legacy table)
        db.create_all()
        _migrate_field_reports(db)
        _migrate_fire_events(db)
        seed_db()


def _migrate_field_reports(db) -> None:
    """Add new columns to field_reports if they are missing (safe, additive migration)."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "field_reports" not in inspector.get_table_names():
        return  # table doesn't exist yet — create_all() will handle it

    existing = {col["name"] for col in inspector.get_columns("field_reports")}
    new_cols = {
        "like_count": "INTEGER NOT NULL DEFAULT 0",
        "flag_count":  "INTEGER NOT NULL DEFAULT 0",
    }
    with db.engine.connect() as conn:
        for col, definition in new_cols.items():
            if col not in existing:
                print(f"[db] field_reports: adding column {col}")
                conn.execute(text(f"ALTER TABLE field_reports ADD COLUMN {col} {definition}"))
        conn.commit()

    # field_report_comments.like_count
    if "field_report_comments" in inspector.get_table_names():
        comment_cols = {col["name"] for col in inspector.get_columns("field_report_comments")}
        if "like_count" not in comment_cols:
            print("[db] field_report_comments: adding column like_count")
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE field_report_comments ADD COLUMN like_count INTEGER NOT NULL DEFAULT 0"))
                conn.commit()


def _migrate_users(db) -> None:
    """Drop legacy users table if it has the old password-auth schema.

    The new schema uses GitHub OAuth (no password column). On a fresh deploy
    `db.create_all()` builds the new table; on an old deploy we drop the legacy
    one so it can be recreated with the new columns.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "users" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("users")}
    legacy = {"username", "password"}
    if legacy & existing:
        print("[db] users: legacy auth schema detected — dropping table")
        with db.engine.connect() as conn:
            conn.execute(text("DROP TABLE users CASCADE"))
            conn.commit()


def _migrate_fire_events(db) -> None:
    """Add replay_ms column to fire_events if missing."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "fire_events" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("fire_events")}
    if "replay_ms" not in existing:
        print("[db] fire_events: adding column replay_ms")
        with db.engine.connect() as conn:
            conn.execute(text("ALTER TABLE fire_events ADD COLUMN replay_ms BIGINT"))
            conn.commit()


def _migrate_event_timesteps(db) -> None:
    """Drop event_timesteps if its columns don't match the current schema.

    event_timesteps is fully rebuilt by the pipeline on every run, so dropping
    it is always safe — no data is lost that can't be regenerated.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "event_timesteps" not in inspector.get_table_names():
        return  # table doesn't exist yet — create_all() will handle it

    existing = {col["name"] for col in inspector.get_columns("event_timesteps")}
    if existing == _REQUIRED_COLS:
        return  # schema is current

    print(f"[db] event_timesteps schema mismatch — dropping and recreating")
    with db.engine.connect() as conn:
        conn.execute(text("DROP TABLE event_timesteps CASCADE"))
        conn.commit()
