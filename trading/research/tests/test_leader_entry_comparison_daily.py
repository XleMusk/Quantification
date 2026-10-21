"""Only the new saved-input daily recipe and observation export are exercised."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import leader_entry_comparison_daily as daily
from trading.research import leader_entry_comparison_forward as forward
from trading.research import leader_entry_comparison_publication as publication
from trading.research import leader_entry_confirmation_inputs as confirmation
from trading.research.tests.test_leader_entry_comparison_forward import real_publications
from trading.research.tests.test_leader_entry_comparison_publication import evidence, read
from trading.research.tests.test_leader_forward_run import A


def clock(e, monkeypatch):
    class SavedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return e["clock"]["value"].astimezone(tz)
    monkeypatch.setattr(daily, "datetime", SavedClock)


def current_extras(pub):
    return Path(read(pub)["confirmation"]["sources"]["current"]["extras_result"])


def test_original16_bootstrap_new19_unknown_confirmation_real15_engine_and_saved_prefix_recovery(
        real_publications, tmp_path, monkeypatch):
    e = real_publications
    clock(e, monkeypatch)
    pub16, own16 = e["make"]("2026-10-16")
    extras16 = current_extras(pub16)
    run = tmp_path / "fifteen_books"
    old_sources = {path: path.read_bytes() for path in (pub16, own16, e["registration"])}
    profile = daily.bootstrap_profile(registration=e["registration"], first_publication=pub16,
        current_extras=extras16, run_dir=run)
    assert not run.exists() and profile["publications"] == [str(pub16)]
    assert profile["publication_bundles"] == [str(own16)]
    assert profile["first_decision_day"] == "2026-10-16" and profile["first_execution_day"] == "2026-10-19"
    pub19, own19 = e["make"]("2026-10-19")
    extras19 = current_extras(pub19)
    # A new synthetic saved current source has unknown turnover. The new
    # sidecar/publication must preserve unknown, without replacing targets.
    metadata = read(extras19)
    frame = pd.read_parquet(metadata["data_file"])
    frame.loc[frame.symbol.eq(A), "turn"] = ""
    frame.loc[frame.symbol.eq(A), "turn_numeric"] = pd.NA
    frame.to_parquet(metadata["data_file"], index=False)
    metadata["data_sha256"] = sha256(Path(metadata["data_file"]).read_bytes()).hexdigest()
    extras19.write_text(json.dumps(metadata), encoding="utf-8")
    e["clock"]["value"] = datetime.fromisoformat("2026-10-19T19:00:00+08:00")
    operation = tmp_path.parent / (tmp_path.name + "_daily19")
    executed, receipts = [], {}

    def run_step(step):
        if step["id"] in receipts:
            return receipts[step["id"]]
        command = step["command"]
        executed.append(deepcopy(command))
        if command[0] == "leader-entry-confirmation-inputs":
            assert command[command.index("--previous-extras") + 1] == str(extras16)
            assert confirmation.main(command[1:]) == 0
        elif command[:2] == ["leader-entry-comparison", "publish"]:
            assert publication.main(command[1:]) == 0
        else:
            assert command[0] == "leader-entry-comparison-forward"
            assert forward.main(command[1:]) == 0
        receipts[step["id"]] = {"status": "completed"}
        return receipts[step["id"]]

    answer = daily.run_profile(profile, "2026-10-19", own19, extras19, operation, run_step=run_step)
    assert answer["result"]["producer_complete"] and answer["result"]["consumer_complete"]
    assert answer["result"]["newly_committed_case_sessions"] == 30
    assert len(executed) == 3 and answer["next_profile"]["last_extras"] == daily._ref(extras19)
    head = read(run / "head.json")
    assert len(head["cases"]) == 15
    for family in publication.FAMILIES:
        case = head["cases"][family + ":daily_confirmation"]
        assert case["state"]["pending_targets"] is None
        assert case["journal"][-1]["confirmation_observation"]["applicable_unknown"] is True
        assert case["state"]["fills"] and case["state"]["day"] == "2026-10-19"
    frozen = (run / "head.json").read_bytes()
    again = daily.run_profile(answer["next_profile"], "2026-10-19", own19, extras19, operation, run_step=run_step)
    assert again["result"]["consumer_complete"] and len(executed) == 3
    assert (run / "head.json").read_bytes() == frozen
    assert all(path.read_bytes() == payload for path, payload in old_sources.items())


def test_exact_previous_extras_and_missing_day_refuse_before_any_daily_output(real_publications, tmp_path, monkeypatch):
    e = real_publications
    clock(e, monkeypatch)
    pub16, _ = e["make"]("2026-10-16")
    profile = daily.bootstrap_profile(registration=e["registration"], first_publication=pub16,
        current_extras=current_extras(pub16), run_dir=tmp_path / "fifteen_books")
    pub19, own19 = e["make"]("2026-10-19")
    extras19 = current_extras(pub19)
    wrong = {**deepcopy(profile), "last_extras": daily._ref(extras19)}
    out = tmp_path.parent / (tmp_path.name + "_bad_previous")
    with pytest.raises(ValueError, match="accurate previous"):
        daily.next_plan(wrong, "2026-10-19", own19, extras19, out)
    with pytest.raises(ValueError, match="exact next"):
        daily.next_plan(profile, "2026-10-20", own19, extras19, out)
    assert not out.exists()


def test_indirect_protocol_and_source_export_roots_are_protected_and_failure_stays_unknown(tmp_path, monkeypatch):
    source = tmp_path / "inputs"
    source.mkdir()
    protocol = tmp_path / "protocols/root.json"
    protocol.parent.mkdir()
    parent = tmp_path / "original_evidence/parent.json"
    parent.parent.mkdir()
    raw = tmp_path / "original_raw/raw.parquet"
    parent.write_text(json.dumps({"raw_file": str(raw)}), encoding="utf-8")
    protocol.write_text(json.dumps({"parent_protocol": "../original_evidence/parent.json"}), encoding="utf-8")
    registration = source / "registration.json"
    registration.write_text(json.dumps({"protocol": {"file": str(protocol)}}), encoding="utf-8")
    run = tmp_path / "books"
    with pytest.raises(ValueError, match="outside the account"):
        forward._result_export_path(raw.parent / "overwrite.json", publications=[], publication_bundles=[],
            latest_bundle=None, registration=registration, run_dir=run)
    with pytest.raises(ValueError, match="outside the account"):
        forward._result_export_path(run / "observation.json", publications=[], publication_bundles=[],
            latest_bundle=None, registration=registration, run_dir=run)
    monkeypatch.setattr(forward, "run", lambda *a, **k: (_ for _ in ()).throw(ValueError("specific execution failure")))
    result = tmp_path / "observations/failed.json"
    before = registration.read_bytes()
    with pytest.raises(ValueError, match="specific execution failure"):
        forward.main(["--registration", str(registration), "--run-dir", str(run), "--result-json", str(result)])
    failed = read(result)
    assert failed["schema"] == "leader_entry_comparison_forward_command_failure_v1"
    assert failed["execution_progress_unknown"] is True and failed["newly_committed_case_sessions"] is None
    assert failed["account_reset"] is False and not run.exists() and registration.read_bytes() == before
