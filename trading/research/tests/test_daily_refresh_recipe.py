"""Synthetic command receipts only; no data source, subprocess or Engine."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from trading.research import daily_refresh_recipe as recipe


def fixture_recipe(tmp_path, monkeypatch):
    base = tmp_path / "receipts"
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": [], "open_sessions": []}), encoding="utf-8")
    old = tmp_path / "old"
    source = [str(old / ("package_" + d)) for d in ("12", "13", "14", "15", "16")]
    common = ["--calendar", str(calendar), "--ipo-evidence", str(old / "original_ipo.json"),
        "--research-master-run", str(old / "original_master.json")]
    repeat = lambda flag, values: [token for value in values for token in (flag, str(value))]
    commands = {
        "extras": ["baostock-bulk-extras", "--package", source[-1], "--out", str(old / "extras16")],
        "pub20": ["leader-close-publication", *repeat("--package", source[-2:]), "--history-snapshot", str(old / "history"),
            "--calendar", str(calendar), "--protocol", str(old / "protocol20.json"), "--cohort-registration", str(old / "registration20.json"),
            "--decision-day", "2026-10-16", "--out", str(old / "pub20_16")],
        "forward20": ["leader-published-forward", *repeat("--publication", [old / "pub20_15/publication.json", old / "pub20_16/publication.json"]),
            *repeat("--publication-bundle", [old / "reconstructed15/bundle.json", old / "pub20_16/input_bundle/bundle.json"]),
            "--bundle", str(old / "pub20_16/input_bundle/bundle.json"), "--protocol", str(old / "protocol20.json"),
            "--registration", str(old / "registration20.json"), "--run-dir", str(old / "account20"), "--through", "2026-10-16"],
        "preview": ["leader-recent-source-preview", *repeat("--package", source), "--history-snapshot", str(old / "history"),
            *common, "--out", str(old / "preview16")],
        "environment": ["leader-environment", *repeat("--preview", [old / "preview16" / ("202610" + d) for d in ("12", "13", "14", "15", "16")]),
            *common, "--industry-snapshot", str(old / "industry.parquet"), "--bulk-extras", str(old / "extras16/result.json"), "--out", str(old / "environment16")],
        "observe": ["leader-identity", "observe", "--registry", str(old / "identity/registry.json"), "--analysis", str(old / "environment16/result.json")],
        "episodes": ["leader-identity-episodes", "--registry", str(old / "identity/registry.json"), "--through", "2026-10-16", "--out", str(old / "episodes16.json")],
        "eight_inputs": ["leader-eight-type-inputs", "--append-from", str(old / "panel15/manifest.json"), "--preview", str(old / "preview16/20261016"),
            *common, "--identity-registry", str(old / "identity/registry.json"), "--industry-snapshot", str(old / "industry.parquet"),
            "--protocol", str(old / "protocol32.json"), "--turnover-source", str(old / "extras16/result.json"), "--out", str(old / "panel16")],
        "paper32": ["leader-eight-type-paper", "--panel", str(old / "panel16/manifest.json"), "--ipo-evidence", str(old / "original_ipo.json"),
            "--research-master-run", str(old / "original_master.json"), "--out", str(old / "paper32_16")],
        "forward32": ["leader-eight-type-forward", *repeat("--publication", [old / "paper32_15/paper_plan.json", old / "paper32_16/paper_plan.json"]),
            "--panel", str(old / "panel16/manifest.json"), "--ipo-evidence", str(old / "original_ipo.json"), "--research-master-run", str(old / "original_master.json"),
            "--registration", str(old / "registration32.json"), "--run-dir", str(old / "account32"), "--through", "2026-10-16"],
        "alpha_provider": ["recent-source-qlib-provider", *repeat("--package", source), "--history-snapshot", str(old / "history"),
            "--calendar", str(calendar), "--out", str(old / "provider16")],
        "alpha_score": ["recent-source-alpha158", "--provider-view", str(old / "provider16"), "--model-run", str(old / "frozen_model.json"),
            "--start", "2026-10-16", "--end", "2026-10-16", "--out", str(old / "scores16")],
        "alpha_publication": ["recent-source-alpha158-target-publication", "--batch-run", str(old / "scores16/result.json"),
            *repeat("--prefix-batch-run", [old / "scores12_14/result.json", old / "scores15/result.json"]), *common, "--out", str(old / "alpha_pub16")],
        "alpha_forward": ["recent-source-alpha158-forward-run", *repeat("--publication", [old / "alpha_pub15/result.json", old / "alpha_pub16/result.json"]),
            "--provider-view", str(old / "provider16"), *common, "--run-dir", str(old / "alpha_account"), "--through", "2026-10-16"],
    }
    for step, (relative, _) in recipe.RECEIPTS.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"argv": ["python.exe", "-B", str(tmp_path / "run_committee.py"), *commands[step]],
            "started_at": "2026-10-16T18:00:00+08:00", "finished_at": "2026-10-16T22:00:00+08:00",
            "returncode": 0, "execution_admitted": False}), encoding="utf-8")
    monkeypatch.setattr(recipe, "RECEIPT_ROOT", base)
    return recipe.bootstrap_profile(), commands


def test_two_successive_day_plans_preserve_prefixes_and_original_strategy_options(tmp_path, monkeypatch):
    profile, original = fixture_recipe(tmp_path, monkeypatch)
    before = deepcopy(profile)
    out19, out20 = tmp_path / "operation19", tmp_path / "operation20"
    p19 = recipe.next_plan(profile, "2026-10-19", tmp_path / "package19", out19)
    p20 = recipe.next_plan(p19["next_profile"], "2026-10-20", tmp_path / "package20", out20)
    assert profile == before and not out19.exists() and not out20.exists()
    assert len(p19["steps"]) == 14 and p19["execution_day"] == "2026-10-20"
    c19, c20 = p19["next_profile"]["templates"], p20["next_profile"]["templates"]
    assert recipe._values(c20["preview"], "--package") == recipe._values(original["preview"], "--package") + [str(tmp_path / "package19"), str(tmp_path / "package20")]
    assert recipe._values(c20["pub20"], "--package")[:2] == recipe._values(original["pub20"], "--package")
    assert recipe._values(c20["forward20"], "--publication-bundle") == recipe._values(original["forward20"], "--publication-bundle") + [str(out19 / "artifacts/pub20/input_bundle/bundle.json"), str(out20 / "artifacts/pub20/input_bundle/bundle.json")]
    assert recipe._one(c20["eight_inputs"], "--append-from") == str(out19 / "artifacts/eight_inputs/manifest.json")
    assert recipe._values(c20["eight_inputs"], "--preview") == [str(out20 / "artifacts/preview/20261020")]
    assert len(recipe._values(c20["environment"], "--preview")) == 7
    assert recipe._values(c20["alpha_publication"], "--prefix-batch-run") == recipe._values(original["alpha_publication"], "--prefix-batch-run") + [str(tmp_path / "old/scores16/result.json"), str(out19 / "artifacts/alpha_score/result.json")]
    for command in (c19, c20):
        assert recipe._one(command["alpha_score"], "--model-run") == recipe._one(original["alpha_score"], "--model-run")
        assert recipe._one(command["pub20"], "--protocol") == recipe._one(original["pub20"], "--protocol")
        assert "--ledger-checkpoint" not in command["pub20"]
        assert recipe._one(command["forward32"], "--run-dir") == recipe._one(original["forward32"], "--run-dir")
    assert recipe._one(c19["alpha_score"], "--start") == recipe._one(c19["alpha_score"], "--end") == "2026-10-19"


def test_expected_files_and_dependencies_describe_real_existing_writer_contracts(tmp_path, monkeypatch):
    profile, original = fixture_recipe(tmp_path, monkeypatch)
    out = tmp_path / "operation19"
    plan = recipe.next_plan(profile, "2026-10-19", tmp_path / "package19", out)
    steps = {s["id"]: s for s in plan["steps"]}
    assert steps["forward20"]["requires"] == ["pub20"]
    assert steps["environment"]["requires"] == ["preview", "extras"]
    assert steps["paper32"]["requires"] == ["eight_inputs"]
    assert steps["observe"]["expected_files"] == [str(tmp_path / "old/identity/observations/20261019/observation.json"), str(tmp_path / "old/identity/observations/20261019/observation_receipt.json")]
    assert steps["forward32"]["expected_files"] == [str(tmp_path / "old/account32/head.json")]
    assert all(s["writes_existing_contract"] is (s["id"] in {"observe", "forward20", "forward32", "alpha_forward"}) for s in plan["steps"])
    assert plan["next_profile"]["legacy_fixed_engineering"]["stopped"] is True
    assert "fixed_lowvol" not in steps and plan["commands_executed"] is False
    assert not out.exists()


def test_bad_terminal_skipped_day_or_changed_calendar_rejected_without_outputs(tmp_path, monkeypatch):
    profile, _ = fixture_recipe(tmp_path, monkeypatch)
    out = tmp_path / "never_written"
    with pytest.raises(ValueError, match="next fixed-calendar"):
        recipe.next_plan(profile, "2026-10-20", tmp_path / "package20", out)
    calendar = Path(profile["calendar"]["path"])
    calendar.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="calendar changed"):
        recipe.next_plan(profile, "2026-10-19", tmp_path / "package19", out)
    receipt = recipe.RECEIPT_ROOT / recipe.RECEIPTS["extras"][0]
    data = json.loads(receipt.read_text())
    data["returncode"] = 1
    receipt.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="successful terminal"):
        recipe.bootstrap_profile()
    assert not out.exists()
