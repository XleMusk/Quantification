# Daily refresh command-policy refactor (2026-10-27)

The close-day supervisor kept a long route-specific condition chain in `system_daily_refresh.py`. The refactor moves those constraints into `daily_refresh_policy.py` as immutable `RoutePolicy` entries. The supervisor still owns orchestration and persisted state; the policy module only normalizes a saved command, validates its allowed route and fixed arguments, and materializes the existing local command.

The change preserves command argv and the existing research-only boundary. It rejects `--live`, `--submit`, `--train`, `--refit`, and `--save-next`; keeps `seat-collect` at one exact day and three pages; and keeps native PAPER limited to an explicit closing session. No Engine/Ledger, fees, strategy specification, account state, broker, or Tonghuashun behavior changed.

Acceptance: `test_daily_refresh_policy.py`, `test_system_daily_refresh.py`, `test_system_daily_disclosures.py`, and `test_system_router_daily_adapter.py` passed (`16 passed`); `py_compile` passed. A separate registered-routes test still exposes an existing synthetic fixture that omits `catalog`; the production function and fixture were outside this structural slice and the failure is retained in the receipt.

Next delivery follows the Google Applied Design waves: real F2 PIT evidence, then the next complete close through fixed_daily and D0/D1, then one more thin application use case. Large framework migrations and whole-repository formatting remain out of scope until a measured consumer requires them.
