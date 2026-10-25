# D0/D1 frozen plan binding

The D0/D1 contract now records the local publication time of the frozen plan separately from the StockDB quote observation and decision cutoff. When the fixed leg comes from a registered `fixed_daily` run, the receipt and target/quote manifest also carry the committed account generation and last completed day as an `account_version`.

This keeps a direct terminal payload usable for research while preventing a persistent account continuation from being mistaken for a fresh initial-cash projection. The timestamp is local artifact publication only; it is not a broker or Tonghuashun submission event.

Validation: `D:\Quantification\env\python.exe -m pytest -q trading/research/tests/test_next_close_d0_d1.py` -> 15 passed.

Execution remains closed (`execution_admitted=false`, no broker/THS interaction).
