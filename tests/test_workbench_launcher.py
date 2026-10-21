import argparse
import os
from pathlib import Path
import socket
import subprocess
from unittest.mock import Mock, patch

import pytest

from scripts import workbench


def test_explicit_data_paths_and_ports_reach_every_child_without_changing_parent(tmp_path):
    args = argparse.Namespace(data_dir=tmp_path / "trading", web_storage=tmp_path / "research",
                              api_port=18965, ui_port=15175)
    original = dict(os.environ)
    with patch.dict(os.environ, {"QUANT_TRADING_HOME": str(tmp_path / "old"),
                                 "PYTHONPATH": "existing_path"}):
        before = dict(os.environ)
        env = workbench.environment(args)
        assert dict(os.environ) == before
    assert dict(os.environ) == original
    assert env["QUANT_TRADING_HOME"] == str(args.data_dir.resolve())
    assert env["QUANT_WEB_STORAGE"] == str(args.web_storage.resolve())
    assert env["QUANT_UI_ORIGIN"] == "http://127.0.0.1:15175"
    assert env["QUANT_API_PORT"] == "18965"
    assert env["PYTHONPATH"].split(os.pathsep) == [str(workbench.PROJECT), "existing_path"]


def test_default_home_is_outside_checkout_on_posix(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX fallback")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert workbench.default_trading_home() == tmp_path / "quantification" / "trading"


def test_port_conflict_does_not_stop_existing_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(ValueError, match="occupied"):
            workbench.require_ports(port, 5175 if port != 5175 else 5176)
        assert listener.getsockname()[1] == port
    with pytest.raises(ValueError, match="different"):
        workbench.require_ports(8765, 8765)


def test_workers_are_opt_in(monkeypatch):
    captured = []
    monkeypatch.setattr(workbench, "check", lambda env: {"ready": True})
    monkeypatch.setattr(workbench, "start", lambda args, env: captured.append(args))
    assert workbench.main(["start"]) == 0
    assert not captured[0].factor_worker and not captured[0].trading_worker
    assert workbench.main(["start", "--factor-worker"]) == 0
    assert captured[1].factor_worker and not captured[1].trading_worker


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group escalation")
def test_cleanup_continues_if_child_exits_during_forced_termination():
    survivor = Mock(pid=111)
    survivor.poll.return_value = None
    disappearing = Mock(pid=222)
    disappearing.poll.return_value = None
    disappearing.wait.side_effect = [subprocess.TimeoutExpired("child", 5), 0]
    with patch.object(workbench.os, "killpg", side_effect=[None, ProcessLookupError(), None]) as killpg:
        workbench.stop_children([survivor, disappearing])
    assert killpg.call_count == 3
    survivor.wait.assert_called_once_with(timeout=5)
