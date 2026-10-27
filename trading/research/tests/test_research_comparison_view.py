from trading.research import research_comparison_view


def test_window_splits_run_query_once(monkeypatch):
    calls = []

    def compare(run_ids, start, end):
        calls.append((run_ids, start, end))
        return {"status": "compared_saved_accounts"}

    monkeypatch.setattr(research_comparison_view.research_comparison, "compare", compare)
    result = research_comparison_view.window("run-a,run-b", "2024-01-02", "2024-01-03")
    assert result["status"] == "compared_saved_accounts"
    assert calls == [(["run-a", "run-b"], "2024-01-02", "2024-01-03")]


def test_snapshots_forwards_catalog_limit(monkeypatch):
    calls = []

    def saved_catalog(*, limit):
        calls.append(limit)
        return {"rows": []}

    monkeypatch.setattr(research_comparison_view.research_comparison, "saved_catalog", saved_catalog)
    assert research_comparison_view.snapshots(7) == {"rows": []}
    assert calls == [7]
