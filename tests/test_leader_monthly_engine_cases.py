from copy import deepcopy

from trading.research import leader_monthly_engine_cases as m


def _sidecar(orders):
    return {"case_key": "original_case", "original_case": {"family": m.FAMILIES[0]},
            "order_annotations": orders}


def _order(day, symbol, sequence, *, filled, price):
    identifier = day + ":" + symbol + ":buy"
    return {"order_id": identifier, "original_order": {"id": identifier,
        "side": "buy", "symbol": symbol, "filled": filled, "limit_price": price,
        "status": "filled" if filled else "cancelled"},
        "signal_link": {"signal_date": day, "target_sequence": sequence,
            "status": "bound_by_original_ledger_event_sequence"},
        "signal_date_phase": {"phase": m.STRENGTH}}


def test_first_original_buy_ignores_fill_outcome_price_and_future_coverage():
    original = [_order("2026-01-06", "000002.SZSE", 2, filled=100, price=200),
                _order("2026-01-06", "000001.SZSE", 2, filled=0, price=100),
                _order("2026-01-07", "000000.SZSE", 3, filled=100, price=1)]
    days = [{"day": "2026-01-06", "phase": m.STRENGTH}]
    first = m.select_monthly_cases([_sidecar(original)], days, months=["2026-01"])
    assert first[1][0]["symbol"] == "000001.SZSE"
    altered = deepcopy(original)
    for row in altered:
        row["original_order"].update(filled=999, limit_price=999999, status="filled")
        row["future_coverage"] = "missing"
        row["future_return"] = -100
    second = m.select_monthly_cases([_sidecar(altered)], days, months=["2026-01"])
    assert [x["order_id"] for x in first[1]] == [x["order_id"] for x in second[1]]
    assert first[0] == second[0]


def test_full_grid_retains_no_phase_no_buy_and_unbound_buy_separately():
    row = _order("2026-01-06", "000001.SZSE", 2, filled=0, price=100)
    row["signal_link"]["signal_date"] = None
    days = [{"day": "2026-01-06", "phase": m.STRENGTH}]
    cells, samples, unassigned = m.select_monthly_cases([_sidecar([row])], days,
                                                       months=["2026-01", "2026-02"])
    assert len(cells) == 2 * 5 * 3
    assert not samples and len(unassigned) == 1
    assert cells[0]["status"] == "observed_phase_no_original_buy"
    assert cells[1]["status"] == "no_observed_phase_in_month"
    assert all(cell["selected_order_id"] is None for cell in cells)
