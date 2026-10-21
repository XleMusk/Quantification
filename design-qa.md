# Terminal workbench design QA — 2026-10-21

Status: **passed** for desktop visual fidelity and the client-side drawing/replay workflow.

## Reference and comparison

- Selected concept: `/workspace/scratch/e302e4229037/generated_images/exec-040e0814-a2da-49be-9008-ec85246a78a6.png`.
- Browser: Chrome, viewport 1363 × 936, local compiled preview at `http://terminal.local:4173/`.
- Final overview: `/workspace/scratch/quant-terminal-final-1792559840303.jpg`.
- Final chart review: `/workspace/scratch/quant-review-verified-1792560304720.jpg`.
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

## Verification boundary

Browser tests used the compiled production frontend plus a read-only mirror of 144 responses recorded from actual FastAPI TestClient GET requests against isolated synthetic state. Non-GET requests return 405 in that temporary preview. This verifies visual/client interactions, not browser-to-live-API writes. The real FastAPI backend separately passed all 24 `test_webapp.py` tests, including four new snapshot endpoint cases; the final TypeScript/Vite production build passed. Existing large ECharts/Trading chunk warnings remain.

Windows desktop startup, the user's external data/account directories, mobile viewport behavior, real-time data and Tonghuashun client integration were not verified in this environment. Real order submission remains disabled. No external site deployment was performed.
