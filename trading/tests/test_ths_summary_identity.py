"""Fake-backend summary/profile contracts; no Windows/UI calls are made."""
from copy import deepcopy
import json

import pytest

from trading.ths import profile_refresh as refresh
from trading.ths.windows import ControlWindow


FIELDS = ("account_alias", "cash_available", "cash_withdrawable", "stock_market_value",
          "total_assets", "position_pnl", "day_pnl", "day_pnl_pct")
ALIAS = "synthetic simulation account"


def selectors(balances=False):
    fields = [*FIELDS, *(["cash_total", "cash_frozen"] if balances else [])]
    return {field: {"control_id": index + 1, "process_id": 7} for index, field in enumerate(fields)}


def fake_window(mapping, *, aliases=None):
    window = ControlWindow.__new__(ControlWindow)
    calls, ready = [], []
    by_id = {value["control_id"]: key for key, value in mapping.items()}
    texts = {"cash_available": "50.00", "cash_withdrawable": "0.00", "cash_total": "60.00",
        "cash_frozen": "10.00", "stock_market_value": "100.00", "total_assets": "160.00",
        "position_pnl": "-1.00", "day_pnl": "0.00", "day_pnl_pct": "0.00%"}
    account_reads = iter(aliases or [ALIAS, ALIAS])

    def summary_text(selector, process):
        field = by_id[selector["control_id"]]
        calls.append(field)
        return next(account_reads) if field == "account_alias" else texts[field]
    window.ready = lambda *, require_foreground=True: ready.append(require_foreground)
    window.summary_text = summary_text
    return window, calls, ready


def test_original_eight_fields_and_explicit_balance_pair_keep_raw_values_and_background_identity():
    original = selectors()
    window, calls, ready = fake_window(original)
    values = window.read_summary_background(original)
    assert set(values) == set(FIELDS) and values["account_alias"] == ALIAS
    assert calls[0] == calls[-1] == "account_alias" and ready == [False, False]
    # Foreground legacy reads still use their original explicit foreground rule.
    window, _, ready = fake_window(original)
    assert window.read_summary(original)["cash_withdrawable"] == "0.00" and ready == [True, True]
    mapping = selectors(balances=True)
    window, calls, ready = fake_window(mapping)
    values = window.read_summary_background(mapping, expected_account_alias=ALIAS)
    assert values["cash_total"] == "60.00" and values["cash_frozen"] == "10.00"
    assert values["cash_withdrawable"] == "0.00" and values["cash_available"] == "50.00"
    assert calls[0] == calls[-1] == "account_alias" and ready == [False, False]


def test_incomplete_or_borrowed_cash_map_and_changed_account_never_return_observation():
    incomplete = selectors(); incomplete["cash_total"] = {"control_id": 101}
    window, calls, ready = fake_window(incomplete)
    with pytest.raises(ValueError, match="pair"):
        window.read_summary_background(incomplete)
    assert not calls and not ready
    borrowed = selectors(balances=True)
    borrowed["cash_total"] = deepcopy(borrowed["cash_withdrawable"])
    window, calls, ready = fake_window(borrowed)
    with pytest.raises(ValueError, match="reuse"):
        window.read_summary_background(borrowed)
    assert not calls and not ready
    mapping = selectors(balances=True)
    window, _, _ = fake_window(mapping, aliases=[ALIAS, "another account"])
    with pytest.raises(RuntimeError, match="changed during"):
        window.read_summary_background(mapping, expected_account_alias=ALIAS)
    window, calls, _ = fake_window(mapping, aliases=["another account"])
    with pytest.raises(RuntimeError, match="explicitly expected"):
        window.read_summary_background(mapping, expected_account_alias=ALIAS)
    assert calls == ["account_alias"]


def profile_fixture():
    return {"summary_verified": True, "expected_account_alias": ALIAS,
        "target": {"hwnd": 10, "pid": 1, "exe": "X:/synthetic/client.exe", "version": "1.0",
            "backend": "win32", "class_name": "SyntheticInspectedRoot"},
        "summary_process": {"pid": 7, "exe": "X:/synthetic/helper.exe", "version": "2.0"},
        "summary": selectors(balances=True), "grids": {"holdings": {"control_id": 99, "process_id": 7}}}


def probe_fixture(*, class_name="SyntheticInspectedRoot"):
    return {"windows": [{"hwnd": 20, "pid": 2, "class_name": class_name,
        "title": "synthetic visible account", "enabled": True, "minimized": False}],
        "processes": [{"pid": 2, "exe": "X:/synthetic/client.exe", "version": "1.0"},
                      {"pid": 8, "exe": "X:/synthetic/helper.exe", "version": "2.0"}]}


def test_refresh_cannot_promote_unknown_mapping_or_different_class_without_reading_controls(monkeypatch):
    profile = profile_fixture()
    monkeypatch.setattr(refresh, "probe", lambda: pytest.fail("No probe before explicit existing mapping/account guard"))
    unknown = deepcopy(profile); unknown["summary_verified"] = False
    with pytest.raises(ValueError, match="already inspected"):
        refresh.refresh_profile(unknown)
    missing_alias = deepcopy(profile); missing_alias.pop("expected_account_alias")
    with pytest.raises(ValueError, match="expected_account_alias"):
        refresh.refresh_profile(missing_alias)
    monkeypatch.setattr(refresh, "probe", lambda: probe_fixture(class_name="DifferentRoot"))
    monkeypatch.setattr(refresh, "ControlWindow", lambda *a: pytest.fail("Different class must not connect"))
    before = deepcopy(profile)
    with pytest.raises(RuntimeError, match="identity is ambiguous"):
        refresh.refresh_profile(profile)
    assert profile == before


def test_refresh_same_class_and_account_preserves_original_and_bad_alias_writes_no_profile(tmp_path, monkeypatch):
    profile = profile_fixture()
    before = deepcopy(profile)
    monkeypatch.setattr(refresh, "probe", probe_fixture)
    reads = []

    class FakeWindow:
        def __init__(self, target):
            reads.append(("target", deepcopy(target)))
        def read_summary_background(self, mapping, *, process, expected_account_alias):
            reads.append(("read", deepcopy(mapping), deepcopy(process), expected_account_alias))
            return {"account_alias": ALIAS, "cash_total": "60.00", "cash_frozen": "10.00",
                    "cash_available": "50.00", "cash_withdrawable": "0.00"}
    monkeypatch.setattr(refresh, "ControlWindow", FakeWindow)
    result = refresh.refresh_profile(profile)
    assert profile == before and result["target"]["hwnd"] == 20 and result["target"]["pid"] == 2
    assert result["summary_verified"] is True and result["summary_process"]["pid"] == 8
    assert all(selector["process_id"] == 8 for selector in result["summary"].values())
    assert result["session_refresh"]["account_alias_verified"] is True
    assert result["session_refresh"]["root_class_verified"] is True
    assert result["session_refresh"]["field_mapping_reverified"] is False
    assert result["session_refresh"]["cash_balance_fields_read"] is True and reads[-1][-1] == ALIAS

    class WrongAccountWindow(FakeWindow):
        def read_summary_background(self, *a, **k):
            return {"account_alias": "a different account"}
    monkeypatch.setattr(refresh, "ControlWindow", WrongAccountWindow)
    source, out = tmp_path / "original_profile.json", tmp_path / "new_profile.json"
    source.write_text(json.dumps(profile), encoding="utf-8")
    saved_bytes = source.read_bytes()
    with pytest.raises(RuntimeError, match="another account"):
        refresh.write_refreshed_profile(source, out)
    assert not out.exists() and source.read_bytes() == saved_bytes and profile == before
