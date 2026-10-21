"""Fixed candidate policy over tiny saved frozen scores; no fit or real market."""
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.contracts import IntentKind, RunContext, TradingMode
from trading.research import shortline_native_preopen as policy
from trading.research import shortline_native_scoring as scoring
from trading.research.baostock_daily_slice import digest


D, E = '2026-10-15', '2026-10-16'


def _write(path, value):
    path.write_text(json.dumps(value, allow_nan=False), encoding='utf8')


@pytest.fixture
def saved(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromisoformat(E + 'T03:00:00+08:00')
            return value.astimezone(tz) if tz is not None else value
    monkeypatch.setattr(scoring, 'datetime', Clock)
    monkeypatch.setattr(policy, '_now', lambda: datetime.fromisoformat(E + 'T03:30:00+08:00'))
    calendar = tmp_path / 'calendar.json'
    _write(calendar, {'schema': 'cn_a_share_exchange_calendar_v1', 'calendar_id': 'tiny_2026',
        'year': 2026, 'closed_ranges': ['2026-10-01/2026-10-07'], 'open_sessions': []})
    model = tmp_path / 'manifest.json'
    _write(model, {'fixture': 'no weights loaded or fitted'})
    monkeypatch.setattr(scoring, 'load_generation', lambda _: SimpleNamespace(path=model))
    def fixed_score(generation, frame, *, day, model_kind, feature_provenance):
        assert 'hit_5' not in frame
        scores = frame.copy()
        probabilities = [.05 + i / 100 for i in range(len(frame))]
        if model_kind == 'lgbm':
            probabilities = list(reversed(probabilities))
        scores['model_hit5_probability'] = probabilities
        scores['paper_candidate_only'] = False
        proof = {'schema': 'shortline_frozen_single_date_score_v1', 'day': day, 'model_kind': model_kind,
            'status': ('completed_frozen_single_date_probabilities_only' if feature_provenance['stage_membership_complete']
                else 'partial_known_stage_frozen_single_date_probabilities_only'),
            'scored_at': D + 'T22:00:00+08:00', 'score_available_at': D + 'T22:00:00+08:00',
            'model_available_at': D + 'T21:00:00+08:00', 'model_refit': False,
            'score_rows': len(scores), 'stage_counts': {str(s): int(scores.stage.eq(s).sum()) for s in (1, 2, 3)},
            'stage_membership_complete': feature_provenance['stage_membership_complete'],
            'unknown_stage_symbols': feature_provenance['unknown_stage_symbols'],
            'known_stage_partial_research_only': not feature_provenance['stage_membership_complete'],
            'batch_complete_for_target': False, 'score_only_not_candidate_batch': True,
            'target_generation_allowed': False, 'feature_source': feature_provenance,
            'feature_contract_id': feature_provenance['feature_contract_id'],
            'feature_semantics': feature_provenance['feature_semantics'],
            'generation_id': 'tiny_fixed_generation', 'manifest_file': str(model), 'manifest_sha256': digest(model),
            **scoring.FLAGS}
        return {'scores': scores, 'provenance': proof}
    monkeypatch.setattr(scoring, 'score_day', fixed_score)

    def make(name, *, partial=False, small=False):
        folder = tmp_path / name
        folder.mkdir()
        stages = [1, 2] if small else [1] * 10 + [2, 2]
        rows = [{'event_id': f'600{i:03d}.SSE|{D}|s{stage}', 'symbol': f'600{i:03d}.SSE',
            'date': D, 'stage': stage, 'stage_reference_known': True, 'decision_eligible': True,
            'hit_5': 999, **{key: .1 for key in scoring.FEATURES}}
            for i, stage in enumerate(stages, 1)]
        if partial:
            rows.append({'event_id': f'301569.SZSE|{D}|sunknown', 'symbol': '301569.SZSE', 'date': D,
                'stage': None, 'stage_reference_known': False, 'decision_eligible': False,
                'hit_5': 999, **{key: .1 for key in scoring.FEATURES}})
        features = folder / 'latest_features.csv'
        pd.DataFrame(rows).to_csv(features, index=False)
        scope = {'stage_membership_complete': not partial, 'unknown_stage_symbols': ['301569.SZSE'] if partial else [],
            'source_rows': len(rows), 'ordinary_rows': len(rows), 'valid_scope_rows': len(rows),
            'stage123_event_count': len(stages), 'full_market_stage_membership_complete': False}
        panel = folder / 'panel.json'
        _write(panel, {'feature_contract_id': 'native_active_known_nonST_observed_nine_features_v1',
            'execution_admitted': False, 'strategy_validated': False,
            'latest_features_path': str(features), 'latest_features_sha256': digest(features),
            'last_source_day': D, 'available_at': D + 'T18:00:00+08:00',
            'feature_semantics': {'scope': 'synthetic own-day observed-only test'}, 'latest_scope': scope})
        output = folder / 'scores'
        scoring.score_saved_generation(model, panel, output)
        return output / 'publication.json'
    return SimpleNamespace(make=make, calendar=calendar, root=tmp_path)


def test_complete_fixed_decile_does_not_force_small_stage_and_binds_true_source_day(saved):
    source = saved.make('complete')
    before = {file: digest(file) for file in source.parent.iterdir() if file.is_file()}
    out = saved.root / 'complete_plan'
    policy.build_preopen_plan(source, saved.calendar, out)
    loaded = policy.load_preopen_plan(out / 'plan.json')
    assert loaded['source_day'] == D and loaded['execution_day'] == E
    for kind, symbol in [('lr', '600010.SSE'), ('lgbm', '600001.SSE')]:
        decision = loaded['decisions'][kind]
        assert decision['decision'] == 'FULL_TARGET'
        assert decision['weights'] == {symbol: .16}
        bound = policy.bind_preopen_plan(out / 'plan.json', kind, RunContext('tiny_' + kind, TradingMode.REPLAY))
        assert bound.source_day == D and bound.execution_day == E
        assert bound.published_at.startswith(E)
        assert bound.intent.kind == IntentKind.FULL_TARGET and dict(bound.intent.weights) == {symbol: .16}
        assert bound.intent.decided_at is None and bound.intent.execution_not_before is None
    assert {file: digest(file) for file in before} == before
    assert all(not pd.read_csv(file).paper_candidate_only.any() for file in source.parent.glob('*_raw_scores.csv'))


@pytest.mark.parametrize('partial,small', [(True, False), (False, True)])
def test_unknown_scope_or_empty_decile_is_NO_CHANGE_not_empty_target(saved, partial, small):
    source = saved.make('nochange', partial=partial, small=small)
    out = saved.root / 'nochange_plan'
    policy.build_preopen_plan(source, saved.calendar, out)
    loaded = policy.load_preopen_plan(out)
    for kind in ('lr', 'lgbm'):
        assert loaded['decisions'][kind]['decision'] == 'NO_CHANGE'
        assert loaded['decisions'][kind]['weights'] is None
        bound = policy.bind_preopen_plan(out, kind, RunContext('nochange_' + kind, TradingMode.PAPER))
        assert bound.intent.kind == IntentKind.NO_CHANGE and not bound.intent.weights


def test_changed_publication_payload_and_missed_opening_cannot_publish(saved, monkeypatch):
    source = saved.make('changed')
    score = next(source.parent.glob('*_lr_raw_scores.csv'))
    score.write_bytes(score.read_bytes() + b'\n')
    with pytest.raises(ValueError):
        policy.build_preopen_plan(source, saved.calendar, saved.root / 'changed_plan')
    assert not (saved.root / 'changed_plan' / 'plan.json').exists()
    source = saved.make('late')
    monkeypatch.setattr(policy, '_now', lambda: datetime.fromisoformat(E + 'T09:30:00+08:00'))
    with pytest.raises(ValueError):
        policy.build_preopen_plan(source, saved.calendar, saved.root / 'late_plan')
    assert not (saved.root / 'late_plan' / 'plan.json').exists()


@pytest.mark.parametrize('partial,mode', [(False, TradingMode.REPLAY), (True, TradingMode.PAPER)])
def test_saved_plan_runs_through_shared_open_orders_fills_cash_and_T1(saved, monkeypatch, partial, mode):
    from trading import engine
    from trading.ledger import Ledger, new_state
    from trading.research.shortline_panel_replay import _config
    source = saved.make('integrated', partial=partial)
    out = saved.root / 'integrated_plan'
    policy.build_preopen_plan(source, saved.calendar, out)
    context = RunContext('integrated_native_' + mode.value, mode)
    intent = policy.bind_preopen_plan(out, 'lr', context)
    target, successor = '600010.SSE', '2026-10-19'
    days = ['2026-10-14', D, E, successor]
    row = {'open': 10., 'close': 10., 'preclose': 10., 'high': 10.2, 'low': 9.8,
        'volume': 1_000_000., 'amount': 10_000_000., 'tradestatus': 1, 'isST': 0, 'adjustflag': '3'}
    def forbidden_scores(*_):
        raise AssertionError('Execution-day close scores must never enter this opening decision')
    feed = SimpleNamespace(calendar=days, rows={day: {target: dict(row)} for day in days},
        volumes=pd.DataFrame({target: [1_000_000.] * len(days)}, index=days),
        instruments={target: {'ipoDate': '2000-01-01', 'outDate': ''}}, actions=[],
        next_day=lambda day: days[days.index(day) + 1], scores=forbidden_scores)
    monkeypatch.setattr(engine, '_published_now', lambda: datetime.fromisoformat(E + 'T20:00:00+08:00'))
    config = _config(100000, 5)
    state = new_state(99000 if partial else 100000)
    if partial:
        state['positions'][target] = [{'qty': 100, 'available': D}]
        state['prices'][target] = 1000
    daily = engine.advance(state, feed, E, config, 0, context=context, preopen_intent=intent)
    book = Ledger(state, config, context)
    if partial:
        assert daily['new_orders'] == 0 and not state['orders']
        assert book.quantity(target) == 100 and state['cash'] == 9_900_000
    else:
        order = state['orders'][E + ':' + target + ':buy']
        assert order['status'] == 'filled' and order['qty'] == 1600
        assert order['fees'] == 516 and state['cash'] == 8_397_884
        assert book.available(target, E) == 0 and book.available(target, successor) == 1600
    assert state['pending_targets'] is None
