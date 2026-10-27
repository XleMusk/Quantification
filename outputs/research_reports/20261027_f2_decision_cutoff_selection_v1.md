# F2 exact decision-time source selection (2026-10-27)

The shared F2 source selector now accepts an explicit offset-aware `cutoff_time`. F2 receipts and the Wayback probe pass `decision_time(decision_day)`, currently `15:05:00+08:00`, instead of comparing only the calendar date. A capture on the same day after the decision cutoff is therefore excluded from `eligible` while remaining visible as observed evidence.

The real `000002.SZSE/2023Q1` Wayback probe still has one capture at `2025-09-05T23:50:23+00:00`, after the `2024-05-01` decision day; the new selection receipt is `BLOCKED_DATA` with zero eligible captures. No PIT fact, factor, target, order or account replay was admitted. Commit: `29ee086`; selection/Wayback/dispatch tests passed 34.

The next required evidence is a pre-decision historical capture with revision identity and source bytes. The cutoff change closes a timing ambiguity; it does not create historical availability evidence.
