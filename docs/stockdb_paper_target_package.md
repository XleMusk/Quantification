# StockDB 纸面目标与订单包

正式入口复用 `trading.ledger.Ledger`、`trading.routing.route_order` 和 `trading.engine.shares_for_target`，输入已有 signal-frame target、raw RMB quote observation 和独立初始现金假设。

```powershell
D:\Quantification\env\python.exe D:\Quantification\run_committee.py stockdb-paper-target-package --target target.json --quotes quotes.json --initial-cash 100000 --execution-day 2026-10-14 --settlement-day 2026-10-15 --output-dir output
```

不传 `--output-dir` 时仅计算并打印结果；传入后输出 `target_holdings.csv`、`order_plan.csv`、`ths_prefill_payloads.csv`、`projected_account.json`、`result.json`。Python 调用入口为 `build_paper_target_package`。

目标必须显式 `research_only=true`、`execution_admitted=false`；报价必须是 `ths_raw_quote_observation_v1`、`price_basis=raw_rmb`，并完整覆盖目标。采用 80% 总仓位和 20% 单票上限；不足最低股数或现金时报告错误。成交金额、手续费和 T+1 库存均为内存 Ledger 投影。

`execution_admitted=false` 不能通过参数开启。路由受阻的标的仍保留在纸面研究目标中，其 THS 数量、价格和代码字段留空。该入口不会观察账户、打开 THS、填表、提交、撤单、转账或持久化账户。执行日和下一可卖日期需调用者明确提供，此命令不验证交易所日历或授予交易资格。

原 `outputs/stockdb_paper_target_packages/20261014_factor190_100k_1205/build_package.py` 保留为历史输出脚本，新的纸面包应使用正式入口。
