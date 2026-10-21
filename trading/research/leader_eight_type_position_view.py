"""Read saved eight-type research positions and their already emitted risk facts.

No raw feed, risk evaluation, account advancement or network access is performed.
The original candidate publication stays separate from actual simulated fills.
"""
from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
import json
import math
from pathlib import Path

from .leader_daily_signals import _calendar_view
from .leader_eight_type_paper_view import CASE_KEYS, LOCAL, _stamp
from .leader_type_risk_exit import surviving_buy_anchor


SCHEMA = 'leader_eight_type_position_review_v1'
HEAD_SCHEMA = 'leader_eight_type_published_close_shared_ledger_checkpoint_v1'
CASE_IDS = tuple('__'.join(key) for key in CASE_KEYS)
RISK_PREFIX = 'leader_eight_type_published_close_structure_and_expiry_queue_v1:'
DATED_CASE_IDS = tuple('sector_catchup_leader__' + guard + '__' + capacity
    for guard in ('unguarded', 'two_close_envguard')
    for capacity in ('causal_first_seen_cap20', 'uncapped'))
KNOWN_CASE_IDS = tuple('cross_regime_candidate__' + guard + '__' + capacity
    for guard in ('unguarded', 'two_close_envguard')
    for capacity in ('causal_first_seen_cap20', 'uncapped'))


def saved_method(source_family='eight_type'):
    """Saved layouts only; never import a signal producer or its Engine runner."""
    if source_family == 'eight_type':
        return {'case_ids': CASE_IDS, 'head_schema': HEAD_SCHEMA,
            'publication_schema': 'leader_eight_type_unbound_close_paper_plan_v1',
            'account_prefix': 'leader-eight-type-forward:', 'risk_prefix': RISK_PREFIX}
    if source_family == 'dated_catchup':
        return {'case_ids': DATED_CASE_IDS, 'head_schema': 'leader_dated_catchup_shared_engine_checkpoint_v1',
            'publication_schema': 'leader_dated_catchup_unbound_close_paper_plan_v1',
            'account_prefix': 'leader-dated-catchup-forward:',
            'risk_prefix': 'dated_catchup_structure_expiry_sticky_queue_v1:',
            'method_id': 'dated_catchup_shared_engine_paper_signal_v1',
            'target_policy_id': 'dated_catchup_published_target_sticky_reduction_v1'}
    if source_family == 'eight_type_residual':
        return {'case_ids': CASE_IDS, 'head_schema': 'leader_eight_type_residual_shared_engine_checkpoint_v1',
            'publication_schema': 'leader_eight_type_residual_close_publication_v1',
            'account_prefix': 'leader-eight-type-residual-forward:',
            'risk_prefix': 'leader_eight_type_structure_expiry_sticky_queue_v1:',
            'method_id': 'leader_eight_type_residual_full_exit_paper_v1',
            'target_policy_id': 'leader_eight_type_published_target_sticky_reduction_v1'}
    if source_family == 'known_identity':
        return {'case_ids': KNOWN_CASE_IDS, 'head_schema': 'leader_known_identity_shared_engine_checkpoint_v1',
            'publication_schema': 'leader_known_identity_close_publication_v1',
            'account_prefix': 'leader-known-identity-forward:', 'risk_prefix': RISK_PREFIX,
            'method_id': 'known_positive_crossing_prospective_shared_engine_v1',
            'target_policy_id': 'leader_eight_type_published_close_fixed_expiry_reduction_v1'}
    raise ValueError('Unsupported saved leader account source family')


FLAGS = {'research_only': True, 'candidate_panel_only': True, 'execution_admitted': False,
    'selection_enabled': False, 'strategy_validated': False, 'current_signal_admitted': False,
    'engine_advanced': False, 'ledger_mutated': False, 'orders_generated': False,
    'account_created': False, 'broker_interacted': False, 'provider_contacted': False,
    'risk_recomputed': False, 'is_BrokerQuote': False, 'pit_verified': False,
    'corporate_actions_verified': False}


def _read(path):
    data = Path(path).read_bytes()
    record = json.loads(data.decode('utf-8-sig'))
    if not isinstance(record, dict):
        raise ValueError('Saved account evidence must be a JSON mapping')
    return record, {'file': str(Path(path).resolve()), 'sha256': sha256(data).hexdigest()}


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('Exact saved account session required')
    return value


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('Saved quantity or minor currency unit is invalid')
    return value


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('A saved finite number is required; unknown is not zero')
    return value


def _optional_price(value):
    if value is None:
        return None
    if _number(value) <= 0:
        raise ValueError('Saved structure prices must be positive or unknown')
    return value


def _boundary(record):
    if (record.get('candidate_panel_only') is not True or record.get('research_only') is not True
            or any(record.get(key) is not False for key in
                ('execution_admitted', 'selection_enabled', 'strategy_validated', 'current_signal_admitted'))):
        raise ValueError('Saved account must remain an isolated unvalidated research book')


def _verify(reference):
    record, actual = _read(reference['file'])
    if actual != {'file': str(Path(reference['file']).resolve()), 'sha256': reference['sha256']}:
        raise ValueError('Saved publication or journal bytes changed')
    return record


def _intent(value, context):
    if value is None:
        return None
    wrapper = value if value.get('schema') == 'replay_published_close_reduced_portfolio_intent_v1' else None
    if wrapper:
        value = wrapper['reduced_intent']
    if (value.get('contract_type') != 'PortfolioIntent' or value.get('context') != context
            or value.get('kind') not in ('FULL_TARGET', 'NO_CHANGE')):
        raise ValueError('Saved portfolio intent belongs to another account or contract')
    if wrapper and (wrapper['execution_not_before'] != value['execution_not_before']
            or wrapper['decision_day'] != _stamp(value['decided_at']).date().isoformat()):
        raise ValueError('Saved reduced intent has inconsistent queue dates')
    return {'kind': value['kind'], 'decision_day': _stamp(value['decided_at']).date().isoformat(),
        'execution_day': _day(value['execution_not_before']), 'weights': deepcopy(value['weights'])}


def _case(case_id, case, directory, sessions, publications, published_records, now, *, method=None):
    method = method or saved_method()
    identity, guard, capacity = case_id.split('__')
    context = case['context']
    expected_account = method['account_prefix'] + sha256(json.dumps(str(directory), sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()[:16] + ':' + case_id
    if (context.get('mode') != 'historical_replay' or context.get('account_id') != expected_account
            or case['state'].get('broker_binding')):
        raise ValueError('A position cannot be joined across research accounts or broker modes')
    state, daily = case['state'], case['daily']
    processed = [row['date'] for row in daily]
    if (processed != [pub['day'] for pub in publications[:len(processed)]]
            or len(processed) != len(case['journal']) or len(processed) != len(case['journal_files'])
            or state['day'] != (processed[-1] if processed else None)):
        raise ValueError('Saved case cursor/publication/journal prefix differs')
    last = daily[-1] if daily else None
    if method.get('method_id') in ('leader_eight_type_residual_full_exit_paper_v1',
            'known_positive_crossing_prospective_shared_engine_v1') and daily:
        risk = {'schema': 'replay_risk_exit_policy_v1',
            'policy_id': method['risk_prefix'] + identity, 'context': context}
        target = {'schema': 'replay_published_close_target_policy_v1',
            'policy_id': method['target_policy_id'], 'context': context}
        if (state.get('replay_risk_exit_policy') != risk
                or state.get('published_close_target_policy') != target):
            raise ValueError('Saved separate-method account policy or context changed')
    sealed, journal_ref = None, None
    if daily:
        journal_ref = case['journal_files'][-1]
        path = Path(journal_ref['file']).resolve()
        if path.parent != directory / 'journal' / case_id:
            raise ValueError('Saved journal belongs to another account package')
        sealed = _verify(journal_ref)
        if (sealed != case['journal'][-1] or sealed['decision_day'] != state['day']
                or sealed['publication'] != publications[len(daily)-1]
                or _stamp(sealed['committed_at']) > now):
            raise ValueError('Latest saved session differs from its sealed account journal')
        for key in ('pending_published_close_intent', 'pending_targets', 'pending_risk_exits'):
            if state.get(key) != sealed['post_session_' + key]:
                raise ValueError('Pending account action differs from saved journal')
        if abs(_number(last['cash']) - _integer(state['cash']) / 100) > 1e-7:
            raise ValueError('Saved daily and account cash differ')
    day = _day(state['day']) if state['day'] else None
    if day is not None and (day not in sessions or day > now.date().isoformat()):
        raise ValueError('Saved book cursor is not an observed calendar session')
    events = state['events']
    assessments = {}
    for event in events:
        if event.get('day') != day or event.get('kind') != 'leader_type_risk_assessed':
            continue
        symbol = event['symbol']
        if (symbol in assessments or event['type_id'] != identity
                or event['policy_id'] != method['risk_prefix'] + identity
                or event['status'] not in ('invalid', 'not_invalid', 'unknown')
                or event.get('execution_admitted') is not False):
            raise ValueError('Saved risk assessment is duplicate or belongs to another case')
        assessments[symbol] = event
    pending = state.get('pending_risk_exits')
    if pending and (pending['context'] != context or pending['decided_on'] not in sessions
            or pending['decided_on'] > day or pending['execution_on'] not in sessions
            or pending['execution_on'] <= pending['decided_on']):
        raise ValueError('Saved risk queue has a different context or date')
    positions = []
    for symbol, lots in state['positions'].items():
        quantity = sum(_integer(lot['qty']) for lot in lots)
        if not quantity:
            continue
        if day is None:
            raise ValueError('A held position needs its actual processed session')
        settled = sum(lot['qty'] for lot in lots if _day(lot['available']) <= day)
        reserved = sum(_integer(order['qty']) - _integer(order['filled'])
            for order in state['orders'].values() if order['symbol'] == symbol and order['side'] == 'sell'
            and order['status'] in ('submitted', 'partial', 'cancel_pending'))
        if not 0 <= reserved <= settled:
            raise ValueError('Saved sell reservations exceed dated settled lots')
        unit = state.get('price_units', {}).get(symbol, 'cent')
        if unit != 'cent':
            raise ValueError('Eight-type ordinary-equity marks must use original cent units')
        mark = state['prices'].get(symbol)
        mark_cny = _integer(mark, 1) / 100 if mark is not None else None
        anchor, reason = surviving_buy_anchor(state, symbol, sessions, day)
        event = assessments.get(symbol)
        entry = (event.get('entry') or {}) if event else {}
        if reason in ('actual_fill_history_invalid', 'actual_sell_exceeds_settled_buy_history',
                'actual_buy_settlement_unavailable'):
            raise ValueError('Saved fill history contradicts the dated research position: ' + reason)
        saved_fill = state['fills'].get(entry.get('fill_id')) if entry else None
        if saved_fill is not None and (saved_fill.get('symbol') != symbol or saved_fill.get('side') != 'buy'
                or saved_fill.get('day') != entry.get('fill_day')):
            raise ValueError('Saved assessment entry points to a different security or buy-fill day')
        if anchor and entry and any(entry.get(key) != anchor[key] for key in
                ('fill_id', 'fill_day', 'available_day', 'surviving_qty', 'decision_day')):
            raise ValueError('Saved risk entry does not match this position surviving FIFO fill')
        if entry and entry.get('candidate_source_day', entry.get('decision_day')) != entry.get('decision_day'):
            raise ValueError('Saved entry candidate belongs to another decision close')
        if entry and anchor:
            original = published_records.get(anchor['decision_day'])
            if original is None:
                raise ValueError('Actual fill origin publication is missing')
            original_case = next(row for row in original['cases'] if row['case_id'] == case_id)
            candidate = next((row for row in original_case['cap_candidates'] if row['symbol'] == symbol), None)
            if candidate is None or (entry.get('evidence') is not None
                    and entry['evidence'] != candidate.get('evidence', {})):
                raise ValueError('Saved entry facts do not match this case original buy candidate')
            setup = next((row for row in original_case.get('candidate_reference_and_setup', [])
                if row['symbol'] == symbol), None)
            if setup and setup.get('setup_low_cny') is not None and entry.get('setup_low') != setup['setup_low_cny']:
                raise ValueError('Held setup cannot be refreshed from a different publication low')
        if anchor:
            fill = state['fills'][anchor['fill_id']]
            if fill.get('price_unit', 'cent') != 'cent':
                raise ValueError('Saved equity fill must retain cent units')
            anchor = {'status': 'matched_saved_FIFO_fill', 'fill_id': anchor['fill_id'],
                'fill_day': anchor['fill_day'], 'decision_day': anchor['decision_day'],
                'fill_price_cny': _integer(fill['price'], 1) / 100,
                'available_day': anchor['available_day'],
                'remaining_earliest_fill_shares': anchor['surviving_qty'],
                'candidate_available_at': entry.get('candidate_available_at'),
                'setup_low_cny': _optional_price(entry.get('setup_low')),
                'setup_high_cny': _optional_price(entry.get('setup_high')),
                'setup_price_source': entry.get('setup_price_source'),
                'evidence': deepcopy(entry.get('evidence')) if entry else None}
        else:
            anchor = {'status': 'unknown', 'reason': reason, 'fill_id': None, 'fill_day': None,
                'decision_day': None, 'fill_price_cny': None, 'available_day': None,
                'remaining_earliest_fill_shares': None, 'setup_low_cny': None,
                'setup_high_cny': None, 'evidence': None}
        assessment = ({'day': event['day'], 'status': event['status'], 'reasons': deepcopy(event['reasons']),
            'facts': deepcopy(event['facts']), 'policy_id': event['policy_id']} if event else None)
        queued = ({**{key: pending[key] for key in ('execution_on', 'reason', 'source')},
            'decided_on': pending['decided_on'], 'execution_day_elapsed': pending['execution_on'] <= day}
            if pending and symbol in pending['symbols'] else None)
        positions.append({'symbol': symbol, 'quantity_shares': quantity,
            'sellable_on_book_day_shares': settled - reserved, 't1_locked_shares': quantity - settled,
            'sell_reserved_shares': reserved, 'mark_price_cny': mark_cny,
            'marked_value_cny': round(mark_cny * quantity, 2) if mark_cny is not None else None,
            'mark_is_latest_market_quote': False, 'mark_stale': symbol in (last or {}).get('stale_symbols', []),
            'anchor': anchor, 'assessment': assessment, 'queued_risk_exit': queued})
    if set(assessments) - {row['symbol'] for row in positions}:
        raise ValueError('Current-day saved risk assessment has no corresponding held position')
    return {'case_id': case_id, 'type': identity, 'guard': guard, 'capacity': capacity,
        'processed_day': day, 'next_session_after_book_day':
            sessions[sessions.index(day) + 1] if day and sessions.index(day) + 1 < len(sessions) else None,
        'context': deepcopy(context), 'position_count': len(positions),
        'cash_cny': _integer(state['cash']) / 100, 'equity_cny': _number(last['equity']) if last else None,
        'positions': positions, 'latest_journal': deepcopy(journal_ref),
        'last_commit_at': sealed['committed_at'] if sealed else None,
        'pending_decision': _intent(state.get('pending_published_close_intent'), context),
        'last_published_decision': _intent(sealed['bound_original_intent'], context) if sealed else None,
        'latest_order_blocks': [deepcopy(event) for event in events if event.get('day') == day
            and event.get('kind') in ('order_blocked', 'rejected')],
        'error': case.get('error'), 'blocking_evidence': deepcopy(case.get('blocking_evidence'))}


def read_saved_positions(run_dir, now=None, *, source_family='eight_type'):
    """Read one exact saved research head; missing/corrupt never falls back."""
    now = _stamp(now if now is not None else datetime.now(LOCAL))
    directory = Path(run_dir).resolve()
    method = saved_method(source_family)
    case_ids = method['case_ids']
    base = {'schema': SCHEMA, 'read_at': now.isoformat(), 'run_directory': str(directory),
        'head_ref': None, 'head_recorded_at': None, 'last_commit_at': None,
        'latest_processed_day': None, 'all_cases_same_day': None, 'case_count': None, 'rows': [],
        'cash_equity_units': 'CNY', 'share_units': 'shares', 'metrics': None, **FLAGS}
    if source_family == 'dated_catchup':
        base.update(schema='leader_dated_catchup_position_review_v1', source_family=source_family)
    elif source_family == 'eight_type_residual':
        base.update(schema='leader_eight_type_residual_position_review_v1', source_family=source_family)
    elif source_family == 'known_identity':
        base.update(schema='leader_known_identity_position_review_v1', source_family=source_family)
    path = directory / 'head.json'
    if not path.is_file():
        return {**base, 'status': 'unavailable', 'reason': 'no_saved_' + source_family + '_research_head'}
    try:
        head, reference = _read(path)
        _boundary(head)
        if (head['schema'] != method['head_schema'] or list(head['cases']) != list(case_ids)
                or str(Path(head['binding']['run_directory']).resolve()) != str(directory)
                or _stamp(head['registered_at']) > now):
            raise ValueError('The saved fixed thirty-two-account identity or package changed')
        if source_family == 'dated_catchup' and (
                head['binding']['protocol'].get('id') != method['method_id']
                or head['binding']['protocol'].get('cases') != list(case_ids)
                or head['binding'].get('published_target_policy_id') != method['target_policy_id']
                or head['binding'].get('risk_policy') != {'id': method['risk_prefix'][:-1]}):
            raise ValueError('Saved dated catchup method or sticky-exit binding changed')
        if source_family == 'eight_type_residual':
            protocol = head['binding']['protocol']
            if (protocol.get('id') != method['method_id']
                    or protocol.get('types') != list(dict.fromkeys(key[0] for key in CASE_KEYS))
                    or protocol.get('guards') != ['unguarded', 'two_close_envguard']
                    or protocol.get('capacities') != [{'id': 'causal_first_seen_cap20', 'max_symbols': 20},
                        {'id': 'uncapped', 'max_symbols': None}]
                    or protocol.get('case_count') != len(case_ids)
                    or protocol.get('combined_risk_policy_id') != method['risk_prefix'][:-1]
                    or protocol.get('target_policy_id') != method['target_policy_id']
                    or head['binding'].get('published_target_policy_id') != method['target_policy_id']
                    or head['binding'].get('risk_policy') != {'id': method['risk_prefix'][:-1]}):
                raise ValueError('Saved fixed eight-type residual method or sticky-exit binding changed')
        if source_family == 'known_identity':
            protocol = head['binding']['protocol']
            if (protocol.get('id') != method['method_id']
                    or protocol.get('types') != ['cross_regime_candidate']
                    or protocol.get('cases') != list(case_ids)
                    or protocol.get('guards') != ['unguarded', 'two_close_envguard']
                    or protocol.get('capacities') != [{'id': 'causal_first_seen_cap20', 'max_symbols': 20},
                        {'id': 'uncapped', 'max_symbols': None}]
                    or protocol.get('case_count') != len(case_ids)
                    or protocol.get('combined_risk_policy_id') != 'leader_eight_type_structural_plus_five_session_expiry_v1'
                    or protocol.get('entry_information_policy', {}).get('id')
                        != 'known_positive_crossing_candidate_identity_scope_v1'
                    or protocol.get('original_32_method_changed') is not False
                    or head['binding'].get('published_target_policy_id') != method['target_policy_id']
                    or head['binding'].get('risk_policy') != {'id': method['risk_prefix'][:-1]}):
                raise ValueError('Saved fixed known-identity crossing method or account binding changed')
        calendar = head['binding']['calendar']
        year = int(calendar['year'])
        sessions, _, status = _calendar_view(calendar, f'{year}-01-01', f'{year}-12-31', [])
        if status != 'pinned_static_exchange_calendar':
            raise ValueError('Saved exact exchange calendar required')
        published_records = {}
        for pub in head['publications']:
            record = _verify(pub)
            if (record.get('schema') != method['publication_schema']
                    or record['source_close_day'] != pub['day']
                    or record['recorded_at'] != pub['recorded_at']
                    or record['proposed_execution_day'] != pub['execution_day']
                    or _stamp(pub['recorded_at']) > now or pub['day'] in published_records
                    or [row['case_id'] for row in record['cases']] != list(case_ids)):
                raise ValueError('Saved publication date or actual clock differs')
            if source_family in {'dated_catchup', 'eight_type_residual', 'known_identity'} and (record.get('method_id') != method['method_id']
                    or record.get('protocol') != head['binding']['protocol']):
                raise ValueError('Saved leader publication belongs to a different method')
            _boundary(record)
            published_records[pub['day']] = record
        rows = [_case(key, head['cases'][key], directory, sessions, head['publications'], published_records, now,
            method=method) for key in case_ids]
        if sha256(path.read_bytes()).hexdigest() != reference['sha256']:
            raise ValueError('Saved account head changed during the read; refresh once it is stable')
        days = [row['processed_day'] for row in rows if row['processed_day']]
        commits = [row['last_commit_at'] for row in rows if row['last_commit_at']]
        return {**base, 'status': 'saved_research_positions', 'head_ref': reference,
            'head_recorded_at': head['registered_at'], 'last_commit_at': max(commits) if commits else None,
            'latest_processed_day': max(days) if days else None,
            'all_cases_same_day': len(set(row['processed_day'] for row in rows)) == 1,
            'case_count': len(rows), 'rows': rows,
            'saved_publications': deepcopy(head['publications']),
            'position_case_count': sum(bool(row['positions']) for row in rows),
            'case_symbol_position_count': sum(len(row['positions']) for row in rows),
            'unique_position_symbol_count': len({p['symbol'] for row in rows for p in row['positions']}),
            'semantics': 'saved research fills and emitted risk facts; no current market or broker observation'}
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError('Saved eight-type account evidence is malformed or incomplete') from error


def leader_eight_type_position_view(data_root, now=None):
    return read_saved_positions(Path(data_root) / 'research/leader_eight_type_forward/20261015_v1', now=now)
