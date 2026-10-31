"""meeseek's core: what a turn is, said in plain Python.

    events.py   what happens in a turn, as an append-only stream of events
    tools.py    what a tool is: its schema, and whether it reads or writes

Nothing here does I/O or knows a framework, vendor or store; that is the
ports' and adapters' business (meeseek.ports, meeseek.adapters).
tests/unit/test_boundaries.py holds the line.
"""
