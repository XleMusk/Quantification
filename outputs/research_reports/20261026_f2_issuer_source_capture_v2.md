# F2 issuer source capture v2: semantic extraction boundary

The locked `000002.SZSE / 2023Q1` CNINFO artifact was re-captured with a semantic extraction gate. `pypdf` output is accepted only when the required Chinese financial field labels are present; otherwise the adapter falls back to Poppler text extraction. This prevents ticker/year digits in mojibake from being treated as valid field evidence.

| Field | Result |
|---|---|
| Symbol / period | `000002.SZSE` / `2023Q1` |
| Source identity | CNINFO PDF, HTTP 200, saved/response SHA-256 match |
| Text engine | `pypdf` after semantic field-label check |
| Scope mapping | verified for parent-attributable net profit and consolidated point-in-time total assets |
| Historical retrieval/as-of | unproven |
| Revision history | unproven |
| Status | `BLOCKED_DATA` |

## Decision

The semantic field contract is now verified locally and the extracted candidates remain blocked from F2 admission because the current retrieval is after the `2024-05-01` decision cutoff and no historical retrieval/as-of or revision payload is available. No factor, target, order, fill, account or execution permission was created.

## Evidence

- Receipt: `outputs/pipeline_receipts/20261026_f2_issuer_source_capture_v2.json`
- Local artifact: `D:\QuantificationData\trading\research\financials\20261026_f2_issuer_2023q1\000002.SZSE_2023Q1.pdf`
- Focused tests: `D:\Quantification\env\python.exe -m pytest -q trading/research/tests/test_f2_issuer_source_evidence.py trading/research/tests/test_f2_source_snapshot.py trading/research/tests/test_f2_fact_bridge.py trading/research/tests/test_publication_event_contract.py` -> `24 passed`

`execution_admitted=false`; no broker, THS or real account channel was touched.
