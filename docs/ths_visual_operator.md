# 同花顺单笔视觉操作卡

`trading.ths.visual_operator` 消费现有 `THSAdapter` 的组合预览，只展示该预览的
唯一 `next_plan`，复用账户/计划时效校验、三字段格式、GUI profile 状态及回读核对。
它不把研究纸面包变为可填单，不建订单、不存计划、不访问窗口、不写 Ledger。

```powershell
D:\Quantification\env\python.exe D:\Quantification\run_committee.py ths-visual-operator-card `
  --preview <prepare-handoff或plan-target生成的组合预览.json> `
  --profile <本机GUI映射.json> --out <visual_operator_card.json>
```

可追加 `--readback-json <symbol-price-qty回读.json>`。输入回读只是调用者提供的
JSON，不能代替当前窗口观察。计划过期、profile 未验证、账户不一致或回读错误
会显示阻碍。其余调仓只显示差额，不提供后续可写字段。

操作卡按“可见账户/方向/限价模式 → 当前三字段 → 回读 → 人工检查点 →
委托/成交/费用/账户刷新”组织，所有步骤 `execution_enabled=false`。
`write_allowed=false`、`submission_enabled=false`、`execution_admitted=false`
始终保留；卡片不会授权操作或自动进入下一笔。后续计划由原有单笔对账和
组合消费者重新计算。

CLI 对研究包或不兼容输入输出 `blocked_input` 回执并返回非零退出码，
回执不含证券、价格、数量字段。它不会把纸面研究结果当作可操作计划。
