"""CLI observation exports do not execute or overwrite account/source packages."""
import json
from pathlib import Path

import pytest

from trading.research import shortline_native_preopen_forward as consumer


@pytest.fixture
def invocation(tmp_path):
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    raw = inputs / 'raw'
    raw.mkdir()
    for name in ('plan.json', 'ipo.json', 'master.json'):
        (inputs / name).write_text('{"original": true}', encoding='utf8')
    run = tmp_path / 'account'
    report = tmp_path / 'status' / 'current.json'
    argv = ['--plan', str(inputs / 'plan.json'), '--raw-close', str(raw),
        '--ipo-proof', str(inputs / 'ipo.json'), '--research-master-run', str(inputs / 'master.json'),
        '--run', str(run), '--result-json', str(report)]
    return argv, inputs, run, report


def test_waiting_full_result_is_exported_without_creating_account(invocation, monkeypatch, capsys):
    argv, _, directory, report = invocation
    calls = []
    expected = {'schema': consumer.RESULT_SCHEMA, 'status': 'waiting_for_exact_complete_raw_close',
        'read_at': '2026-10-16T03:49:39+08:00', 'checkpoint': None, 'case_count': 0,
        'newly_committed_case_sessions': 0, 'metrics': None, 'execution_admitted': False,
        'missing_raw_days': ['2026-10-16'], 'orders_generated': False}
    def observe(*args, **kwargs):
        calls.append((args, kwargs))
        return expected
    monkeypatch.setattr(consumer, 'run', observe)
    assert consumer.main(argv) == 0
    assert json.loads(report.read_text(encoding='utf8')) == expected
    assert len(calls) == 1 and not directory.exists()
    assert json.loads(capsys.readouterr().out)['result_file'] == str(report.resolve())


def test_failed_command_exports_failure_and_reraises_without_inventing_account_state(invocation, monkeypatch):
    argv, _, directory, report = invocation
    report.parent.mkdir()
    report.write_text('{"status":"old_success"}', encoding='utf8')
    def failed(*args, **kwargs):
        raise ValueError('source digest mismatch')
    monkeypatch.setattr(consumer, 'run', failed)
    with pytest.raises(ValueError, match='source digest mismatch'):
        consumer.main(argv)
    failure = json.loads(report.read_text(encoding='utf8'))
    assert failure['schema'] == 'native_shortline_preopen_forward_command_failure_v1'
    assert failure['status'] == 'failed' and failure['error_type'] == 'ValueError'
    assert failure['execution_progress_unknown'] is True
    assert 'case_count' not in failure and 'cash' not in failure
    assert failure['execution_admitted'] is False and not directory.exists()


@pytest.mark.parametrize('location', ['head', 'plan', 'source_package', 'raw_package'])
def test_export_cannot_replace_source_or_account_before_consumer_runs(invocation, monkeypatch, location):
    argv, inputs, directory, report = invocation
    targets = {'head': directory / 'head.json', 'plan': inputs / 'plan.json',
        'source_package': inputs / 'new_result.json', 'raw_package': inputs / 'raw' / 'result.json'}
    target = targets[location]
    old = target.read_bytes() if target.exists() else None
    calls = []
    monkeypatch.setattr(consumer, 'run', lambda *args, **kwargs: calls.append('must_not_run'))
    argv[-1] = str(target)
    with pytest.raises(ValueError, match='outside the account and original source'):
        consumer.main(argv)
    assert not calls and not directory.exists()
    assert (target.read_bytes() if target.exists() else None) == old
