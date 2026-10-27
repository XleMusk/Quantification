# Operator strategy evidence application view v1

The operator evidence route now delegates to `trading/research/operator_evidence_view.py`. The application view reads the saved evidence card, promotion review, strategy board, optional robustness diagnostic and explainability artifacts. HTTP keeps the existing missing-artifact and invalid-artifact status mapping.

Validation: `41 passed` across the new view tests and the existing operator-package tests. No research rerun, promotion, account mutation, broker access or THS interaction occurred; `execution_admitted=false` remains in force.

Receipt: `outputs/pipeline_receipts/20261027_operator_evidence_usecase_v1.json`.
