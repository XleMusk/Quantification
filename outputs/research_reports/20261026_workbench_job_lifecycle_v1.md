# Workbench job lifecycle delivery

The workbench now exposes a read-only lifecycle view for each factor job. The view binds task submission, compute progress, result availability and downloadable artifacts, and reports manual recovery for interrupted/failed/cancelled jobs. It never retries automatically and keeps `execution_admitted=false`.

Validation: backend `22 passed`; frontend `npm.cmd run build` passed with the existing Vite chunk-size warning.
