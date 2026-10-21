"""Saved-file integration only; no new signal, account advance or market replay."""
from contextlib import nullcontext
from copy import deepcopy
from hashlib import sha256
import json
import os
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading.ledger import new_state
from trading.contracts import RunContext, TradingMode
from trading.research import leader_eight_type_opening as bridge
from trading.research import leader_eight_type_position_view as positions
from trading.research import opening_daily as coordinator
from trading.research import opening_daily_artifacts as factory
from trading.research import opening_daily_view as view
from trading.research.opening_daily_detail import opening_daily_detail
from trading.research.opening_daily_ledger import opening_daily_ledger
from trading.research import opening_pack
from trading.research import saved_leader_account_intent as saved
from trading.research.saved_account_intent import read_request
from trading.research.tests.test_saved_leader_account_intent import books, write, D, E, NOW, A, B


@pytest.fixture
def dated(books):
    directory, head = books
    method = positions.saved_method('dated_catchup')
    old = list(head['cases'].values())[:4]
    protocol = {'id': method['method_id'], 'cases': list(method['case_ids'])}
    publication_path = Path(head['publications'][0]['file'])
    publication = json.loads(publication_path.read_text(encoding='utf-8'))
    publication.update(schema=method['publication_schema'], method_id=method['method_id'], protocol=protocol,
        cases=[{'case_id': key, 'cap_candidates': []} for key in method['case_ids']])
    raw = directory / 'raw.parquet'
    pq.write_table(pa.Table.from_pylist([
        {'date': D, 'code': 'sh.600001', 'close': '10.00', 'tradestatus': '1', 'isST': '0'},
        {'date': D, 'code': 'sz.000002', 'close': '20.00', 'tradestatus': '1', 'isST': '0'}]), raw)
    manifest = directory / 'manifest.json'
    write(manifest, {'raw_file': str(raw), 'raw_sha256': sha256(raw.read_bytes()).hexdigest()})
    reference = {**write(publication_path, publication), 'day': D, 'execution_day': E,
        'recorded_at': publication['recorded_at'], 'input_manifest': str(manifest),
        'input_manifest_sha256': sha256(manifest.read_bytes()).hexdigest()}
    cases = {}
    identity = sha256(json.dumps(str(directory), sort_keys=True, separators=(',', ':'),
        ensure_ascii=False).encode()).hexdigest()[:16]
    for key, case in zip(method['case_ids'], old):
        case = deepcopy(case)
        context = RunContext(method['account_prefix'] + identity + ':' + key, TradingMode.REPLAY).to_dict()
        case['context'] = context
        state = {**new_state(100000), **case['state']}
        case['state'] = state
        for value in (state.get('pending_published_close_intent'), state.get('pending_risk_exits')):
            if value is not None:
                value['context'] = deepcopy(context)
        entry = case['journal'][0]
        entry['bound_original_intent']['context'] = deepcopy(context)
        entry['publication'] = reference
        for field in ('pending_targets', 'pending_published_close_intent', 'pending_risk_exits'):
            entry['post_session_' + field] = deepcopy(state[field])
        case['journal_files'] = [write(directory / 'journal' / key / 'synthetic_session.json', entry)]
        cases[key] = case
    head.update(schema=method['head_schema'], cases=cases, publications=[reference])
    head['binding'].update(protocol=protocol, published_target_policy_id=method['target_policy_id'],
        risk_policy={'id': method['risk_prefix'][:-1]})
    write(directory / 'head.json', head)
    return directory, head


def test_dated_four_books_full_nochange_empty_and_blocked_are_saved_not_reselected(dated):
    directory, head = dated
    before = (directory / 'head.json').read_bytes()
    payloads = saved.export_requests(directory, E, current=NOW, source_family='dated_catchup')
    assert list(payloads) == list(positions.DATED_CASE_IDS)
    full, nc, empty, blocked = payloads.values()
    assert full['schema'] == saved.DATED_SCHEMA and full['source_family'] == 'dated_catchup'
    assert full['weights'] == {A: 0, B: .16} and full['remaining_exit_symbols'] == [A]
    assert nc['kind'] == 'NO_CHANGE' and nc['weights'] is None and nc['remaining_exit_symbols'] == [B]
    assert empty['kind'] == 'FULL_TARGET' and empty['weights'] == {}
    assert blocked['status'] == 'not_ready' and 'weights' not in blocked
    assert read_request(full, current=NOW) == full
    assert before == (directory / 'head.json').read_bytes()
    wrong = deepcopy(full)
    wrong['remaining_exit_symbols'] = []
    with pytest.raises(ValueError, match='exact head'):
        read_request(wrong, current=NOW)
    with pytest.raises(ValueError, match='identity or package'):
        saved.export_requests(directory, E, current=NOW)


def test_dated_missing_head_has_four_unknown_books_and_creates_nothing(tmp_path):
    directory = tmp_path / 'missing'
    payloads = saved.export_requests(directory, E, current=NOW, source_family='dated_catchup')
    assert list(payloads) == list(positions.DATED_CASE_IDS)
    assert all(row['schema'] == saved.DATED_NOT_READY_SCHEMA and row['status'] == 'not_ready'
        and 'weights' not in row for row in payloads.values())
    assert not directory.exists()


def test_dated_source_schema_and_exit_binding_cannot_masquerade_as_original32(dated):
    directory, head = dated
    head['binding']['risk_policy'] = {'id': positions.RISK_PREFIX[:-1]}
    write(directory / 'head.json', head)
    with pytest.raises(ValueError, match='sticky-exit binding'):
        saved.export_requests(directory, E, current=NOW, source_family='dated_catchup')


def test_dated_position_risk_readback_accepts_only_its_saved_policy_and_keeps_unknown_residual(dated):
    directory, head = dated
    state = head['cases'][positions.DATED_CASE_IDS[0]]['state']
    state['events'].append({'day': D, 'kind': 'leader_type_risk_assessed', 'symbol': A,
        'type_id': 'sector_catchup_leader', 'policy_id': positions.saved_method('dated_catchup')['risk_prefix']
        + 'sector_catchup_leader', 'status': 'unknown', 'reasons': ['saved_feature_unknown'],
        'facts': {'close': None}, 'execution_admitted': False})
    write(directory / 'head.json', head)
    row = positions.read_saved_positions(directory, now=NOW, source_family='dated_catchup')['rows'][0]
    assert row['positions'][0]['assessment']['status'] == 'unknown'
    assert row['positions'][0]['queued_risk_exit']['execution_on'] == E
    payload = saved.export_requests(directory, E, current=NOW, source_family='dated_catchup')
    assert payload[positions.DATED_CASE_IDS[0]]['remaining_exit_symbols'] == [A]
    state['events'][0]['policy_id'] = positions.RISK_PREFIX + 'sector_catchup_leader'
    write(directory / 'head.json', head)
    with pytest.raises(ValueError, match='another case'):
        positions.read_saved_positions(directory, now=NOW, source_family='dated_catchup')


def test_dated_opening_pack_and_readonly_status_keep_independent_four_cases(dated, tmp_path, monkeypatch):
    directory, head = dated
    before = {path: path.read_bytes() for path in directory.rglob('*') if path.is_file()}
    monkeypatch.setattr(opening_pack, 'ROOT', tmp_path / 'repo')
    monkeypatch.setattr(opening_pack, 'DATA_ROOT', tmp_path / 'data')
    output, result = bridge.build(directory, E, tmp_path / 'opening', current=NOW, source_family='dated_catchup')
    assert result['source_family'] == 'dated_catchup' and result['case_count'] == 4
    assert result['preview_count'] == 3 and result['not_ready_count'] == 1
    assert [row['case_id'] for row in result['cases']] == list(positions.DATED_CASE_IDS)
    full, nc = result['cases'][:2]
    assert full['queued_exit_symbols'] == [A] and nc['queued_exit_symbols'] == [B]
    pack = json.loads(Path(result['opening_pack']).read_text(encoding='utf-8'))
    assert pack['strategy_count'] == 3 and pack['automatic_strategy_selection'] is False
    assert pack['execution_admitted'] is False
    route = {'id': 'dated_catchup', 'family': 'dated_catchup', 'source': str(directory),
        'adopt': {'result': str(output / 'result.json'), 'opening_pack': result['opening_pack']}}
    product = factory.adopt(route, factory._reference(directory / 'head.json'), route['adopt'])
    assert product['case_count'] == 4 and product['ready_case_count'] == 3
    assert product['not_ready_case_count'] == 1 and product['prefill_allowed'] is False
    monkeypatch.setattr(coordinator, 'DATA_ROOT', tmp_path)
    monkeypatch.setattr(coordinator, '_lock', lambda _: nullcontext())
    run = tmp_path / 'data' / view.RUN_RELATIVE
    coordinator.register(run, [route], current=NOW)
    monkeypatch.setattr(coordinator, '_factory', lambda: pytest.fail('status imported producer'))
    status = view.opening_daily_view(tmp_path / 'data', now=NOW)
    assert status['routes'][0]['family'] == 'dated_catchup'
    assert status['routes'][0]['case_count'] == 4 and status['factory_called'] is False
    details = opening_daily_detail(tmp_path / 'data', 'dated_catchup', now=NOW)
    assert details['cases'][0]['intent']['remaining_exit_symbols'] == [A]
    assert details['cases'][1]['intent']['weights'] is None
    ledger = opening_daily_ledger(tmp_path / 'data', 'dated_catchup', positions.DATED_CASE_IDS[0], now=NOW)
    assert ledger['status'] == 'saved_completed_research_ledger'
    assert ledger['cash']['total_cents'] == 9_000_000 and ledger['positions'][0]['quantity'] == 300
    assert ledger['positions'][0]['sellable_on_ledger_day'] == 0
    assert ledger['fill_count'] == ledger['order_count'] == 0
    assert before == {path: path.read_bytes() for path in directory.rglob('*') if path.is_file()}
    code = ('import sys; from trading.research.opening_daily import status; '
        'r=status(sys.argv[1]); assert r["routes"][0]["family"]=="dated_catchup"; '
        'assert "trading.engine" not in sys.modules; '
        'assert "trading.research.leader_dated_catchup_paper" not in sys.modules; '
        'assert "trading.research.leader_dated_catchup_forward" not in sys.modules')
    checked = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', code, str(run)],
        capture_output=True, text=True, env={**os.environ, 'QUANT_TRADING_HOME': str(tmp_path)})
    assert checked.returncode == 0, checked.stdout + checked.stderr
