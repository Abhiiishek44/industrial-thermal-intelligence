"""
reset_timesteps.py
------------------
Resets all 'failed' EventTimestep rows back to 'pending' so the pipeline
will retry them on next startup.

Run from the backend directory:
    python reset_timesteps.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from main import create_app
from db.connection import db
from db.models import EventTimestep

app = create_app()
with app.app_context():
    n_pred = EventTimestep.query.filter_by(prediction_status='failed').update(
        {'prediction_status': 'pending'}
    )
    n_spat = EventTimestep.query.filter_by(spatial_analysis_status='failed').update(
        {'spatial_analysis_status': 'pending'}
    )
    db.session.commit()
    print(f"Reset {n_pred} prediction(s) and {n_spat} spatial analysis rows back to pending.")
