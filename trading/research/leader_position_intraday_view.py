"""Attach saved public observations to actual saved research positions.

This reads the existing fixed-cohort observations. It never collects quotes,
re-evaluates a close rule, advances an account, or replaces the entry anchor.
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

from .leader_candidate_intraday_view import _positive
from .leader_cohort_quote_view import leader_cohort_intraday_view
from .leader_eight_type_paper_view import LOCAL, _stamp


SCHEMA = 'saved_leader_position_public_quote_review_v1'
RULE_REQUIREMENTS = {
    'space_height_leader': 'qualified_close_and_complete_same_board_ladder',
    'relay_continuation_leader': 'qualified_close_limit_status',
    'liquidity_capacity_proxy': 'complete_close_amount_rank',
    'trend_leader': 'complete_close_sma20_sma60',
    'rebound_repair_candidate': 'close_vs_actual_entry_exact_previous_open',
    'cross_regime_candidate': 'close_vs_actual_entry_episode_structure_close',
    'sector_catchup_leader': 'close_vs_actual_entry_setup_low',
    'turnover_leader': 'close_vs_actual_entry_setup_low',
}
FLAGS = dict(provider_contacted=False, engine_advanced=False, ledger_mutated=False,
    orders_generated=False, execution_admitted=False, selection_enabled=False,
    strategy_validated=False, candidate_panel_only=True, is_BrokerQuote=False,
    risk_recomputed=False, broker_sellable_shares=None)


def build_position_tracking(positions, cohort, receipt, now):
    """Join by exact held symbol and adjacent book session; preserve saved facts."""
    clock = _stamp(now)
    result = deepcopy(positions)
    raw = {q['symbol']: q for q in receipt.get('quotes', [])}
    viewed = {q['symbol']: q for q in cohort.get('quotes', [])}
    if len(raw) != len(receipt.get('quotes', [])) or len(viewed) != len(cohort.get('quotes', [])):
        raise ValueError('Duplicate saved public quote identity')
    finished = _stamp(receipt['finished_at']) if receipt.get('finished_at') else None
    if finished is not None and finished > clock:
        raise ValueError('Public quote observation is not available yet')
    observed, fresh, symbols = set(), set(), set()
    queued = 0
    failed = str(cohort.get('status', '')).startswith('unavailable')
    for account in result.get('rows', []):
        day, expected = account.get('processed_day'), account.get('next_session_after_book_day')
        for held in account.get('positions', []):
            symbol = held['symbol']; symbols.add(symbol)
            queued += bool(held.get('queued_risk_exit'))
            q, v = raw.get(symbol), viewed.get(symbol)
            tracking = dict(status='missing_from_saved_cohort', snapshot_price_cny=None,
                source_quote_time=None, received_at=None, fresh_at_read=False,
                previous_close_cny=None, book_reference_status='unknown',
                price_change_from_book_percent=None,
                price_change_is_strategy_return=False,
                source_file=cohort.get('source_file'),
                rule_requirement=RULE_REQUIREMENTS.get(account['type'], 'saved_close_rule_required'),
                current_rule_status='not_evaluated_from_public_snapshot',
                fixed_intraday_stop_cny=None, saved_exit_queue_preserved=True, **FLAGS)
            if q is not None and v is not None:
                tracking.update(status='unavailable_latest_observation' if failed else 'unknown_saved_quote',
                    source_quote_time=q.get('source_quote_time'), received_at=q.get('received_at'))
                try:
                    source, received = _stamp(q['source_quote_time']), _stamp(q['received_at'])
                    exact = (not failed and q.get('current_quote') is True and q.get('source_status') == '00'
                        and day and expected and day < expected and q.get('source_date') == expected
                        and source.date().isoformat() == expected
                        and receipt.get('session_day') == expected
                        and (cohort.get('session') or {}).get('session_day') == expected
                        and v.get('source_quote_time') == q['source_quote_time']
                        and source <= received <= finished <= clock
                        and 0 <= (received - source).total_seconds() <= 120)
                except (KeyError, ValueError, TypeError):
                    exact = False
                price, previous = _positive(q.get('last_price_rmb')), _positive(q.get('previous_close_price_rmb'))
                if exact and price is not None and previous is not None:
                    observed.add(symbol)
                    current = bool(v.get('current_at_read') is True and clock.date().isoformat() == expected
                        and 0 <= (clock - source).total_seconds() <= 120)
                    if current: fresh.add(symbol)
                    mark = _positive(held.get('mark_price_cny'))
                    comparable = mark is not None and held.get('mark_stale') is False
                    reference = ('matched_book_close' if mark == previous else 'changed_or_incompatible') if comparable else 'unknown'
                    tracking.update(status='current_public_observation' if current else 'saved_snapshot_not_current',
                        snapshot_price_cny=float(price), previous_close_cny=float(previous), fresh_at_read=current,
                        book_reference_status=reference,
                        price_change_from_book_percent=float((price / mark - 1) * 100)
                            if reference == 'matched_book_close' else None)
            held['public_quote_tracking'] = tracking
    result['intraday_tracking'] = dict(schema=SCHEMA,
        status='saved_held_quote_observations' if observed else 'no_matching_held_quote_observations',
        read_at=clock.isoformat(), source_file=cohort.get('source_file'),
        source_scope='existing_fixed_590_cohort_not_the_held_universe',
        observation_asof=receipt.get('finished_at'),
        unique_position_symbols=len(symbols), observed_unique=len(observed), current_unique=len(fresh),
        missing_unique=len(symbols - observed), missing_symbols=sorted(symbols - observed),
        queued_exit_position_occurrences=queued, account_cash_equity_unchanged=True,
        account_sellable_as_of_saved_book_only=True, actual_entry_anchors_unchanged=True,
        reference_equality_is_not_action_verification=True, **FLAGS)
    return result


def attach_saved_position_quotes(positions, data_root, *, now=None):
    """Only reads existing evidence; a quote failure must not hide the holdings."""
    actual = _stamp(now or datetime.now(LOCAL))
    try:
        cohort = leader_cohort_intraday_view(data_root, now=actual)
        source = cohort.get('source_file')
        receipt = json.loads(Path(source).read_text(encoding='utf-8-sig')) if source else {}
        return build_position_tracking(positions, cohort, receipt, actual)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
        return {**positions, 'intraday_tracking': dict(schema=SCHEMA, status='unavailable',
            error=str(error), saved_positions_and_exits_preserved=True, **FLAGS)}
