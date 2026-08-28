import os
from datetime import date

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def get_db_uri():
    return (
        f"postgresql://{os.getenv('DB_USER', 'postgres')}"
        f":{os.getenv('DB_PASSWORD', 'password')}"
        f"@{os.getenv('DB_HOST', 'localhost')}"
        f":{os.getenv('DB_PORT', '5432')}"
        f"/{os.getenv('DB_NAME', 'wildfire_db')}"
    )


def ensure_db():
    """Create the database and enable PostGIS if they don't already exist.

    SQLAlchemy's create_all() can only create tables — not the database itself.
    This function runs before create_all() to guarantee the database exists
    and PostGIS is enabled, so the app can start without any manual setup.
    """
    db_name = os.getenv('DB_NAME', 'wildfire_db')
    conn_args = dict(
        host=os.getenv('DB_HOST', 'localhost'),
        port=os.getenv('DB_PORT', '5432'),
        user=os.getenv('DB_USER', 'postgres'),
        password=os.getenv('DB_PASSWORD', ''),
    )

    # Step 1: connect to the default 'postgres' DB to create our database
    conn = psycopg2.connect(**conn_args, dbname='postgres')
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
    if not cur.fetchone():
        cur.execute(f'CREATE DATABASE "{db_name}"')
        print(f"[db] created database '{db_name}'")
    cur.close()
    conn.close()

    # Step 2: connect to our database and enable the PostGIS extension
    conn2 = psycopg2.connect(**conn_args, dbname=db_name)
    conn2.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur2 = conn2.cursor()
    cur2.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    cur2.close()
    conn2.close()


def seed_db():
    """Insert configured demo events and synchronize their replay windows.

    Safe to call on every startup. Other fields on existing events are preserved.

    An admin credential account is created once when ADMIN_PASSWORD is set.
    """
    from db.models import FireEvent, User
    from geoalchemy2 import WKTElement

    admin_password = os.getenv('ADMIN_PASSWORD', '')
    admin_username = os.getenv('ADMIN_USERNAME', 'admin').strip().lower()
    if admin_password:
        admin = User.query.filter_by(username=admin_username).first()
        if not admin:
            admin = User(username=admin_username, is_admin=True)
            admin.set_password(admin_password)
            db.session.add(admin)
            db.session.commit()
            print(f"[db] seeded admin account '{admin_username}'")
        elif not admin.is_admin:
            print(
                f"[db] admin account '{admin_username}' already exists without admin access; "
                "not promoting it automatically"
            )

    from pipeline.event_config import EVENT_CONFIGS

    added = []
    updated = []
    for config in EVENT_CONFIGS:
        existing = db.session.get(FireEvent, config.event_id)
        if existing is None:
            existing = FireEvent.query.filter_by(name=config.name).first()
        if existing is not None:
            configured_start = date.fromisoformat(config.start_date)
            configured_end = date.fromisoformat(config.end_date)
            # A successful live thermal refresh advances the replay window.
            # Preserve that progress across restarts instead of resetting the
            # event to the original demonstration end date.
            synchronized_end = configured_end
            if (
                config.analysis_mode == "thermal_monitoring"
                and existing.end_date is not None
                and existing.end_date > configured_end
            ):
                synchronized_end = existing.end_date
            configured_bbox = WKTElement(config.bbox_wkt, srid=4326)
            changed = (
                existing.name != config.name
                or existing.year != config.year
                or existing.start_date != configured_start
                or existing.end_date != synchronized_end
                or existing.description != config.description
            )
            existing.name = config.name
            existing.year = config.year
            existing.bbox = configured_bbox
            existing.start_date = configured_start
            existing.end_date = synchronized_end
            existing.description = config.description
            if changed:
                existing.replay_ms = None
                updated.append(config.name)
            continue
        if db.session.get(FireEvent, config.event_id) is not None:
            print(
                f"[db] cannot seed configured event {config.event_id} ({config.name}): "
                "that id is already in use"
            )
            continue
        event = FireEvent(
            id=config.event_id,
            name=config.name,
            year=config.year,
            bbox=WKTElement(config.bbox_wkt, srid=4326),
            start_date=config.start_date,
            end_date=config.end_date,
            description=config.description,
        )
        db.session.add(event)
        added.append(config.name)

    if added:
        from sqlalchemy import text

        db.session.flush()
        db.session.execute(text(
            "SELECT setval(pg_get_serial_sequence('fire_events', 'id'), "
            "MAX(id), true) FROM fire_events"
        ))
        print(f"[db] seeded {len(added)} fire event(s): {', '.join(added)}")

    if added or updated:
        db.session.commit()
    if updated:
        print(f"[db] synchronized {len(updated)} configured event(s): {', '.join(updated)}")
