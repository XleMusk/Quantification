# F2 issuer source capture: 000002.SZSE / 2023Q1

The locked target period now has a verified public CNINFO PDF artifact and a content-hash match on a fresh retrieval.

| Field | Result |
|---|---|
| Symbol / period | `000002.SZSE` / `2023Q1` |
| Issuer source | https://static.cninfo.com.cn/finalpage/2023-04-29/1216686497.PDF |
| Publication date | `2023-04-29` |
| Saved SHA-256 | `0be2f3d7585e85a33e4acaa62a99caa8667ef65ba2678ee23e393c4ff638fd72` |
| Retrieval status | `HTTP 200`, hash match `true` |
| Identity checks | title, code, period text and RMB text all present |
| Candidate fields | parent net profit `1,445,811,341.87` yuan; total assets `1,743,078,594,535.06` yuan |

## Decision

`BLOCKED_DATA` remains correct. The retrieval happened on 2026-10-26, after the locked 2024-05-01 decision cutoff, so it proves current access and artifact identity but does not prove historical retrieval/as-of availability. The server `Last-Modified` date is retained as transport metadata, not as revision history. Issuer scope mapping and revision history remain unresolved; no F2 fact, factor, target, order, fill or account replay was created.

## Evidence

- Receipt: `outputs/pipeline_receipts/20261026_f2_issuer_source_capture_v1.json`
- Local artifact: `D:\QuantificationData\trading\research\financials\20261026_f2_issuer_2023q1\000002.SZSE_2023Q1.pdf`
- Tests: `D:\Quantification\env\python.exe -m pytest -q trading/research/tests/test_f2_issuer_source_evidence.py` -> 2 passed.

execution_admitted=false; live account and THS channels were not touched.
