import os
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
    """Insert initial data if the database is empty.

    Also patches any existing events that are missing end_date (treated as
    realtime otherwise, which crashes the pipeline). Safe to call on every startup.

    Note: User accounts are no longer seeded — they are created on-the-fly via
    GitHub OAuth callback. Admin status is derived at runtime from
    `github_login == ADMIN_GITHUB_LOGIN`.
    """
    from db.models import FireEvent
    from geoalchemy2 import WKTElement

    _SEED_NAME = 'Fort McMurray Wildfire 2016'
    _SEED_END  = '2016-05-10'

    if FireEvent.query.count() == 0:
        events = [
            FireEvent(
                name        = _SEED_NAME,
                year        = 2016,
                bbox        = WKTElement(
                    'POLYGON((-112.634 56.157, -110.002 56.157, -110.002 57.380, -112.634 57.380, -112.634 56.157))',
                    srid=4326
                ),
                start_date  = '2016-05-01',
                end_date    = _SEED_END,
                description = (
                    'The 2016 Horse River Wildfire (MWF-009) forced the evacuation of approximately '
                    '88,000 residents from Fort McMurray, Alberta. It burned approximately 590,000 '
                    'hectares and is the costliest disaster in Canadian history.'
                ),
            ),
        ]
        db.session.add_all(events)
        db.session.commit()
        print(f"[db] seeded {len(events)} fire event(s)")
        return

    # Patch any existing event missing end_date — without it the pipeline
    # treats the event as realtime and crashes on event.end_date.strftime().
    patched = 0
    for event in FireEvent.query.filter(FireEvent.end_date.is_(None)).all():
        event.end_date = _SEED_END
        patched += 1
    if patched:
        db.session.commit()
        print(f"[db] patched end_date on {patched} existing fire event(s)")
