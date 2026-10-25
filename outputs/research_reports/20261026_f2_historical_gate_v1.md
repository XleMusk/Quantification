# F2 historical evidence gate

The captured 000002.SZSE/2023Q1 PDF is classified as `current_network_fetch`. Its current retrieval hash and issuer identity do not prove that the same artifact was retrievable/as-of before the 2024-05-01 decision cutoff, nor do they prove a revision history.

Result: `BLOCKED_DATA`; no factor, target, order, fill or account action. The contract accepts future `historical_archive`, `provider_revision`, or `source_snapshot` adapters only when retrieval/as-of, revision identity, source provenance and publication timing all pass.
