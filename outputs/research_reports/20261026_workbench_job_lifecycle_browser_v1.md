# Workbench job lifecycle browser evidence

An isolated local workbench run covered submit, queued refresh, local worker completion, completed refresh, and result download links for one factor-research job. The page displayed the lifecycle stages `submitted -> compute -> result -> download` and kept `execution_admitted=false`; recovery remains manual resubmit only.

The result page rendered links for `REPORT.md`, `summary.csv`, `daily_ic.csv`, `groups.csv`, `factors.parquet`, and `result.json`. No broker, Tonghuashun, order, fill, or account action occurred.

This is local seeded research UI evidence, not current-market or account-channel evidence.
