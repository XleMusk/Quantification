"""Exact saved Engine bytes are opt-in and never imported by inspection."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

from trading import engine as current_engine
from trading.research import native_lowvol_multiyear_resume as recovery
from trading.research import native_lowvol_replay as replay
from trading.research.baostock_daily_slice import digest
from trading.research.tests.test_native_lowvol_multiyear_resume import (
    DAYS, read, stopped, write)


@pytest.fixture
def frozen(stopped, tmp_path):
    # The original numerical source differs from current only in a harmless
    # fixture comment, so its exact hash proves the explicit selection path.
    path = tmp_path / "original_engine.py"
    path.write_bytes(Path(current_engine.__file__).read_bytes() + b"\n# synthetic registered Engine source\n")
    registration = read(stopped["old"] / "registration.json")
    engine_pin = next(ref for ref in registration["implementation"] if ref["path"].endswith("engine.py"))
    engine_pin["sha256"] = digest(path)
    write(stopped["old"] / "registration.json", registration)
    result = read(stopped["old"] / "result.json")
    result["registration"] = deepcopy(registration)
    write(stopped["old"] / "result.json", result)
    stopped["original_files"] = {item: item.read_bytes() for item in stopped["old"].iterdir()}
    return {**stopped, "snapshot": path, "snapshot_bytes": path.read_bytes(),
        "reference": replay._reference(path)}


def test_exact_frozen_source_inspection_does_not_import_or_advance(frozen, tmp_path, monkeypatch):
    args = list(frozen["args"])
    args[2:4] = frozen["old_pair"]
    module_name = "trading._native_lowvol_frozen_engine_" + frozen["reference"]["sha256"]
    modules_before = dict(sys.modules)
    monkeypatch.setattr(recovery, "_frozen_advance", lambda *a: pytest.fail("Inspection cannot import a saved Engine"))
    monkeypatch.setattr(recovery, "advance", lambda *a, **k: pytest.fail("Inspection cannot advance"))
    output = tmp_path / "inspect_output"
    result = replay.run(*args, output, prior_calendars=[frozen["prior"]],
        resume_stopped_multiyear=frozen["old"], inspect_only=True, frozen_engine_source=frozen["snapshot"])
    assert result["status"] == "blocked_stopped_multiyear"
    assert result["frozen_engine_source"] == frozen["reference"]
    assert result["frozen_engine_imported"] is result["engine_advanced"] is False
    assert not output.exists() and sys.modules.get(module_name) is modules_before.get(module_name)
    assert frozen["snapshot"].read_bytes() == frozen["snapshot_bytes"]
    assert all(path.read_bytes() == data for path, data in frozen["original_files"].items())


def test_ready_resume_runs_exact_engine_snapshot_and_binds_it_without_replacing_current_engine(frozen, tmp_path, monkeypatch):
    output = tmp_path / "frozen_completed"
    engine_module, engine_advance = sys.modules["trading.engine"], current_engine.advance
    real_loader, indices = recovery._frozen_advance, []
    def loader(reference):
        assert reference == frozen["reference"]
        loaded = real_loader(reference)
        assert loaded.__module__ == "trading._native_lowvol_frozen_engine_" + reference["sha256"]
        def advance(state, feed, day, config, index, **kwargs):
            indices.append(index)
            return loaded(state, feed, day, config, index, **kwargs)
        return advance
    monkeypatch.setattr(recovery, "_frozen_advance", loader)
    monkeypatch.setattr(recovery, "advance", lambda *a, **k: pytest.fail("Explicit snapshot cannot fall back to current Engine"))
    result = recovery.resume(*frozen["args"], output, resume_from=frozen["old"],
        prior_calendars=[frozen["prior"]], frozen_engine_source=frozen["snapshot"])
    assert result["status"] == "completed" and result["completed_sessions"] == 6
    assert result["reused_sessions"] == 4 and indices == [4, 5]
    assert result["registration"]["continuation"]["frozen_engine_source"] == frozen["reference"]
    after = read(output / "account.json")
    assert after["day"] == DAYS[-1] and after["positions"] == frozen["state"]["positions"]
    assert after["cash"] == frozen["state"]["cash"] + sum(frozen["state"]["receivables"].values())
    assert after["receivables"] == {}
    assert sys.modules["trading.engine"] is engine_module and current_engine.advance is engine_advance
    assert frozen["snapshot"].read_bytes() == frozen["snapshot_bytes"]
    assert not (frozen["snapshot"].parent / "__pycache__").exists()
    assert all(path.read_bytes() == data for path, data in frozen["original_files"].items())


@pytest.mark.parametrize("fault", ["wrong_sha", "changed_before_loader"])
def test_frozen_engine_wrong_or_changed_bytes_are_rejected_without_account_commit(frozen, tmp_path, monkeypatch, fault):
    output = tmp_path / "rejected_frozen"
    engine_module, engine_advance = sys.modules["trading.engine"], current_engine.advance
    if fault == "wrong_sha":
        frozen["snapshot"].write_bytes(frozen["snapshot_bytes"] + b"\n# wrong bytes\n")
        message = "exactly match the original registered Engine SHA256"
    else:
        real_loader = recovery._frozen_advance
        def changing_loader(reference):
            # Readiness and the first input check have already succeeded.
            frozen["snapshot"].write_bytes(frozen["snapshot_bytes"] + b"\n# changed after preparation\n")
            return real_loader(reference)
        monkeypatch.setattr(recovery, "_frozen_advance", changing_loader)
        message = "pinned.*input changed"
    with pytest.raises(ValueError, match=message):
        recovery.resume(*frozen["args"], output, resume_from=frozen["old"],
            prior_calendars=[frozen["prior"]], frozen_engine_source=frozen["snapshot"])
    assert not (output / "account.json").exists() and not (output / "daily.csv").exists()
    assert sys.modules["trading.engine"] is engine_module and current_engine.advance is engine_advance
    assert all(path.read_bytes() == data for path, data in frozen["original_files"].items())
