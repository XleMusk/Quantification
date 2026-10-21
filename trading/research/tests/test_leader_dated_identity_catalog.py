"""Small saved-source tests; no provider, old replay, or account is invoked."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from trading.research import leader_dated_identity_catalog as catalog
from trading.research import leader_identity_registry as registry
from trading.research.tests.test_leader_identity_registry import registered


def clock(value="2026-10-17T10:00:00+08:00"):
    return lambda: datetime.fromisoformat(value)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def dated(tmp_path, day="2026-10-16", *, absent=(), outside=False, unresolved=False):
    make, symbols, anchor = registered(tmp_path, count=3)
    source = make(day, absent=absent)
    value = read(source)
    types = value["analysis"]["archetypes"]
    sample = deepcopy(types[0]["candidates"][0])
    types[0]["candidates"] = [c for c in types[0]["candidates"] if c["symbol"] == symbols[0]]
    types[1]["candidates"] = [c for c in types[1]["candidates"] if c["symbol"] == symbols[1]]
    if outside:
        sample["symbol"] = "002000.SZSE"
        types[1]["candidates"].append(sample)
    if unresolved:
        types[1]["unresolved_stage_symbols"] = [symbols[2]]
    for t in types:
        t["candidate_count"] = len(t["candidates"])
        t["status"] = "matched" if t["candidates"] else "not_matched"
    save(source, value)
    return make, symbols, anchor, source


def resolve(path, day="2026-10-19", asof="2026-10-17T11:00:00+08:00"):
    return catalog.resolve_prior_identity(path, decision_day=day, decision_as_of=asof,
        observed_at="2026-10-17T12:00:00+08:00")


def reseal(folder, value):
    save(folder / "catalog.json", value)
    receipt = read(folder / "receipt.json")
    receipt["catalog_sha256"] = catalog.digest(folder / "catalog.json")
    save(folder / "receipt.json", receipt)


def test_original_scope_daily_labels_and_no_outside_member_or_new_leader_threshold(tmp_path):
    _, symbols, anchor, source = dated(tmp_path, outside=True)
    anchor_before = anchor.read_bytes()
    record = catalog.build_catalog(anchor, [source], tmp_path / "out", _clock=clock())
    entry = record["entries"][0]
    assert record["rule"]["leader_types"] == ["space_height_leader", "relay_continuation_leader"]
    assert [m["symbol"] for m in entry["members"]] == symbols
    assert entry["source_member_outside_fixed_scope_count"] == 1
    assert [m["labels"] for m in entry["members"]] == [["space_height_leader"], ["relay_continuation_leader"], []]
    assert entry["leader_symbols"] == symbols[:2]
    assert entry["members"][2]["leader_membership_status"] == "known_nonleader_proxy"
    value = resolve(tmp_path / "out/catalog.json")
    assert value["status"] == "available" and value["required_prior_day"] == "2026-10-16"
    assert value["members"] == entry["members"] and value["entry"] == entry
    assert value["catalog_reference"]["sha256"] == catalog.digest(tmp_path / "out/catalog.json")
    assert anchor.read_bytes() == anchor_before
    assert value["pit_verified"] is False and value["person_identity_verified"] is False


@pytest.mark.parametrize("absent,unresolved", [(("600000.SSE",), False), ((), True)])
def test_missing_or_saved_unresolved_fixed_member_stays_unknown(tmp_path, absent, unresolved):
    _, symbols, anchor, source = dated(tmp_path, absent=absent, unresolved=unresolved)
    catalog.build_catalog(anchor, [source], tmp_path / "out", _clock=clock())
    result = resolve(tmp_path / "out/catalog.json")
    expected = symbols[0] if absent else symbols[2]
    assert result["status"] == "available" and result["scope_complete"] is False
    assert result["unknown_leader_symbols"] == [expected]
    member = next(m for m in result["members"] if m["symbol"] == expected)
    assert member["leader_membership_status"] == "unknown" and member["labels"] == []


def test_missing_exact_previous_day_never_uses_an_available_neighbor(tmp_path):
    _, _, anchor, source = dated(tmp_path, day="2026-10-15")
    catalog.build_catalog(anchor, [source], tmp_path / "out", _clock=clock())
    result = resolve(tmp_path / "out/catalog.json")
    assert result["status"] == "unknown" and result["required_prior_day"] == "2026-10-16"
    assert result["reason"] == "exact_previous_close_identity_missing"


def test_postwrite_actual_clock_and_no_backfilling_old_16_decision(tmp_path):
    make, _, anchor, source = dated(tmp_path, day="2026-10-15")
    source16 = make("2026-10-16")
    ticks = iter(datetime.fromisoformat(v) for v in [
        "2026-10-17T10:00:00+08:00", "2026-10-17T10:00:01+08:00", "2026-10-17T10:00:03+08:00"])
    record = catalog.build_catalog(anchor, [source, source16], tmp_path / "out", _clock=lambda: next(ticks))
    assert record["entries"][0]["available_at"] == "2026-10-17T10:00:01+08:00"
    early = resolve(tmp_path / "out/catalog.json", asof="2026-10-17T10:00:02+08:00")
    assert early["status"] == "unknown"
    current = resolve(tmp_path / "out/catalog.json")
    assert current["available_at"] == "2026-10-17T10:00:03+08:00"
    old = resolve(tmp_path / "out/catalog.json", day="2026-10-16")
    assert old["status"] == "unknown"  # Even an asof on the 17th cannot rescue old D16.
    assert old["reason"] == "exact_previous_identity_not_available_before_decision"


def test_append_preserves_every_old_source_label_clock_and_effective_availability(tmp_path):
    make, _, anchor, first = dated(tmp_path, day="2026-10-15")
    parent = catalog.build_catalog(anchor, [first], tmp_path / "parent", _clock=clock())
    parent_file = tmp_path / "parent/catalog.json"
    before = parent_file.read_bytes()
    latest = catalog.build_catalog(anchor, [make("2026-10-16")], tmp_path / "child",
        parent=parent_file, _clock=clock("2026-10-17T11:00:00+08:00"))
    assert latest["entries"][:1] == parent["entries"]
    assert parent_file.read_bytes() == before
    loaded = catalog.load_catalog(tmp_path / "child/catalog.json", observed_at="2026-10-17T12:00:00+08:00")
    assert loaded["effective_available_at_by_day"]["2026-10-15"] == "2026-10-17T10:00:00+08:00"
    assert loaded["effective_available_at_by_day"]["2026-10-16"] == "2026-10-17T11:00:00+08:00"


@pytest.mark.parametrize("day", ["2026-10-15", "2026-10-19"])
def test_append_rejects_duplicate_or_skipped_source_before_creating_output(tmp_path, day):
    make, _, anchor, first = dated(tmp_path, day="2026-10-15")
    catalog.build_catalog(anchor, [first], tmp_path / "parent", _clock=clock())
    next_source = first if day == "2026-10-15" else make(day)
    with pytest.raises(ValueError, match="unique|exact next"):
        catalog.build_catalog(anchor, [next_source], tmp_path / "bad",
            parent=tmp_path / "parent/catalog.json", _clock=clock("2026-10-20T10:00:00+08:00"))
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("field", ["labels", "recorded_at"])
def test_resealed_child_cannot_repair_old_parent_label_or_clock_prefix(tmp_path, field):
    make, _, anchor, first = dated(tmp_path, day="2026-10-15")
    catalog.build_catalog(anchor, [first], tmp_path / "parent", _clock=clock())
    child = tmp_path / "child"
    value = catalog.build_catalog(anchor, [make("2026-10-16")], child,
        parent=tmp_path / "parent/catalog.json", _clock=clock())
    if field == "labels":
        value["entries"][0]["members"][0]["labels"] = []
    else:
        value["entries"][0][field] = "2026-10-16T16:00:00+08:00"
    reseal(child, value)
    with pytest.raises(ValueError, match="prefix changed"):
        catalog.load_catalog(child / "catalog.json", observed_at="2026-10-17T12:00:00+08:00")


def test_resealed_new_label_tampering_rejected(tmp_path):
    _, _, anchor, source = dated(tmp_path)
    out = tmp_path / "out"
    value = catalog.build_catalog(anchor, [source], out, _clock=clock())
    entry = value["entries"][0]
    entry["members"][0]["labels"] = []
    entry["entry_sha256"] = registry._hash({k: v for k, v in entry.items() if k != "entry_sha256"})
    reseal(out, value)
    with pytest.raises(ValueError, match="labels, scope"):
        catalog.load_catalog(out / "catalog.json", observed_at="2026-10-17T12:00:00+08:00")


def test_changed_original_feature_bytes_invalidate_catalog_without_rewriting_it(tmp_path):
    _, _, anchor, source = dated(tmp_path)
    out = tmp_path / "out"
    value = catalog.build_catalog(anchor, [source], out, _clock=clock())
    before = (out / "catalog.json").read_bytes()
    feature = Path(value["entries"][0]["source"]["feature_file"])
    feature.write_bytes(feature.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="input proof changed"):
        catalog.load_catalog(out / "catalog.json", observed_at="2026-10-17T12:00:00+08:00")
    assert (out / "catalog.json").read_bytes() == before


def test_resealed_new_entry_cannot_claim_an_earlier_recording_clock(tmp_path):
    _, _, anchor, source = dated(tmp_path)
    out = tmp_path / "out"
    value = catalog.build_catalog(anchor, [source], out, _clock=clock())
    entry = value["entries"][0]
    entry["recorded_at"] = "2026-10-16T16:00:00+08:00"
    entry["entry_sha256"] = registry._hash({k: v for k, v in entry.items() if k != "entry_sha256"})
    reseal(out, value)
    with pytest.raises(ValueError, match="older origin or recording clock"):
        catalog.load_catalog(out / "catalog.json", observed_at="2026-10-17T12:00:00+08:00")


def test_future_source_or_changed_binding_rejected_and_no_output(tmp_path):
    _, _, anchor, source = dated(tmp_path)
    with pytest.raises(ValueError, match="actually available"):
        catalog.build_catalog(anchor, [source], tmp_path / "future", _clock=clock("2026-10-16T12:00:00+08:00"))
    assert not (tmp_path / "future").exists()
    value = read(source)
    value["analysis"]["implementation_revision"] = "different-policy"
    save(source, value)
    with pytest.raises(ValueError, match="frozen feature/calendar"):
        catalog.build_catalog(anchor, [source], tmp_path / "different", _clock=clock())
    assert not (tmp_path / "different").exists()


def test_no_overwrite_of_catalog_or_original_registry(tmp_path):
    _, _, anchor, source = dated(tmp_path)
    catalog.build_catalog(anchor, [source], tmp_path / "out", _clock=clock())
    with pytest.raises(FileExistsError):
        catalog.build_catalog(anchor, [source], tmp_path / "out", _clock=clock())
    with pytest.raises(ValueError, match="inside the original registry"):
        catalog.build_catalog(anchor, [source], anchor.parent / "child", _clock=clock())


def test_future_decision_clock_or_missing_catalog_returns_unknown_without_fallback(tmp_path):
    missing = tmp_path / "none/catalog.json"
    result = resolve(missing)
    assert result["status"] == "unknown" and result["reason"] == "dated_identity_catalog_unavailable"
    result = resolve(missing, asof="2026-10-19T16:00:00+08:00")
    assert result["reason"] == "decision_asof_is_not_yet_actual"
