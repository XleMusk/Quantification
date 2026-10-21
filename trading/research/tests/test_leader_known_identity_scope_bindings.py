"""Small saved-source bindings only; no market panels, replay or network."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from trading.research import leader_known_identity_scope as m
from trading.research import leader_environment_exit_ablation as ablation
from trading.research import leader_historical_identity_inputs as identities
from trading.research.leader_historical_identity_rules import SPEC as IDENTITY_SPEC
from trading.research.tests.test_leader_known_identity_scope import bound_fixture, write


def modern_fixture(tmp_path, kind):
    source, baseline, event, reference, accounting = bound_fixture(tmp_path)
    manifest = json.loads(source.read_text())
    identity_reg = tmp_path / 'identity_registration.json'
    write(identity_reg, {'method_id': identities.METHOD_ID, 'rules': IDENTITY_SPEC, 'fixed_protocol': m.PROTOCOL})
    manifest['identity_research'] = {'method_id': identities.METHOD_ID, 'changed_types': list(identities.TYPES),
        'historical_publication_backdated': False, 'old_six_type_observations_modified': False,
        'registration': str(identity_reg), 'registration_sha256': m.digest(identity_reg)}
    write(source, manifest)
    saved = json.loads(baseline.read_text())
    ids = list(ablation.CASE_IDS) if kind == '48' else [f'{t}__{g}__{c}'
        for t in m.TYPE_IDS for g in m.GUARDS for c, _ in m.CAPS]
    cases = []
    for case_id in ids:
        t, g, c = case_id.split('__')
        cases.append({'case_id': case_id, 'type': t, 'guard': g, 'capacity': c,
            'config': m._config(100000, 5).to_dict(), 'orders': [], 'fills': [], 'metrics': None,
            'account': {'synthetic': True}, 'daily': [], 'engine_sessions_committed': 0})
    saved.update(source={'manifest': str(source), 'manifest_sha256': m.digest(source)}, cases=cases,
        scope_members=manifest['scope_members'], config=m._config(100000, 5).to_dict(), case_count=len(ids),
        historical_execution_policy={'accounting': accounting})
    saved.pop('execution_policy')
    if kind == '48':
        saved.update(schema=ablation.RESULT_SCHEMA, method_id=ablation.METHOD_ID, spec=deepcopy(ablation.SPEC))
    else:
        saved.update(schema='leader_eight_type_candidate_panel_replay_v1', protocol=m.PROTOCOL)
        saved.pop('parent_fixed_protocol')
    write(baseline, saved)
    if kind == '32':
        write(baseline.parent / 'identity_method.json', {
            'schema': 'leader_historical_identity_all32_shared_replay_v1', 'method_id': identities.METHOD_ID,
            'all32': True, 'fixed_protocol': m.PROTOCOL, 'input_manifest': str(source),
            'input_manifest_sha256': m.digest(source), 'shared_result': str(baseline),
            'shared_result_sha256': m.digest(baseline)})
    return source, baseline, event, reference, saved


@pytest.mark.parametrize('kind', ['32', '48'])
def test_existing_filled_and_blocked_controls_are_retained_without_pnl_filter(tmp_path, monkeypatch, kind):
    source, baseline, event, reference, saved = modern_fixture(tmp_path, kind)
    cross = [c for c in saved['cases'] if c['case_id'] in m.CASE_IDS]
    cross[0].update(orders=[{'id': 'existing-order'}], fills=[{'id': 'existing-fill'}],
        metrics={'net_return': -.043}, engine_sessions_committed=28,
        blocking_evidence={'reason': 'retained_unknown'})
    # Re-save a genuine immutable fixture before creating its source marker.
    write(baseline, saved)
    if kind == '32':
        marker = json.loads((baseline.parent / 'identity_method.json').read_text())
        marker['shared_result_sha256'] = m.digest(baseline)
        write(baseline.parent / 'identity_method.json', marker)
    before = baseline.read_bytes()
    monkeypatch.setattr(m, 'load_inputs', lambda *_: pytest.fail('no giant panels at registration'))
    record = m.register(source, baseline, event, reference, tmp_path / 'registration')
    _, retained = m.load_registration(tmp_path / 'registration' / 'registration.json')
    assert retained == cross and baseline.read_bytes() == before
    assert len(retained) == 4 and all(c['guard'] in m.GUARDS for c in retained)
    assert record['policy'] == m.SPEC and record['baseline_replayed'] is False
    assert retained[0]['metrics']['net_return'] < 0 and retained[0]['fills']


def test_skipped_four_of_48_stay_unavailable_without_accounts(tmp_path):
    source, baseline, event, reference, saved = modern_fixture(tmp_path, '48')
    for c in saved['cases']:
        if c['case_id'] in m.CASE_IDS:
            c.update(status='skipped_no_possible_entry_from_saved_panel', account=None,
                entry_readiness={'potential_entry_days': 0}, metrics=None, daily=[], engine_sessions_committed=0)
    write(baseline, saved)
    _, _, retained, _ = m._bound(source, baseline, event, reference)
    assert len(retained) == 4 and all(c['account'] is None and c['metrics'] is None for c in retained)
    assert not any(c['guard'] == ablation.CONTROL_GUARD for c in retained)
    next(c for c in saved['cases'] if c['case_id'] == retained[0]['case_id'])['account'] = {'cash': 100000}
    write(baseline, saved)
    with pytest.raises(ValueError, match='no simulated account'):
        m._bound(source, baseline, event, reference)


def test_48_source_spec_and_identity_definition_remain_exact(tmp_path):
    source, baseline, event, reference, saved = modern_fixture(tmp_path, '48')
    saved['spec']['control_transform'] = 'changed'
    write(baseline, saved)
    with pytest.raises(ValueError, match='fixed 48-case'):
        m._bound(source, baseline, event, reference)
    saved['spec'] = deepcopy(ablation.SPEC)
    saved['cases'][-1]['case_id'] = saved['cases'][0]['case_id']
    write(baseline, saved)
    with pytest.raises(ValueError, match='All original baseline cases'):
        m._bound(source, baseline, event, reference)
    saved['cases'][-1]['case_id'] = ablation.CASE_IDS[-1]
    saved['source']['manifest_sha256'] = '0' * 64
    write(baseline, saved)
    with pytest.raises(ValueError, match='Exact saved identity input'):
        m._bound(source, baseline, event, reference)
