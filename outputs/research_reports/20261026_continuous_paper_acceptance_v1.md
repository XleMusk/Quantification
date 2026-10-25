# Continuous paper continuity acceptance

This local fixture runs the shared Ledger across two adjacent sessions. The first session publishes one target and records a partial fill; the second session reloads the serialized state, carries cash and the unsettled lot forward, and changes only the target-day metadata. Repeating the same order/fill identifiers is idempotent and does not create a second fill.

The receipt is `VERIFIED_LOCAL_RESEARCH_ONLY` and synthetic. It does not prove StockDB availability, forward market operation, BrokerAdapter/THS evidence or live execution.
