"""Focused latest-reader regression; synthetic saved evidence, no producer/Engine."""
from datetime import datetime
from hashlib import sha256
import json
import os

import pytest

from trading.research import recent_alpha158_view as m
from trading.research.tests.test_recent_alpha158_view import save

NOW = datetime.fromisoformat('2026-10-16T23:00:00+08:00')


def latest(tmp_path):
    old, record, provider_path = save(tmp_path)
    provider = json.loads(provider_path.read_text())
    provider.update(created_at='2026-10-16T21:50:00+08:00',
        recent_source_days=['2026-10-12', '2026-10-13', '2026-10-14', '2026-10-15', '2026-10-16'])
    new_provider = provider_path.parent.parent / '20261016_1012_1016_v1/result.json'
    new_provider.parent.mkdir()
    new_provider.write_text(json.dumps(provider), encoding='utf8')
    day = '2026-10-16'
    record.update(started_at='2026-10-16T22:00:00+08:00', finished_at='2026-10-16T22:02:00+08:00',
        actual_computed_at='2026-10-16T22:02:00+08:00', source_available_at=provider['created_at'],
        provider_view=str(new_provider.parent), provider_result_sha256=sha256(new_provider.read_bytes()).hexdigest(),
        requested_feature_dates=[day], score_dates=[day], score_rows=7,
        native_feature_rows=10, partitions=[{'symbols': [str(i) for i in range(10)], 'rows': 10}],
        provider_calendar_end=day, coverage=[{**record['coverage'][0], 'day': day}])
    path = old.parent.parent / '20261016_1016_v1/result.json'
    path.parent.mkdir()
    path.write_text(json.dumps(record), encoding='utf8')
    os.utime(old, (2000000000, 2000000000))
    os.utime(path, (1, 1))
    return path, record


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'corrupt'])
def test_latest_semantic_batch_ignores_mtime_and_never_falls_back(tmp_path, terminal):
    path, record = latest(tmp_path)
    if terminal == 'failed':
        record.update(status=m.FAILED, error='actual saved failure')
        path.write_text(json.dumps(record), encoding='utf8')
    elif terminal == 'corrupt':
        path.write_text('{', encoding='utf8')
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    if terminal == 'corrupt':
        with pytest.raises(ValueError, match='cannot be ordered'):
            m.recent_alpha158_view(tmp_path, now=NOW)
    else:
        view = m.recent_alpha158_view(tmp_path, now=NOW)
        assert view['source_file'] == str(path)
        assert view['status'] == (m.COMPLETED if terminal == 'completed' else m.FAILED)
        assert view['score_rows'] == (7 if terminal == 'completed' else None)
        assert view['engine_ledger_replay_completed'] is view['ledger_mutated'] is False
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_saved_publication_and_bound_head_are_separate_and_hash_pinned(tmp_path):
    path, _ = latest(tmp_path)
    project = tmp_path / 'project'
    pub_path = project / 'outputs/recent_alpha158_target_publication/20261016/result.json'
    pub_path.parent.mkdir(parents=True)
    pub = dict(schema='recent_alpha158_dated_target_publication_v1', status='close_research_target_publication_only',
        source_close_day='2026-10-16', proposed_execution_day='2026-10-19', recorded_at='2026-10-16T22:03:00+08:00',
        score_computed_at='2026-10-16T22:02:00+08:00', publication_origin='local_recording_now',
        execution_binding_pending=False, source=dict(batch_run=str(path), batch_sha256=sha256(path.read_bytes()).hexdigest()),
        frozen_model_contract={'frozen': 'same'}, cases=[dict(case=k, status='unbound_research_full_target',
            target_weights={'000001.SZSE': 0.16}) for k in sorted(m.CASE_IDS)],
        **{k: False for k in ('publication_time_backfilled', 'execution_day_rolled_forward', 'strategy_validated',
            'selection_enabled', 'execution_admitted', 'model_refit', 'broker_interacted', 'provider_contacted')})
    pub_path.write_text(json.dumps(pub), encoding='utf8')
    cases = {k: dict(context={'account_id': 'independent:' + k}, state=dict(day='2026-10-16', cash=10000,
        positions={'000001.SZSE': [{'qty': 100}]}, orders=[{}], fills=[{}],
        pending_published_close_intent={'execution_not_before': '2026-10-19'}),
        daily=[dict(date='2026-10-16', equity=1000,
            consumed_published_close_intent={'decision_day': '2026-10-15'})],
        journal=[dict(decision_day='2026-10-16', committed_at='2026-10-16T22:04:00+08:00')])
        for k in sorted(m.CASE_IDS)}
    head = dict(schema='recent_alpha158_published_close_shared_ledger_checkpoint_v1',
        registered_at='2026-10-16T22:04:00+08:00', first_decision_day='2026-10-16',
        binding={'frozen_model_contract': pub['frozen_model_contract']}, cases=cases,
        publications=[dict(file=str(pub_path), sha256=sha256(pub_path.read_bytes()).hexdigest())],
        **{k: False for k in ('strategy_validated', 'selection_enabled', 'execution_admitted', 'model_refit',
            'broker_interacted', 'provider_contacted', 'user_account_mutated')})
    head_path = tmp_path / 'research/recent_alpha158_forward/original/head.json'
    head_path.parent.mkdir(parents=True)
    head_path.write_text(json.dumps(head), encoding='utf8')
    before = path.read_bytes(), pub_path.read_bytes(), head_path.read_bytes()
    view = m.recent_alpha158_view(tmp_path, project_root=project, now=NOW)
    assert view['source_file'] == str(path)
    assert view['latest_publication']['recorded_at'] == pub['recorded_at']
    trace = view['saved_forward_trace']
    assert trace['latest_publication_bound'] is True and trace['case_count'] == 2
    assert all(c['pending_execution_day'] == '2026-10-19' and c['metrics'] is None for c in trace['cases'])
    assert view['engine_ledger_replay_completed'] is view['ledger_mutated'] is view['score_run_started'] is False
    assert before == (path.read_bytes(), pub_path.read_bytes(), head_path.read_bytes())
    head['publications'][0]['sha256'] = '0' * 64
    head_path.write_text(json.dumps(head), encoding='utf8')
    with pytest.raises(ValueError, match='publication binding changed'):
        m.recent_alpha158_view(tmp_path, project_root=project, now=NOW)
