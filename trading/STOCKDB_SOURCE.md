# Local StockDB candidate source

The user-supplied StockDB v0.3.5 bundle is at
`C:\Users\13944\Desktop\free-stockdb-windows-v0.3.5-more-power\stockdb`.
Its existing process serves read-only queries on `127.0.0.1:7899`. The
`stockdb-audit` command uses the HTTP/MessagePack interface, not the vendor
Python extension, updater, MCP server or online API.
The project interpreter has `msgpack==1.1.1` for decoding this response;
reinstall it with `uv pip install --python .\env\python.exe msgpack==1.1.1` if
the project environment is rebuilt.

```powershell
.\env\python.exe -X utf8 run_committee.py stockdb-audit --out outputs\stockdb_source\local_audit.json
```

The report inventories the code catalog and sync manifests, compares selected
daily bars against the existing BaoStock raw cache, and checks one exact minute
bar per sampled symbol. The code catalog
includes non-A-share instruments; its size is not an A-share coverage claim.
The local sample has raw OHLC but `pre_close` appears to use an adjusted basis
on historical dates,
and occasional volume differences. An exact 2026-09-24 15:00 minute record
exists for 600519 but not 000001; this does not establish broader coverage.
Do not map `pre_close` to the execution
feed or use it for raw daily returns. Financial fields, minute bars, corporate
actions, historical universe membership and licensing remain unvalidated.

This source stays separate from `HistoricalFeed`, the order ledger and live
trading. `execution_admitted` is always false in the audit output.

For a current-day coverage gate, query the already-running service directly
with the read-only receipt command:

```powershell
.\env\python.exe .\run_committee.py stockdb-live-coverage `
  --date 2026-10-13 --date 2026-10-14 `
  --out outputs\stockdb_live_coverage\<run>
```

The receipt distinguishes a complete current-day sample from a stale or
unavailable service. A live response is diagnostic only; it does not promote
the service into the frozen research snapshot.
