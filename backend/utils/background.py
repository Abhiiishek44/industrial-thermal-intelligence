"""
utils/background.py
-------------------
Lightweight background task runner using daemon threads.
"""
import threading


def run_in_background(fn, *args):
    """Run fn(*args) in a daemon thread. Returns immediately."""
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
