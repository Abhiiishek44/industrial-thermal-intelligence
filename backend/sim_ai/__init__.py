"""
sim_ai — GIS-informed field report simulator
=============================================
Self-contained module: prompt / geospatial context / LLM generator / Flask route.

Exposes:
    simulate_bp  — Blueprint registered on crowd_bp in api/crowd.py
"""
from sim_ai.routes import simulate_bp  # noqa: F401
