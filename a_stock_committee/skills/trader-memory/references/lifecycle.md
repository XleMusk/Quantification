# Thesis 生命周期

```text
IDEA -> ENTRY_READY -> ACTIVE -> CLOSED
IDEA -> INVALIDATED
ENTRY_READY -> INVALIDATED
ACTIVE -> CLOSED
```

- IDEA：只有想法，没有交易许可。
- ENTRY_READY：研究、风控、仓位都完成，但仍需人工确认。
- ACTIVE：真实或纸面持仓已打开。
- CLOSED：已退出并记录原因。
- INVALIDATED：买入前逻辑已经失效。

