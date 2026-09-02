import config  # loads .env variables
import logging
import os
import time
from flask import Flask
from flask_cors import CORS
from pathlib import Path
from db.connection import db, get_db_uri

BASE_DIR     = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def create_app():
    app = Flask(__name__)
    CORS(app)
    app.config['SQLALCHEMY_DATABASE_URI'] = get_db_uri()
    db.init_app(app)

    # ── Blueprints ────────────────────────────────────────────────────────────
    from api.auth      import auth_bp
    from api.events    import events_bp
    from api.timesteps import timesteps_bp
    from api.firms     import firms_bp
    from api.config    import config_bp
    from api.regions   import regions_bp
    from api.crowd     import crowd_bp

    app.register_blueprint(auth_bp,      url_prefix='/auth')
    app.register_blueprint(events_bp,    url_prefix='/api/events')
    app.register_blueprint(timesteps_bp, url_prefix='/api')
    app.register_blueprint(firms_bp,     url_prefix='/api/firms')
    app.register_blueprint(config_bp,    url_prefix='/api/config')
    app.register_blueprint(regions_bp,   url_prefix='/api/regions')
    app.register_blueprint(crowd_bp,     url_prefix='/api/events')

    # ── Frontend routes ───────────────────────────────────────────────────────
    from flask import redirect, request, send_from_directory

    @app.route('/')
    @app.route('/login')
    @app.route('/signup')
    def index():
        return send_from_directory(str(FRONTEND_DIR), 'index.html')

    @app.route('/health')
    def health():
        """Railway readiness check: HTTP is live and Postgres is reachable."""
        from flask import jsonify
        from sqlalchemy import text

        try:
            db.session.execute(text("SELECT 1"))
        except Exception as exc:
            db.session.rollback()
            app.logger.warning("Health check database query failed: %s", exc)
            return jsonify({"status": "unhealthy", "database": "unavailable"}), 503
        return jsonify({"status": "healthy", "database": "available"}), 200

    @app.route('/demo')
    @app.route('/demo/')
    @app.route('/demo/dashboard')
    @app.route('/demo/dashboard/')
    @app.route('/dashboard')
    @app.route('/dashboard/')
    def dashboard():
        return send_from_directory(str(FRONTEND_DIR), 'dashboard.html')

    @app.route('/demo/dashboard.html')
    @app.route('/dashboard.html')
    def legacy_dashboard_url():
        """Keep old links working while exposing the extension-free URL."""
        query = request.query_string.decode('latin-1')
        target = '/demo/dashboard' + (f'?{query}' if query else '')
        return redirect(target, code=308)

    @app.route('/css/<path:filename>')
    def frontend_css(filename):
        return send_from_directory(str(FRONTEND_DIR / 'css'), filename)

    @app.route('/js/<path:filename>')
    def frontend_js(filename):
        return send_from_directory(str(FRONTEND_DIR / 'js'), filename)

    @app.route('/demo/<path:filename>')
    @app.route('/dashboard/<path:filename>')
    def dashboard_static(filename):
        return send_from_directory(str(FRONTEND_DIR), filename)

    return app


def _setup_database_with_retry(app):
    """Wait for a separately managed database to accept connections."""
    from pipeline.db import setup_db
    from psycopg2 import OperationalError as PsycopgOperationalError
    from sqlalchemy.exc import OperationalError as SqlalchemyOperationalError

    attempts = max(1, int(os.getenv("DB_STARTUP_MAX_ATTEMPTS", "12")))
    retry_seconds = max(0.1, float(os.getenv("DB_STARTUP_RETRY_SECONDS", "5")))

    for attempt in range(1, attempts + 1):
        try:
            setup_db(app)
            return
        except (PsycopgOperationalError, SqlalchemyOperationalError) as exc:
            if attempt == attempts:
                raise
            logging.warning(
                "Database is not ready (attempt %d/%d): %s; retrying in %.1fs",
                attempt,
                attempts,
                exc,
                retry_seconds,
            )
            time.sleep(retry_seconds)


def _sweep_desynced_timesteps(app):
    """Reset timesteps where STATUS.json says done/failed but output files are gone.

    'running' is stored in-memory only (builder_slots._running) and never written
    to disk, so zombie running states vanish automatically on restart — no sweep needed.
    """
    import json
    import pandas as pd
    from db.models import EventTimestep, FireEvent

    _DATA_DIR = BASE_DIR.parent / "data"

    def _read_status(status_dir) -> str:
        path = Path(status_dir) / "STATUS.json"
        if not path.exists():
            return "pending"
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("status", "pending")
        except Exception:
            return "pending"

    def _write_status(status_dir, status: str) -> None:
        d = Path(status_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "STATUS.json").write_text(json.dumps({"status": status}, indent=2), encoding="utf-8")

    with app.app_context():
        rows = EventTimestep.query.all()
        reset_count = 0
        for ts in rows:
            event = FireEvent.query.get(ts.event_id)
            if not event:
                continue
            ts_str = pd.Timestamp(ts.slot_time).strftime("%Y-%m-%dT%H%M")
            ts_base = _DATA_DIR / "events" / f"{event.year}_{event.id:04d}" / "timesteps" / ts_str
            ml_dir  = ts_base / "prediction" / "ML"
            sp_dir  = ts_base / "spatial_analysis"

            ml_status = _read_status(ml_dir)
            if ml_status in ("done", "failed"):
                sentinel = ml_dir / "fire_context.json"
                if not sentinel.exists():
                    _write_status(ml_dir, "pending")
                    _write_status(sp_dir, "pending")
                    reset_count += 1

        if reset_count:
            print(f"=== [sweep] Reset {reset_count} desynced timestep(s) to pending ===")
        else:
            print("=== [sweep] All timesteps consistent ===")


if __name__ == '__main__':  
    import threading
    from pipeline.env import prepare_all_events
    from pipeline.check import run_checks

    app = create_app()

    # DB must finish before Flask starts (API needs tables to exist)
    print("=== Setting up database ===")
    _setup_database_with_retry(app)
    print("=== Database ready ===")

    # Pipeline runs in background — Flask starts immediately
    def _run_pipeline():
        print("=== [pipeline] Preparing environment ===")
        prepare_all_events(app)

        print("=== [pipeline] Sweeping desynced timesteps ===")
        _sweep_desynced_timesteps(app)

        print("=== [pipeline] Running checks ===")
        run_checks(app)
        print("=== [pipeline] Complete — timestep slots ready ===")

        from pipeline.thermal.refresh import start_thermal_refresh_scheduler
        start_thermal_refresh_scheduler(app)

    threading.Thread(target=_run_pipeline, daemon=True).start()

    print("=== Starting Flask ===")
    app.run(host='0.0.0.0', debug=False, port=int(os.getenv("PORT", "5000")))
