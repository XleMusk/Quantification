# Workbench recovery API boundary

工程范围：`VERIFIED_LOCAL_RESEARCH_ONLY`。

`GET /api/jobs/{job_id}/recovery` 和生命周期响应中的 `recovery_evidence` 现在直接读取持久任务状态，返回 `MANUAL_RESUBMIT_REQUIRED`、错误状态、下一步操作和外部订单边界。接口只读，不重试、不创建新任务、不访问券商或同花顺。

验证：`D:\Quantification\env\python.exe -m pytest -q webapp/backend/tests/test_webapp.py`（23 passed）；前端 `npm run build` 通过。

这份回执使用中断任务 fixture，不能证明真实账户、真实行情或外部渠道。
