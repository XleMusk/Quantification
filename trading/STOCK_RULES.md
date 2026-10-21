# SH/SZ Stock Replay Rules

The shared `rules -> engine -> SimulatedBrokerAdapter -> Ledger` path now derives
the board from the canonical stock code. This is a 2024+ established ordinary
equity, daily-opening-auction approximation, not full exchange simulation.

| Board | Code | Normal daily band | Limit-order quantity |
| --- | --- | --- | --- |
| Main | 60xxxx.SSE / 00xxxx.SZSE | 10% | 100-share buy steps; max 1,000,000 |
| ChiNext | 30xxxx.SZSE | 20%, including ST | 100-share buy steps; max 300,000 |
| STAR | 688xxx.SSE | 20%, including ST | min 200; one-share steps; max 100,000 |

Main-board ST uses 5% before 2026-07-06 and 10% from that date. STAR sell
orders below 200 shares must exit the full balance. Main/ChiNext odd-lot
sell orders must exit the full balance. A valid submitted order can partially
fill below the submission minimum; fills still use the same costs and T+1 lots.
Larger targets are capped per order, not split into invented same-session fills.

IPO first-five-session orders are blocked rather than given a normal band.
Missing or inadequate listing/calendar evidence is blocked. Relisting and first
delisting-period sessions, intraday price cages, market orders, after-hours orders
and fractional corporate-action entitlements are not implemented. Missing data
or unresolved delisted holdings do not receive fabricated fills or settlement.
ST buys remain blocked. ETF/BSE/CDR are not admitted by this stock rule profile.

Nonfixed snapshots may include all fully acquired SH/SZ ordinary stock boards,
but acquisition order is not a point-in-time or representative strategy universe.
This change does not broaden THS's main-board-only prefill scope, establish
account permissions, or enable submission/cancellation. Raw prices execute;
adjusted prices are only research features.

## Explicit Historical Fund Profiles

The same Engine/Ledger now supports reviewed 2024-2025 secondary-market
profiles for `511010.SSE` (bond ETF) and `518880.SSE` (gold ETF): 100-unit
buy steps, CNY0.001 price ticks,10% daily limits, same-day resale after a
confirmed simulated purchase. Ordinary stocks and equity ETFs remain T+1.
No type is inferred from a fund-code prefix. Missing/wrong profiles or dates
outside the reviewed span are rejected; cross-border funds remain unsupported.

`ExecutionEvent` V1 keeps its existing next-day contract. Explicit same-day
resale uses V2 and is checked against the ledger's pinned instrument profile.
Saved fills retain `available_day`; duplicate fills cannot change that day.
This field controls share sellability, not cash settlement or withdrawal.
Daily EOD signals still execute at the next session, not the signal day's close.
There is no primary creation/redemption, leverage/repo or intraday strategy path.

Issuer cash distributions reuse registration/ex-date/payment-date accounting;
ETF costs reuse the explicit all-in commission assumption, not stock stamp/
transfer charges or a verified broker tariff. The `equity_etf` fee keyword is
retained for old callers and now selects the fund-cost path, not fund type.
This is historical replay support, not native THS ETF prefill or paper admission.

Historical basis: original SSE2023rules clauses3.1.4-3.1.5,3.3.8-3.3.13,
read from the [regulated-broker mirror](https://htfc.com/wz_upload/png_upload/20241129/1732871659448145356.pdf).
The dated [gold ETF rules](https://www.sse.com.cn/lawandrules/sselawsrules2025/fund/trading/c/c_20250519_10779388.shtml)
and [bond ETF rules](https://www.sse.com.cn/lawandrules/sselawsrules2025/fund/trading/c/c_20250606_10781071.shtml)
corroborate same-day secondary-market resale. The2023rule PDF is pinned with
the issuer sources in the new mixed snapshot, not labelled an official-host download.

Pre-2024 rights issues and no-holder-entitlement restructurings can explain
warmup ex-prices with archived company announcements and formula parameters.
They are price evidence only, not free-share/dividend ledger entries; adjustments
at or after replay start remain rejected by this supplement path. Cash dividends
continue through the existing entitlement/payment path, using actual per-share
cash rather than the diluted ex-price reference used by the exchange.

## Primary References

Checked 2026-09-30, including the SSE deferred-provisions attachment. Its listed
deferrals concern block trades, not the quantity/price-band clauses used here.

- [SSE 2026 rules notice, effective July 6](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml):
  attached rules 3.3.8-3.3.9, 3.3.13-3.3.17, 6.6-6.7.
- [SSE official STAR FAQ](https://edu.sse.com.cn/tib/ysptj/c/4768085.shtml):
  pre-2026 quantity steps/minimum and price-band corroboration.
- [SZSE 2026 official rules](https://docs.static.szse.cn/www/lawrules/rule/trade/W020260424690713155663.pdf):
  3.3.8-3.3.9, 3.3.13, 3.3.15-3.3.17.
- [SZSE ChiNext rule transition](https://www.szse.cn/aboutus/trends/news/t20200821_580924.html):
  20% regime from 2020-08-24; 100-share buy steps and 300,000-share limit orders.

Tests cover these quantities, ST bands, calendar-counted IPO blocking, STAR
partial fills/T+1, multi-board targets in the actual engine, and snapshot scope.
They establish software behavior, not strategy profitability or source accuracy.
