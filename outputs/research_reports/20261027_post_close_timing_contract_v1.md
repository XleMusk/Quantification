# Post-close timing contract (2026-10-27)

Post-close terminal receipts now carry `finished_at` and bounded local `elapsed_seconds`, tied to the existing input fingerprint and execution boundary. This measures orchestration duration only; it is not provider expense, fill quality or strategy evidence. Focused post-close tests: `8 passed`.
