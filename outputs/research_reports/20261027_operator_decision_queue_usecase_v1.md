# Operator decision queue application view v1

The saved strategy-level paper decision queue now has an explicit application view in `trading/research/operator_decision_queue_view.py`. The HTTP route delegates queue reading, attaches the current same-ledger reconciliation views, and preserves recovery handoff rows with `retry_permitted=false`.

Validation: `42 passed` across the new view tests and the existing operator-package tests. No account mutation, order retry, broker access or THS interaction occurred; `execution_admitted=false` remains in force.

Receipt: `outputs/pipeline_receipts/20261027_operator_decision_queue_usecase_v1.json`.
