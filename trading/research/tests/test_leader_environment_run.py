import json

import pandas as pd
import pytest

from trading.research import leader_environment_run as runner


def fixture(tmp_path, monkeypatch):
    raw = pd.DataFrame([
        {"code": "sh.600000", "date": "2026-10-14", "tradestatus": "1", "isST": "0", "open": "10", "high": "11", "low": "9", "close": "10.5", "volume": "1000", "amount": "10000", "pctChg": "5"},
        {"code": "sh.600001", "date": "2026-10-14", "tradestatus": "0", "isST": "0", "open": "3", "high": "3", "low": "3", "close": "3", "volume": "", "amount": "", "pctChg": ""},
        {"code": "sz.000001", "date": "2026-10-14", "tradestatus": "1", "isST": "1", "open": "3", "high": "3", "low": "3", "close": "3", "volume": "100", "amount": "300", "pctChg": "0"},
    ])
    meta = {"plan": {"day": "2026-10-14"}, "receipt": {"raw_daily_sha256": "raw"},
            "source_folder": str(tmp_path / "source"),
            "ordinary_roster": [{"symbol": "600000.SSE", "code_name": "测试名称"}]}
    monkeypatch.setattr(runner, "load_complete", lambda _: (raw, meta))
    folder = tmp_path / "preview"
    folder.mkdir()
    preview = {"schema": "leader_complete_close_preview_v1", "status": "complete_source_close_research_preview_only",
               "source_inventory_complete": True, "day": "2026-10-14", "source": meta, **runner.BOUNDARY}
    (folder / "result.json").write_text(json.dumps(preview), encoding="utf-8")
    row = {"date": "2026-10-14", "symbol": "600000.SSE", "code": "600000", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000, "amount": 10000, "pct_chg": 5}
    pd.DataFrame([row]).to_csv(folder / "current_features.csv", index=False)
    return folder, row


def test_preview_matches_entire_active_scope_and_keeps_display_name(tmp_path, monkeypatch):
    folder, _ = fixture(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in folder.iterdir()}
    _, frame, provenance = runner.read_preview(folder)
    assert frame.symbol.tolist() == ["600000.SSE"]
    assert frame.name.tolist() == ["测试名称"]
    assert provenance["features_match_current_active_raw"] is True
    assert before == {p: p.read_bytes() for p in folder.iterdir()}


def test_changed_quote_or_wrong_active_scope_cannot_enter_environment(tmp_path, monkeypatch):
    folder, row = fixture(tmp_path, monkeypatch)
    pd.DataFrame([{**row, "close": 10.6}]).to_csv(folder / "current_features.csv", index=False)
    with pytest.raises(ValueError, match="values disagree"):
        runner.read_preview(folder)
    pd.DataFrame([{**row, "symbol": "600001.SSE"}]).to_csv(folder / "current_features.csv", index=False)
    with pytest.raises(ValueError, match="symbols differ"):
        runner.read_preview(folder)
