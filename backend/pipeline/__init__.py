"""
pipeline/
---------
Full startup sequence: DB setup → env prep → build event data → checks.

Entry point: build_env()
  Called once before Flask starts. Safe to re-run (all steps are idempotent).
"""

from pipeline.db    import setup_db
from pipeline.check import run_checks, build_playback_events


def build_env(app) -> None:
    """Prepare all data and build event timesteps. Call before app.run()."""
    print("=== Setting up database ===")
    setup_db(app)

    print("=== Preparing environment ===")
    from pipeline.env import prepare_all_events
    prepare_all_events(app)

    print("=== Building event data ===")
    with app.app_context():
        build_playback_events()

    print("=== Environment check ===")
    run_checks(app)

    print("=== Ready ===")
