# F2 additional Wayback issuer probes (2026-10-27)

Two existing F01 issuer URLs were probed once through the Wayback CDX path: 600004.SSE/2024Q1 and 688008.SSE/2024Q1, both with the 2024-05-01 decision cutoff. Neither URL returned a CDX capture. Both receipts therefore remain `BLOCKED_DATA`, with zero observed and eligible captures, `pit_asof_safe=false`, and no factor or account replay.

Receipts: `20261027_f2_wayback_600004_2024q1_v1.json` and `20261027_f2_wayback_688008_2024q1_v1.json`; combined summary: `20261027_f2_wayback_two_issuer_probe_v1.json`.

This is a bounded failed experiment. It does not turn current issuer PDFs into historical PIT evidence and does not block the independent code, paper, or strategy-comparison routes.
