from trading.research.idea_frontier import _input_key


def test_input_key_separates_execution_costs_but_not_candidate_rules():
    base = {
        "inputs": {"price_view": "prices", "event_view": "events"},
        "start": "2024-01-01",
        "end": "2024-12-31",
        "protocol": {
            "portfolio": {
                "initial_cash": 100000,
                "commission": 0.0003,
                "minimum_commission": 5,
                "sell_tax": 0.0005,
                "slippage_bps": 10,
                "participation": 0.01,
            }
        },
    }

    changed_cost = {
        **base,
        "protocol": {
            **base["protocol"],
            "portfolio": {**base["protocol"]["portfolio"], "slippage_bps": 20},
        },
    }
    changed_rule = {
        **base,
        "protocol": {**base["protocol"], "donchian_rule": {"window": 55}},
    }

    assert _input_key(base) != _input_key(changed_cost)
    assert _input_key(base) == _input_key(changed_rule)
