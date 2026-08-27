"""
pipeline/
---------
Full startup sequence: DB setup → env prep/slot creation → checks.

Entry point: build_env()
  Called once before Flask starts. Safe to re-run (all steps are idempotent).
"""

def build_env(app) -> None:
    """Prepare all data and build event timesteps. Call before app.run()."""
    from pipeline.db import setup_db
    from pipeline.check import run_checks

    print("=== Setting up database ===")
    setup_db(app)

    print("=== Preparing environment ===")
    from pipeline.env import prepare_all_events
    prepare_all_events(app)

    print("=== Environment check ===")
    run_checks(app)

    print("=== Ready ===")
