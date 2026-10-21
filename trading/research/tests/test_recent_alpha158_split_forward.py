import json
from pathlib import Path

import pytest

from trading.research import recent_alpha158_split_forward as split
from trading.research.tests import test_recent_alpha158_forward_run as fixtures


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    return fixtures.evidence.__wrapped__(tmp_path, monkeypatch)


def test_split_settles_raw_day_then_attaches_late_publication(evidence):
    first, second = evidence["pubs"]
    run_dir = evidence["run_dir"] / "split"
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    ipo, master = run_dir.parent / "ipo.json", run_dir.parent / "master.json"
    ipo.write_text("{}", encoding="utf-8"); master.write_text("{}", encoding="utf-8")
    split.register(first, evidence["calendar"], ipo, master, run_dir)
    settled = split.run([first], evidence["providers"]["2026-10-19"], evidence["calendar"],
        ipo, master, run_dir, through="2026-10-16")
    assert settled["newly_committed_phases"] == 4
    assert all(case["missing_publication_days"] == ["2026-10-16"] for case in settled["cases"])
    attached = split.run([first, second], evidence["providers"]["2026-10-19"], evidence["calendar"],
        ipo, master, run_dir, through="2026-10-16")
    assert attached["newly_committed_phases"] == 2
    assert all(case["missing_publication_days"] == [] for case in attached["cases"])
    head = json.loads((run_dir / "head.json").read_text(encoding="utf-8"))
    assert all(len(case["daily"]) == 2 and len(case["attachments"]) == 1
               for case in head["cases"].values())
    original = (run_dir / "head.json").read_bytes()
    again = split.run([first, second], evidence["providers"]["2026-10-16"], evidence["calendar"],
        "ipo", "master", run_dir, through="2026-10-16")
    assert again["newly_committed_phases"] == 0
    assert (run_dir / "head.json").read_bytes() == original


def test_split_requires_immutable_registration(evidence):
    first = evidence["pubs"][0]
    run_dir = evidence["run_dir"] / "split_registration"
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    ipo, master = run_dir.parent / "ipo.json", run_dir.parent / "master.json"
    ipo.write_text("{}", encoding="utf-8"); master.write_text("{}", encoding="utf-8")
    split.register(first, evidence["calendar"], ipo, master, run_dir)
    with pytest.raises(ValueError, match="registration"):
        split.register(evidence["pubs"][1], evidence["calendar"], ipo, master, run_dir)
