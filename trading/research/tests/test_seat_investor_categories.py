"""Overlapping investor statistics cannot become independent seat alpha."""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

import pandas as pd

from trading.research.seats import (INVESTOR_CATEGORY_AGGREGATES, derived_seat_entity,
    normalize, observations)
from trading.research.seat_case_study import disclosure_events
from trading.research.seat_trend import event_signals, select_seats
from trading.research import bulk_seat_trend


def test_category_overlap_never_inflates_seats_or_net_and_preserves_original_keys_and_windows(monkeypatch):
    amounts = {"自然人": (60., 40.), "中小投资者": (40., 20.), "其他自然人": (20., 20.),
        "机构投资者": (40., 60.), "深股通投资者": (0., 0.)}
    branch = "中国银河证券股份有限公司北京陶然桥证券营业部"
    for reason, expected_window in (("严重异常期间日收盘价格涨幅偏离值累计达到100%的证券", "unknown_or_multi"),
            ("日换手率达到20%的前5只证券", "single")):
        source = [{"TRADE_DATE": "2026-09-02", "SECUCODE": "000017.SZ", "EXPLANATION": reason,
            "OPERATEDEPT_NAME": name, "OPERATEDEPT_CODE": "10000128629", "BUY": buy,
            "SELL": sell, "NET": buy - sell, "ACCUM_AMOUNT": 100.,
            "_raw_id": "buy:" + str(i), "_retrieved_at": "2026-09-26T12:00:00+08:00"}
            for i, (name, (buy, sell)) in enumerate(amounts.items())]
        assert sum(v['NET'] for v in source) == 20. and sum(v['NET'] > 0 for v in source) == 2
        original = deepcopy(source)
        sells = [{**v, "_raw_id": v["_raw_id"].replace("buy:", "sell:")} for v in source]
        frame = normalize(source, sells)
        assert source == original and len(frame) == 5
        assert set(frame.entity_kind) == {"investor_category_aggregate"}
        assert frame.reported_identity_usable.all() and not frame.identity_usable.any()
        assert not frame.seat_identity_usable.any() and not frame.eligible.any()
        assert frame.loc[frame.eligible, "net_cny"].sum() == 0
        assert set(frame.window_kind) == {expected_window}
        assert frame.window_start.isna().all() if expected_window != "single" else frame.window_start.eq("2026-09-02").all()
        # Same old identity keys and event-id recipe, not a reindex of facts.
        for row in frame.to_dict('records'):
            parts = [row['trade_date'], row['symbol'], row['window_kind'], row['seat_name'], row['seat_code'],
                str(row['buy_cny']), str(row['sell_cny']), str(row['period_turnover_cny']),
                reason if expected_window != 'single' else '']
            assert row['event_id'] == sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:20]
        saved = observations(source, sells)
        for row in saved:
            row.update(event_key='original:' + row['seat_name'], raw_file='saved-original.json.gz')
        saved_before = deepcopy(saved)
        events = disclosure_events(saved)
        assert saved == saved_before and {r['event_key'] for r in events} == {r['event_key'] for r in saved}
        assert all(r['reported_identity_usable'] and not r['identity_usable']
            and not r['seat_identity_usable'] and r['raw_listing_count'] == 2 for r in events)
        assert not any(r['amount_conflict'] for r in events)
        assert all(r['cross_event_amount_comparison'].startswith('not_comparable') for r in events)
        # Even a stale event table claiming eligible cannot select categories.
        stale = frame.copy()
        stale['eligible'] = True
        selected, _ = select_seats(stale, ('2026-09-01', '2026-09-03'), minimum=1)
        assert selected == []
        assert event_signals(stale, [], list(INVESTOR_CATEGORY_AGGREGATES),
            ['2026-09-02', '2026-09-03'], 1, .01) == {}

    ordinary = {**source[0], 'EXPLANATION': '日换手率达到20%的前5只证券', 'OPERATEDEPT_NAME': branch,
        'OPERATEDEPT_CODE': 'real-branch', 'BUY': 5., 'SELL': 0., 'NET': 5., '_raw_id': 'ordinary'}
    frame = normalize([ordinary], [])
    assert len(frame) == 1 and frame.iloc[0].seat_identity_usable and frame.iloc[0].eligible
    assert frame.iloc[0].identity_usable and frame.iloc[0].buy_cny == 5. and frame.iloc[0].sell_cny == 0.
    assert derived_seat_entity(branch, 1)['entity_kind'] == 'reported_named_seat_not_person_verified'
    # Exercise the saved-database consumer without opening or executing SQL.
    events = pd.concat([stale, frame], ignore_index=True)
    events['identity_usable'] = True  # legacy DB flags are deliberately still wrong
    events['event_key'] = events.event_id
    for field, value in (('raw_listings', 2), ('buy_versions', 1), ('sell_versions', 1), ('turnover_versions', 1)):
        events[field] = value
    summaries = pd.DataFrame(columns=['trade_date', 'symbol', 'window_kind'])
    monkeypatch.setattr(bulk_seat_trend.sqlite3, 'connect', lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(bulk_seat_trend.pd, 'read_sql_query', lambda query, *a, **kw:
        events.copy() if 'FROM seat_observations' in query else summaries.copy())
    derived, _, coverage = bulk_seat_trend.load_disclosures('never-opened.sqlite3', ['000017.SZSE'], '2026-09-01', '2026-09-03')
    assert derived.loc[derived.eligible, 'seat_name'].tolist() == [branch]
    assert coverage['investor_category_aggregate_events_excluded'] == 5
    aggregates = derived.loc[derived.entity_kind.eq('investor_category_aggregate')]
    assert aggregates.reported_identity_usable.all() and not aggregates.identity_usable.any()
