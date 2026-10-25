# Exchange calendar boundary delivery

The year-scoped SSE calendar now returns an explicit unresolved result when the next session would cross the artifact's covered year. It no longer manufactures a 2027 session from a 2026-only calendar.

Validation: `D:\Quantification\env\python.exe -m pytest -q trading/research/tests/test_exchange_calendar.py` -> 2 passed.

This is an offline scheduling correction only. It does not fetch live calendar data, advance a paper account, or change `execution_admitted=false`.
