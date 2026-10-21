# Institutional trader function matrix

This is a practical comparison of functions exposed by institutional buy-side
OMS/EMS products and the local A-share single-ledger path. It is a capability
map, not a claim that a personal THS account has institutional connectivity.

## Official reference set

- [Bloomberg AIM](https://professional.bloomberg.com/products/trading/order-management-system/aim/): portfolio management, order modeling, rebalancing, pre/post
  trade compliance, matching, settlement, reconciliation and portfolio
  accounting.
- [Bloomberg execution management](https://professional.bloomberg.com/products/trading/execution-management-system/): order and execution workflow across the
  trading lifecycle.
- [FlexTRADER](https://flextrade.com/products/flextrader-execution-management-system/): broker-neutral EMS, portfolio and basket trading, broker
  connectivity, real-time market data and workflow automation.
- [FlexTCA](https://flextrade.com/products/flextca/) / FlexAlgoWheel: pre/post execution analysis, broker/algo selection
  and feedback from execution results.

## Local restoration map

| Function | Institutional analogue | Local state | Opening-session use | Next concrete step |
|---|---|---|---|---|
| Portfolio rebalance | Bloomberg AIM | paper_ready | after account + quotes | observed-account sizing |
| EMS blotter lifecycle | Bloomberg EMS / FlexTRADER | paper_ready | paper only | order/trade observations |
| Lifecycle recovery | OMS/EMS exception workflow | paper_recovery_defined | manual recovery | broker-state receipt |
| Pre-trade risk/compliance | AIM / FlexTRADER | paper_preflight_only | after account + quotes | account-sized checks |
| Basket execution schedule | FlexTRADER portfolio scheduler | paper_manual_sequence | manual sequence | quote-aware schedule |
| Release controls | Bloomberg rule builder | paper_controls_defined | manual review | clock/quote triggers |
| Child-order algorithms | EMS TWAP/VWAP/POV | paper_twap_schedule_ready | paper only | live market and child-fill feedback |
| Portfolio what-if | AIM scenario analysis | paper_scenario_only | after account + quotes | observed-account scenarios |
| Broker routing | EMS / FlexLINK | single_manual_ths_path | manual THS only | authorized read-only mapping |
| Real-time market data | Bloomberg market data | missing_for_open | required input | same-session raw-RMB import |
| Transaction-cost analysis | FlexTCA | pretrade_estimate_only | pre-trade only | fill benchmark |
| Post-trade reconciliation | AIM operations | code_ready_readonly | after manual trade | account/orders/trades capture |
| Allocation and settlement | AIM operations | ledger_ready_paper_only | paper only | contract and cashflow binding |
| Audit trail and alerts | OMS/EMS lifecycle controls | paper_alerts_ready | paper only | live acknowledgement/fill alerts |
| Strategy explainability | portfolio decision support | historical_diagnostic_ready | review only | validated current-EOD bundle |
| Kill switch | pre-trade risk control | submission_disabled | enabled | keep automatic submission disabled |

## Current operator artifacts

- Latest desk matrix: `outputs/trader_desk_snapshots/20261007_152000_alerts/function_matrix.csv`
- Latest paper blotter: `outputs/trader_desk_snapshots/20261007_152000_alerts/blotter.csv`
- Latest paper exception alerts: `outputs/trader_desk_snapshots/20261007_152000_alerts/exception_alerts.csv`
- Latest paper child-order schedule: `outputs/trader_desk_snapshots/20261007_152000_alerts/child_order_schedule.csv`
- Latest recovery plan: `outputs/trader_desk_snapshots/20261007_152000_alerts/recovery_plan.csv`
- Latest strategy explainability diagnostic: `outputs/trader_desk_snapshots/20261007_152000_alerts/strategy_explainability.json`
- Manual refresh script: `scripts/run_manual_opening_review.ps1`

The only inputs still needed for an account-sized opening preview are a fresh
normalized account observation and same-session raw-RMB quotes. Historical
prices are deliberately not promoted to current order prices. THS submission
and cancellation remain disabled; a manual user decision is required even
after a future prefill/readback observation.
