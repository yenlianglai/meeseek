"""Adapters: the only code that knows a framework or a vendor SDK by name.

    adk/   Google ADK — the turn loop, sessions, agents and plugins meeseek
           runs on today. Nothing outside this package imports google.adk
           (tests/unit/test_boundaries.py); S3 replaces it with meeseek's own
           turn runner behind the same seams.
"""
