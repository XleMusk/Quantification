# 工作台研究发布绑定（2026-10-26）

本工作包把工作台任务生命周期绑定到当前本地研究发布清单：`GET /api/release` 只读返回最新 `release_manifest.json`，任务生命周期同时返回相同发布版本和提交号，Tasks 页面展示绑定版本。该链路不会创建发布、写入账本、访问同花顺或改变执行准入。

## 验收

- 当前发布：`20261026_v12`，提交 `456a2bced4065719b13968e38d23b77c3d10c198`。
- `execution_admitted=false`，`live_execution_ready=false`，账户/同花顺缺失字段保持原值。
- 后端 `webapp/backend/tests/test_webapp.py`：25 passed。
- 前端 `npm run build`：通过；仅有 Vite chunk-size 提示。
- 回执：`outputs/pipeline_receipts/20261026_workbench_release_binding_v1.json`。
