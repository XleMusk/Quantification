# Terminal workbench design QA — 2026-10-21

## Batches 2–4: saved research, minute review and execution evidence

The same compact terminal now includes saved research A/B comparison and Ledger portfolio concentration, per-day frozen paper-market versions, bounded StockDB minute capture, 1/5/15/30/60-minute linked review, session/anchored VWAP, disposable minute execution diagnostics, saved Tonghuashun observations, explicit-reference execution-cost CSV calculations, and independent daily operations status. No live order submission was enabled.

Final validation used the same Chrome viewport and read-only compiled preview, refreshed from 308 actual isolated FastAPI GET responses. Browser interactions verified complete-result filtering and paired A/B selection, per-day paper chart selection and overlays, saved-observation empty states, CSV-header download, minute period/link selection, Shanghai anchor entry, replay start/scrub/next/exit, hidden future-anchor output, and JSON export. The 09:43 anchor returned 24.711 at the final cutoff; session VWAP returned 24.633. At the first two revealed minutes the future anchor remained unavailable. No relevant application warnings/errors were logged. Mobile rendering and browser-to-live-API POSTs were not tested.

A separate actual FastAPI POST using synthetic cent-aligned minute bars filled 100 shares at 17.58 in the first fully eligible interval ending 09:32, charged 5.02, and marked shares available next session, 2025-09-16. Saved accounts and the original chart fixture remained unchanged. Off-tick synthetic bars returned unavailable. The actual cost endpoint independently matched synthetic buy/sell references, reporting adverse slippage 30/20 bps, respectively, with actual supplied fees kept separate; saved accounts were unchanged. These are software fixtures, not market or strategy evidence.

Final checks: 191 backend/account/merged-close-path tests, 66 charting tests, TypeScript/Vite production build, and git diff whitespace check passed. Existing large-bundle and test-client deprecation warnings remain. Regression coverage includes per-arm portfolio/cost settings, missing reference data, earlier-cutoff causality, same-book T+1/cash/fees, interrupted minute jobs, truncated paper calendars and changes to the captured execution implementation.

Screenshots: `/workspace/scratch/quant-research-workspace-1792574499970.jpg` and `/workspace/scratch/quant-minute-workbench-final-1792575077401.jpg`. Both display clearly named synthetic fixtures. Final Desktop Commander device check remained offline (last seen about two hours earlier), so installation on Windows, installed-provider minute timestamp/unit semantics, real four-table Tonghuashun observation and actual execution-cost acceptance remain pending. The branch includes the latest main changes through `fc3de5a`; no saved paper generation was reset.

## Batch 1: frozen daily inventory integration

The data center now separates the local StockDB port connection from saved inventory availability. It lists completed StockDB snapshots/bulk inventories and BaoStock raw daily cache views, registers a frozen daily projection, and exposes actual amount, percentage turnover, optional provider fields, units, coverage, source omissions and version identity. Shared dataset selection spans the overview, data center, chart review and factor research, including reload.

Browser validation used Chrome at `http://terminal.local:4173/`, 1363 × 936. The local Edge extension could not open that internal preview (`ERR_BLOCKED_BY_CLIENT`); the Browser skill's cloud preview fallback was used. The displayed acceptance dataset is explicitly synthetic (8 SYN securities, 75 sessions, 596 rows with four real fixture gaps), produced through the actual registry and served as recorded FastAPI GET responses. No Windows data or private account was copied into this preview.

| Check | Result |
| --- | --- |
| Page identity, meaningful content, no framework overlay | Pass |
| Separate offline service and available frozen version | Pass |
| Amount in RMB, turnover rate in percent, extended fields and coverage | Pass |
| Data center → chart uses selected version | Pass |
| Chart version change → research uses that same ID | Pass |
| Reload retains selected acceptance dataset | Pass |
| Relevant application console errors | None; browser-extension metadata errors excluded |
| Browser registration writes and Windows installation | Not verified; registration separately tested with the actual FastAPI API |

Screenshot: `/workspace/scratch/quant-data-center-1792571511800.jpg`. The view shows the dataset selection, canonical daily fields and extended-source preview. Backend checks cover source hashes, completed inventories, large-register vs CSV limits, preserved omissions, read filtering, actual registration API, raw/adjusted amount compatibility, prefix availability, and exact 20-session research warmup. The frontend production build and 56 charting checks passed. Existing large-chunk warnings remain.

The isolated acceptance dataset also completed the actual registration → `POST /api/jobs` → targeted factor worker → saved result → chart-signals flow. The 596 observed input rows align to 600 calendar/security cells with gaps preserved; all three saved factor columns use the registered dataset fingerprint. The frozen bars and extended-field metadata were unchanged. Browser writes remain outside the read-only preview verification. Final backend suite: 59 passed.

Desktop Commander reported the user's Windows device offline during this batch. The running Windows Git version, real inventory registration and local UI startup remain pending; no local project, source service, research account or broker state was changed remotely in this batch.

Status: **passed** for the verified desktop terminal, research toolbox and drawing/replay workflow.

## Reference and comparison

- Selected concept: `/workspace/scratch/e302e4229037/generated_images/exec-040e0814-a2da-49be-9008-ec85246a78a6.png`.
- Browser: Chrome, viewport 1363 × 936, local compiled preview at `http://terminal.local:4173/`.
- Final overview: `/workspace/scratch/quant-terminal-final-1792559840303.jpg`.
- Initial chart review: `/workspace/scratch/quant-review-verified-1792560304720.jpg`.
- Final research toolbox, fixed-snapshot executions and 2R drawing: `/workspace/scratch/quant-chart-workbench-1792565002254.jpg`.
- Side-by-side comparison, both images scaled to equal width: `/workspace/scratch/e302e4229037/ui_refs/terminal_comparison.jpg`.

The charcoal palette, compact top navigation, account strip, three docked columns, thin separators, square controls, tabular numbers, China red/green convention and bottom blotter match the approved direction. The narrower browser viewport intentionally hides the workspace label and clock. No unresolved P0/P1 visual defect remains in this verified desktop view.

The production UI uses API data rather than copying concept values. Benchmark series, live depth, company names and unsupported pretrade check badges are not fabricated. The right pane instead displays selected snapshot OHLC, actual holdings and target weights, with a link to the existing strategy flow. Fixture prices, factor calculations and Engine/Ledger account results are explicitly synthetic.

## Repairs verified in browser

- Removed overlapping drawdown axis labels with a compact chart configuration.
- Corrected the desktop minimum height: footer bottom and document height both equal the 936px viewport height.
- Tightened the right pane and added local scrolling to preserve all status rows.
- Kept the snapshot field label on one line.
- Replaced secure-context-only `crypto.randomUUID` with a compatible local UUID generator.
- Switched chart data/context replacement to a layout effect, with callback suppression and origin-key checks, to clear previous-context geometry before painting.
- Used Blob URLs for PNG export; the initial data-URL download did not produce a browser download event. Retest downloaded a valid 1030 × 610 PNG (94,833 bytes).

## Interaction evidence

- Selected a watchlist security and confirmed matching right-pane OHLC; filtered sell fills and inspected holdings.
- Opened the original data center and strategy/target pages; dark styling and data display remained usable.
- Drew a horizontal line; changed line width; locked, hid, deleted, undid and redid it. Reload restored the line and analysis note.
- Entered replay at 30 / 166 bars, advanced to 31, then played at 8 bars/second and paused at 83. Display cutoff was 2025-05-09 while the source dataset continued to 2025-09-16.
- Confirmed a new replay had no analysis drawings. Drew a two-anchor rectangle and saved a replay note. Reload and explicit “恢复上次回放” restored 83 / 166, rectangle and note, paused; normal analysis still restored its separate horizontal line and note.
- Enabled MA, VOL and MACD. Exported JSON and validated dataset ID, symbol, replay cutoff, note and two rectangle anchors. PNG download decoded successfully.
- Inspected warning/error logs for the final application bundles: no application error or warning. Browser extension metadata errors were external to the app. The initial UUID exception was fixed and did not recur in final-bundle tests.

## Expanded research workbench verification

- 56 indicator definitions (27 native, 29 public-formula extensions) with searchable categories, favourites, editable validated parameters, formula/source disclosure and saved combinations. Only active available studies are computed; no more than 12 selections or six visible study panes.
- Applied trend/risk/benchmark presets. Invalid volatility window 0 was rejected; window 10 and weekly annual factor 52 survived reload. Saved templates disclose their source period and rebase annual factors across D/W/M.
- Restored 30/166 daily observations, switched W/M/D without changing the 2025-02-18 cutoff, advanced to 32/166 and reloaded/restored at 2025-02-20. All periods share the latest session cursor; old period checkpoints select the furthest valid cursor from that same session. New replay sessions remain independent.
- Weekly/monthly bars aggregate only the revealed daily prefix. Crosshair matching uses the corresponding visible calendar bucket. W/M drawing points use stable indices and are reapplied after data changes; cloned points prevent library mutation of saved geometry/history. Prefix, gap, ISO-year and checkpoint migration regressions are covered.
- Selected a comparison security from the same research snapshot and enabled the benchmark statistics preset. Selected the saved momentum factor; only the same dataset/price-basis causal factor columns are available.
- Switched to a historical account's pinned raw-price feed. Actual shared-Ledger fills rendered at their saved prices. AVP became available only with genuine saved turnover, plotted nonzero, and retained that field through the chart adapter. Account target weights are separate from executions.
- Drew a three-anchor long plan; the chart displayed 2.00R. The separate risk calculator returned 2.00R, 1,000 shares and RMB1,000 risk for entry 10, stop 9, target 12, equity 100,000 and 1% risk. These are measurements, not orders.
- Exported weekly replay JSON v2 and checked source, period, 2025-02-19 cutoff and index-based points. Final PNG export decoded as 940 × 590. The final application bundle produced no application warning/error; extension metadata failures were excluded.

## Verification boundary

Browser tests used the compiled production frontend plus a read-only mirror of 248 responses recorded from actual FastAPI TestClient GET requests against isolated synthetic state. Non-GET requests return 405 in that temporary preview. This verifies visual/client interactions, not browser-to-live-API writes. The real FastAPI backend separately passed 31 chart/webapp API tests, including seven new full-history/factor/pinned-ledger cases; 56 frontend charting tests and the final TypeScript/Vite production build passed. Existing large ECharts/Trading chunk warnings remain.

Minute candles and session/anchored VWAP were added in batches 2–4 above. Volume-at-price/POC/value areas, order flow, Pine execution and server-side annotation sync remain unavailable. Legacy paper accounts without per-day market versions and unmatched broker observations cannot present a frozen matched chart. Daily OHLCV is never converted into a purported actual price-level distribution.

Windows desktop startup, the user's external data/account directories, mobile viewport behavior, real-time data and Tonghuashun client integration were not verified in this environment. Real order submission remains disabled. No external site deployment was performed.
