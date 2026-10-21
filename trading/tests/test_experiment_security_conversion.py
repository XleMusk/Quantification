"""The optional conversion method survives the existing replay/checkpoint path."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, SignalUnavailable, TradingMode
from trading.ledger import Ledger
from trading.research.experiment import load_replay_checkpoint, replay
from trading.security_conversion import ConversionCredit, SecurityConversionReceipt, SecurityConversionView


A, B, C = "600001.SSE", "600002.SSE", "600003.SSE"
D, E, F, G, H, I = "2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10", "2025-01-13"
CALENDAR = ["2025-01-03", D, E, F, G, H, I]
DATES = [D, E, F, G, H]
CONTEXT = RunContext("standalone_replay", TradingMode.REPLAY)
CONFIG = StrategyConfig(initial_cash=10000, holdings=1, retain_rank=2, gross_limit=.8,
                        single_limit=.8, rebalance_sessions=1, slippage_bps=0)


def feed():
    rows = {}
    for day in CALENDAR:
        rows[day] = {symbol: dict(open=price, high=price*1.01, low=price*.99, close=price,
            preclose=price, volume=1_000_000., amount=20_000_000., tradestatus=1, isST=0, adjustflag="3")
            for symbol, price in ((A, 10.), (B, 5.), (C, 20.))
            if not (symbol == A and day >= F or symbol == B and day >= G)}
    def scores(day, config):
        if day == D:
            return [{"symbol": A, "score": 1.}]
        return [] if day == G else SignalUnavailable("no_new_actual_signal")
    return SimpleNamespace(calendar=CALENDAR, rows=rows,
        volumes=pd.DataFrame(1_000_000., index=CALENDAR, columns=[A, B, C]),
        instruments={A: {"ipoDate": "2000-01-01", "outDate": F},
                     B: {"ipoDate": "2000-01-01", "outDate": G},
                     C: {"ipoDate": "2000-01-01", "outDate": ""}},
        actions=[], next_day=lambda day: CALENDAR[CALENDAR.index(day)+1], scores=scores)


def view(receipts=(), context=CONTEXT):
    return SecurityConversionView("experiment_conversion_fixture", context, "dated_asof", tuple(receipts))


def read(folder, name):
    return json.loads((folder/name).read_text(encoding="utf-8"))


def receipt(tmp_path, state, identifier, effective_day, source_symbol, destination, multiplier, cash):
    source = tmp_path/(identifier+"_source.json")
    source.write_text(json.dumps({"synthetic_explicit_allocation": identifier}), encoding="utf-8")
    lots = state["positions"][source_symbol]
    previous = CALENDAR[CALENDAR.index(effective_day)-1]
    return SecurityConversionReceipt(CONTEXT, identifier, effective_day, source_symbol, destination,
        sum(lot["qty"] for lot in lots),
        tuple(ConversionCredit(lot["lot_id"], lot["qty"], int(lot["qty"]*multiplier),
            CALENDAR[CALENDAR.index(effective_day)+1]) for lot in lots), cash,
        str(source.resolve()), hashlib.sha256(source.read_bytes()).hexdigest(),
        previous+"T18:00:00+08:00", previous+"T18:01:00+08:00")


def test_replay_checkpoint_append_two_sources_and_continue_real_engine_without_fake_conversion_fills(tmp_path):
    base = feed()
    first, second, final = (tmp_path/name for name in ("first", "second", "final"))
    replay(base, CONFIG, DATES[:2], first, security_conversion_view=view())
    a_state = read(first, "account.json")
    assert Ledger(a_state, CONFIG).quantity(A) == 800 and len(a_state["fills"]) == 2
    first_receipt = receipt(tmp_path, a_state, "first_credit", F, A, B, 2, 123)
    checkpoint = load_replay_checkpoint(first, CONFIG, DATES[:3], security_conversion_view=view((first_receipt,)))
    replay(base, CONFIG, DATES[:3], second, checkpoint=checkpoint,
           security_conversion_view=view((first_receipt,)))
    b_state = read(second, "account.json")
    assert Ledger(b_state, CONFIG).quantity(B) == 1600 and Ledger(b_state, CONFIG).available(B, F) == 0
    assert b_state["fills"] == a_state["fills"] and b_state["cash"] == a_state["cash"]+123
    second_receipt = receipt(tmp_path, b_state, "second_credit", G, B, C, .25, 77)
    latest = view((first_receipt, second_receipt))
    checkpoint = load_replay_checkpoint(second, CONFIG, DATES, security_conversion_view=latest)
    result = replay(base, CONFIG, DATES, final, checkpoint=checkpoint, security_conversion_view=latest)
    state, record = read(final, "account.json"), read(final, "replay.json")
    assert result["reused_sessions"] == 3 and result["newly_completed_sessions"] == 2
    assert Ledger(state, CONFIG).quantity(C) == 0 and len(state["orders"]) == 2 and len(state["fills"]) == 4
    assert sum(fill["qty"] for fill in state["fills"].values() if fill["side"] == "sell") == 400
    assert len(state["security_conversion_applied"]) == 2
    assert record["security_conversion_binding"] == state["security_conversion_binding"]
    assert record["security_conversion_binding"]["calendar"] == CALENDAR
    assert record["security_conversion_catalog"] == [first_receipt.to_dict(), second_receipt.to_dict()]
    assert [item["source_sha256"] for item in record["security_conversion_sources"]] == [
        first_receipt.source_sha256, second_receipt.source_sha256]
    frozen = deepcopy(state)
    loaded = load_replay_checkpoint(final, CONFIG, DATES, security_conversion_view=latest)
    assert loaded["state"] == frozen
    assert read(first, "account.json") == a_state and read(second, "account.json") == b_state


def test_checkpoint_rejects_disable_retrofit_changed_receipt_and_changed_proof(tmp_path):
    base, old, bound = feed(), tmp_path/"old", tmp_path/"bound"
    replay(base, CONFIG, DATES[:2], old)
    with pytest.raises(ValueError):
        load_replay_checkpoint(old, CONFIG, DATES[:3], security_conversion_view=view())
    replay(base, CONFIG, DATES[:2], bound, security_conversion_view=view())
    with pytest.raises(ValueError):
        load_replay_checkpoint(bound, CONFIG, DATES[:3])
    original = receipt(tmp_path, read(bound, "account.json"), "future_credit", F, A, B, 2, 123)
    declared = tmp_path/"declared"
    initial = load_replay_checkpoint(bound, CONFIG, DATES[:2], security_conversion_view=view((original,)))
    replay(base, CONFIG, DATES[:2], declared, checkpoint=initial, security_conversion_view=view((original,)))
    with pytest.raises(ValueError):
        load_replay_checkpoint(declared, CONFIG, DATES[:3], security_conversion_view=view((replace(original, cash_cents=124),)))
    from pathlib import Path
    Path(original.source_file).write_text("changed source bytes", encoding="utf-8")
    with pytest.raises(ValueError):
        load_replay_checkpoint(declared, CONFIG, DATES[:3], security_conversion_view=view((original,)))


def test_invalid_fresh_context_fails_before_output_and_default_receipt_remains_legacy(tmp_path):
    base, bad = feed(), tmp_path/"bad"
    with pytest.raises(ValueError):
        replay(base, CONFIG, DATES[:2], bad,
               security_conversion_view=view(context=RunContext("other", TradingMode.REPLAY)))
    assert not bad.exists()
    source = tmp_path/"future_proof.json"
    source.write_text("actual fixture bytes", encoding="utf-8")
    invalid_proof = SecurityConversionReceipt(CONTEXT, "invalid_future_proof", F, A, B, 100,
        (ConversionCredit("explicit_future_lot", 100, 200, G),), 0, str(source.resolve()), "0"*64,
        E+"T18:00:00+08:00", E+"T18:01:00+08:00")
    with pytest.raises(ValueError, match="SHA256"):
        replay(base, CONFIG, DATES[:2], bad, security_conversion_view=view((invalid_proof,)))
    assert not bad.exists()
    first, second = tmp_path/"legacy_default", tmp_path/"legacy_explicit_none"
    replay(base, CONFIG, DATES[:2], first)
    replay(base, CONFIG, DATES[:2], second, security_conversion_view=None)
    assert read(first, "account.json") == read(second, "account.json")
    assert read(first, "replay.json") == read(second, "replay.json")
    assert not any(key.startswith("security_conversion") for key in read(first, "replay.json"))
