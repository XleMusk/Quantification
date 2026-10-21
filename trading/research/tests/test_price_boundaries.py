from decimal import Decimal

import pandas as pd
import pytest

from trading.research.price_boundaries import (
    EFFECTIVE, NEW, OLD, RENAME_SHA256, alias_levels, canonicalize, initial_level,
)
from trading.research.tests.test_adjusted_prices import factor_frame, raw_frame


def initial_inputs():
    raw = raw_frame()
    raw.at[1, "preclose"] = raw.close.iloc[0]
    factors = factor_frame().iloc[1:].copy()
    factors[["backAdjustFactor", "adjustFactor"]] = "1.000000"
    factors["foreAdjustFactor"] = "0.730000"
    return raw, factors


def rename():
    return {"old": OLD, "new": NEW, "effective_date": EFFECTIVE, "published_date": "2025-02-14",
            "disclosure_sha256": RENAME_SHA256, "share_count_unchanged_on_rename": True,
            "pre_rename_forward_scale": "0.986265"}


def test_initial_bridge_uses_observed_nonunit_scale_without_source_mutation():
    raw, factors = initial_inputs()
    before_raw, before_factors = raw.copy(deep=True), factors.copy(deep=True)
    row, proof = initial_level(raw, factors, raw.date.iloc[0], raw.date.tolist())
    assert row["foreAdjustFactor"] == "0.730000"
    assert row["dividOperateDate"] == "2017-05-24" and row["source_factor_date"] == "2017-05-25"
    assert row["backAdjustFactor"] == row["adjustFactor"] == ""
    assert proof["derived_rows"] == 1
    assert raw.equals(before_raw) and factors.equals(before_factors)


@pytest.mark.parametrize("broken", ["calendar", "listing", "action", "back", "inactive", "empty"])
def test_initial_bridge_rejects_unproved_boundaries(broken):
    raw, factors = initial_inputs()
    ipo, calendar = raw.date.iloc[0], raw.date.tolist()
    if broken == "calendar":
        calendar = calendar[1:]
    elif broken == "listing":
        ipo = "2017-05-23"
    elif broken == "action":
        raw.at[1, "preclose"] = "9"
    elif broken == "back":
        factors["backAdjustFactor"] = "2"
    elif broken == "inactive":
        raw.at[0, "tradestatus"] = "0"
    else:
        raw = raw.iloc[:0]
    with pytest.raises(ValueError):
        initial_level(raw, factors, ipo, calendar)


def alias_inputs():
    old = factor_frame().copy()
    old["code"] = OLD
    new = old.iloc[:1].copy()
    new["code"], new["dividOperateDate"] = NEW, EFFECTIVE
    new[["backAdjustFactor", "adjustFactor"]] = "1"
    new["foreAdjustFactor"] = "0.986265"
    terminal = new.copy()
    terminal["dividOperateDate"], terminal["foreAdjustFactor"] = "2026-06-16", "1"
    new = pd.concat([new, terminal], ignore_index=True)
    raw = raw_frame().copy()
    raw["date"] = ["2025-02-14", EFFECTIVE]
    raw.at[1, "preclose"] = raw.close.iloc[0]
    return old, new, raw.iloc[0], raw.iloc[1]


def test_rename_reanchors_old_levels_not_share_counts_or_source_events():
    old, new, last, first = alias_inputs()
    before = old.copy(deep=True)
    derived = alias_levels(old, new, last, first, RENAME_SHA256)
    assert [Decimal(row["foreAdjustFactor"]) for row in derived] == [
        Decimal("0.759535") * Decimal("0.986265"), Decimal("0.986265")]
    assert all(row["code"] == NEW and row["source_code"] == OLD for row in derived)
    assert all(row["backAdjustFactor"] == row["adjustFactor"] == "" for row in derived)
    assert old.equals(before)


@pytest.mark.parametrize("broken", ["disclosure", "boundary", "duplicate", "zero", "effective"])
def test_rename_rejects_conflicts(broken):
    old, new, last, first = alias_inputs()
    digest = RENAME_SHA256
    if broken == "disclosure":
        digest = "unreviewed"
    elif broken == "boundary":
        first = first.copy()
        first.preclose = "1"
    elif broken == "duplicate":
        old = pd.concat([old, old.iloc[:1]])
    elif broken == "zero":
        old.loc[old.index[0], "foreAdjustFactor"] = "0"
    else:
        new.loc[0, "dividOperateDate"] = "2025-02-18"
    with pytest.raises(ValueError):
        alias_levels(old, new, last, first, digest)


def alias_day(day="2025-02-14"):
    old = raw_frame().iloc[:1].copy()
    old["code"], old["date"] = OLD, day
    new = old.copy()
    new["code"] = NEW
    return pd.concat([old, new], ignore_index=True)


def test_canonical_consumer_retains_both_source_rows_and_one_dated_identity():
    raw = alias_day()
    kept, canonical = canonicalize(raw, raw, rename())
    assert kept.code.tolist() == [OLD, NEW] and kept.alias_shadow.tolist() == [True, False]
    assert canonical.code.tolist() == [NEW]
    assert canonical.security_id.tolist() == canonical.tradable_code.tolist() == [OLD]
    raw = alias_day(EFFECTIVE)
    raw.loc[0, ["open", "high", "low", "close", "preclose", "volume", "amount"]] = ""
    raw.at[0, "tradestatus"] = "0"
    kept, canonical = canonicalize(raw, raw, rename())
    assert kept.iloc[0].close == "" and len(kept) == 2
    assert canonical.tradable_code.tolist() == [NEW] and canonical.security_id.tolist() == [OLD]


@pytest.mark.parametrize("broken", ["price", "activity", "missing", "effective_active", "late_old"])
def test_canonical_consumer_never_silently_drops_conflicting_aliases(broken):
    raw = alias_day()
    if broken in {"price", "activity"}:
        raw.at[1, "close" if broken == "price" else "amount"] = "1"
    elif broken == "missing":
        raw = raw.iloc[:1]
    else:
        raw["date"] = EFFECTIVE if broken == "effective_active" else "2025-02-18"
    with pytest.raises(ValueError):
        canonicalize(raw, raw, rename())


def test_project_route_only_dispatches_boundary_builder(monkeypatch):
    import run_committee
    from trading.research import price_boundaries
    calls = []
    monkeypatch.setattr(price_boundaries, "main", lambda args: calls.append(args) or 0)
    assert run_committee.main(["research-price-boundaries", "--coverage", "local"]) == 0
    assert calls == [["--coverage", "local"]]
