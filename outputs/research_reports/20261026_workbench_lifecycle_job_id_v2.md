# 2026-10-26 工作台任务生命周期 job_id 绑定

工作台生命周期接口现在为 `submitted → compute → result → download` 的每个阶段返回同一个 `job_id`，前端 `JobLifecycle` 类型与后端响应一致。这样刷新或重连时，阶段标签不会脱离实际任务而显示成另一条任务的状态。

验收：`webapp/backend/tests/test_webapp.py` 28 passed；排队态和完成态都检查四个阶段的 `job_id` 集合只有当前任务 ID。工作台仍为本地 research-only，`execution_admitted=false`，没有券商、同花顺或真实账户交互。

回执：`outputs/pipeline_receipts/20261026_workbench_lifecycle_job_id_v2.json`。
