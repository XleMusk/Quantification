from pathlib import Path
import json
from trading.research.f2_vertical_slice import BLOCKED, COMPLETED, run_vertical_slice


def fact(symbol, role, period_start, period_end, value, available='2024-04-30T12:00:00+08:00'):
    metric = 'total_assets' if role == 'assets' else 'parent_net_profit'
    period_kind = 'point_in_time' if role == 'assets' else 'single_quarter'
    return {'fact_id': f'{symbol}:{role}', 'issuer_id': symbol, 'metric_id': metric,
            'period_start': period_start, 'period_end': period_end, 'period_kind': period_kind,
            'scope_group': 'consolidated_parent', 'currency': 'CNY', 'unit': 'yuan',
            'value': str(value), 'available_at': available, 'status': 'OBSERVED'}


def historical(symbol):
    return {
        'symbol': symbol, 'period': '2024Q1', 'evidence_kind': 'historical_archive',
        'publication_date': '2024-04-27',
        'retrieved_at': '2024-04-30T12:00:00+08:00',
        'asof_at': '2024-04-30T12:00:00+08:00',
        'revision_id': f'{symbol}-2024Q1-v1',
        'source_file': f'{symbol}_2024Q1.pdf', 'source_sha256': 'a' * 64,
    }


def spec(available='2024-04-30T12:00:00+08:00'):
    return {
      'decision_day': '2024-05-01', 'execution_day': '2024-05-02', 'settlement_day': '2024-05-03',
      'initial_cash': 100000, 'quotes': {'000001.SZSE': 10.0, '600000.SSE': 12.0},
      'events': [
        {'symbol': '000001.SZSE', 'historical_evidence': historical('000001.SZSE'), 'current': fact('000001.SZSE','current','2024-01-01','2024-03-31',1200000,available),
         'prior': fact('000001.SZSE','prior','2023-01-01','2023-03-31',900000,available),
         'assets': fact('000001.SZSE','assets','2024-01-01','2024-03-31',50000000,available)},
        {'symbol': '600000.SSE', 'historical_evidence': historical('600000.SSE'), 'current': fact('600000.SSE','current','2024-01-01','2024-03-31',900000,available),
         'prior': fact('600000.SSE','prior','2023-01-01','2023-03-31',600000,available),
         'assets': fact('600000.SSE','assets','2024-01-01','2024-03-31',30000000,available)},
      ]}


def test_ready_f2_vertical_slice_reaches_paper_package(tmp_path):
    result = run_vertical_slice(spec(), tmp_path)
    assert result['status'] == COMPLETED
    assert result['execution_admitted'] is False
    assert result['account_replay'] is True
    assert result['package']['paper_order_count'] == 2
    assert result['package']['cost_model']['id'] == 'cn_a_share_standard_ledger_costs_v1'
    assert result['package_dir']
    assert Path(result['receipt_path']).is_file()
    assert set(result['target_portfolio']['weights']) == {'000001.SZSE', '600000.SSE'}


def test_late_fact_blocks_before_signal_or_account(tmp_path):
    result = run_vertical_slice(spec('2024-05-02T12:00:00+08:00'), tmp_path)
    assert result['status'] == BLOCKED
    assert result['account_replay'] is False
    assert result['package'] is None
    assert result['signal']['status'] == 'UNAVAILABLE'
    assert 'factor_available_after_decision' in result['signal']['reason'] or 'PRE_EVENT_UNPROVEN' in str(result['facts'])


def test_missing_historical_evidence_blocks_before_fact_materialization(tmp_path):
    current = spec()
    current['events'][0].pop('historical_evidence')
    result = run_vertical_slice(current, tmp_path)
    assert result['status'] == BLOCKED
    assert result['reason'] == 'historical_evidence_blocked'
    assert result['account_replay'] is False
    assert result['facts'] == []
