import json

from trading.research.galaxy_api_probe import build_probe


def test_probe_is_local_only_and_never_admits_execution(monkeypatch, tmp_path):
    monkeypatch.setenv("QMT_SECRET", "must_not_be_read")
    result = build_probe(search_roots=[tmp_path])

    assert result["schema"] == "galaxy_api_readonly_probe_v1"
    assert result["status"] == "prepared_no_connection"
    assert result["execution_admitted"] is False
    assert result["login_attempted"] is False
    assert result["broker_io_attempted"] is False
    assert result["submitted"] is False
    assert result["cancelled"] is False
    assert result["ledger_mutated"] is False
    assert result["local_discovery"]["environment_presence"]["values_read"] is False
    assert "QMT_SECRET" in result["local_discovery"]["environment_presence"]["relevant_names_present"]
    assert "must_not_be_read" not in json.dumps(result, ensure_ascii=False)


def test_probe_exposes_read_contract_without_calling_it(monkeypatch, tmp_path):
    monkeypatch.setattr("trading.research.galaxy_api_probe._process_probe", lambda: {"status": "completed", "matched": []})
    result = build_probe(search_roots=[tmp_path])

    assert result["read_contract"]["required_account_reads"] == ["asset", "positions", "orders", "trades"]
    assert result["read_contract"]["current_probe_action"] == "local_discovery_only"
    assert all(not value["connection_attempted"] for value in result["api_candidates"].values())
