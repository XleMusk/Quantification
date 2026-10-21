"""Preserve one dated official SZSE risk-warning report, without trade status.

This is a source adapter. Report absence is not silently turned into a current
non-ST or tradability claim; account and price consumers remain independent.
"""
from datetime import datetime
from hashlib import sha256
import argparse
import json
from pathlib import Path

from ..contracts import MARKET_TZ

URL = 'https://www.szse.cn/api/report/ShowReport/data'
PAGE_URL = 'https://www.szse.cn/disclosure/listed/warn/index.html'
SCHEMA = 'official_szse_dated_risk_warning_source_v1'
FLAGS = dict(execution_admitted=False, selection_enabled=False, strategy_validated=False,
    candidate_panel_only=True, account_accessed=False, ledger_mutated=False,
    is_BrokerQuote=False, historical_pit_verified=False, tradability_verified=False)


def _now():
    return datetime.now(MARKET_TZ)


def _stamp(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('Actual source clock must carry a timezone')
    return result.astimezone(MARKET_TZ)


def _save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def _tab(payload, day, page):
    if not isinstance(payload, list):
        raise ValueError('Official report response is not a tab array')
    matches = [item for item in payload if item.get('metadata', {}).get('tabkey') == 'tab1']
    if len(matches) != 1:
        raise ValueError('Exactly one risk-warning tab required; delisting tab is separate')
    tab = matches[0]; meta = tab['metadata']
    if (meta.get('catalogid') != 'fxjsb' or meta.get('name') != '风险警示股票'
            or str(meta.get('subname', '')).strip() != day
            or int(meta['pageno']) != page or meta.get('error') not in (None, '', '0', 0)):
        raise ValueError('Risk-warning report day, page or catalog changed')
    size, count, pages = (int(meta[key]) for key in ('pagesize', 'recordcount', 'pagecount'))
    if size < 1 or count < 1 or pages != (count + size - 1) // size or pages > 25 or count > 500:
        raise ValueError('Unexpected official report inventory; stop rather than truncate')
    rows = tab['data']
    if not isinstance(rows, list) or len(rows) != min(size, count - (page - 1) * size):
        raise ValueError('Official report page is incomplete')
    for row in rows:
        code = row.get('zqdm')
        if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
            raise ValueError('Original report security code must remain six literal digits')
        if not isinstance(row.get('gsjc'), str):
            raise ValueError('Original report name must remain a string')
    return meta, rows


def collect(day, out, *, clock=None, get=None):
    """One bounded serial pass; preserve bytes before parsing; no retry."""
    import requests
    clock, get = clock or _now, get or requests.get
    started = _stamp(clock())
    if started.date().isoformat() != day:
        raise ValueError('Only an actual current-day official report may be acquired')
    out = Path(out).resolve(); out.mkdir(parents=True, exist_ok=False)
    receipt = dict(schema=SCHEMA, status='started', day=day, source_url=URL, source_page=PAGE_URL,
        started_at=started.isoformat(), completed_at=None, source_available_at=None,
        raw_response_preserved=False, pages=[], full_report_complete=False,
        reported_count=None, reported_page_count=None, ordinary_risk_symbols=[],
        all_reported_codes=[], report_absence_is_non_ST_proof=False,
        negative_tradability_evidence_usable=False, automatic_retry=False,
        reason_for_new_acquisition='first_complete_raw_capture_after_unarchived_metadata_probe', **FLAGS)
    _save(out / 'receipt.json', receipt)
    try:
        page, expected, all_rows = 1, None, []
        while expected is None or page <= expected[2]:
            before = _stamp(clock())
            params = dict(SHOWTYPE='JSON', CATALOGID='fxjsb', TABKEY='tab1', PAGENO=str(page))
            observation = dict(page=page, params=params, requested_at=before.isoformat(),
                response_completed_at=None, status_code=None, raw_file=f'page_{page:03d}.raw.json')
            receipt['pages'].append(observation); _save(out / 'receipt.json', receipt)
            with get(URL, params=params, timeout=(10, 20), allow_redirects=False, stream=True,
                    headers={'Referer': PAGE_URL, 'User-Agent': 'Mozilla/5.0'}) as response:
                observation['status_code'] = response.status_code
                observation['response_headers'] = {k: response.headers.get(k) for k in ('Date', 'Content-Type', 'Content-Length', 'Location')}
                size = 0
                with (out / observation['raw_file']).open('xb') as handle:
                    for piece in response.iter_content(16384):
                        handle.write(piece); size += len(piece)
                        if size > 262144:
                            raise ValueError('Official report page exceeds the bounded size')
                finished = _stamp(clock())
                observation['response_completed_at'] = finished.isoformat()
                raw = (out / observation['raw_file']).read_bytes()
                observation.update(byte_count=len(raw), sha256=sha256(raw).hexdigest())
                receipt['raw_response_preserved'] = True
                _save(out / 'receipt.json', receipt)
                if response.status_code != 200:
                    raise ValueError('Expected one nonredirect HTTP200; stop without retry')
            if finished.date().isoformat() != day or finished < before:
                raise ValueError('Source acquisition crossed the requested date or clock reversed')
            meta, rows = _tab(json.loads(raw), day, page)
            inventory = tuple(int(meta[key]) for key in ('pagesize', 'recordcount', 'pagecount'))
            if expected is not None and inventory != expected:
                raise ValueError('Official report inventory changed between pages')
            expected = inventory; all_rows.extend(rows)
            observation['metadata'] = meta
            page += 1
        codes = [row['zqdm'] for row in all_rows]
        if len(codes) != expected[1] or len(set(codes)) != len(codes):
            raise ValueError('Full official report count or unique identity differs')
        receipt.update(status='completed_full_dated_risk_warning_report', full_report_complete=True,
            reported_count=expected[1], reported_page_count=expected[2], all_reported_codes=codes,
            ordinary_risk_symbols=[code + '.SZSE' for code in codes if code.startswith(('0', '3'))],
            source_available_at=finished.isoformat())
    except (Exception, KeyboardInterrupt) as error:
        receipt.update(status='failed_single_pass_no_retry', error=f'{type(error).__name__}: {error}')
    receipt['completed_at'] = _stamp(clock()).isoformat()
    _save(out / 'receipt.json', receipt)
    return receipt


def load(source, *, as_of=None):
    """Re-read the saved original pages; partial/old/future inputs stay unknown."""
    actual = _stamp(as_of or _now()); source = Path(source).resolve()
    receipt = json.loads((source / 'receipt.json').read_text(encoding='utf-8-sig'))
    base = dict(schema=SCHEMA + '_view', status='unavailable', day=receipt.get('day'),
        source=str(source), source_available_at=receipt.get('source_available_at'),
        ordinary_risk_symbols=[], full_report_complete=False, report_absence_is_non_ST_proof=False, **FLAGS)
    if (receipt.get('schema') != SCHEMA or any(receipt.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError('Official risk source scope or research boundary changed')
    if receipt.get('status') != 'completed_full_dated_risk_warning_report':
        return {**base, 'reason': 'source_incomplete_or_failed'}
    if (_stamp(receipt['source_available_at']) > actual or _stamp(receipt['completed_at']) > actual
            or actual.date().isoformat() != receipt['day']):
        return {**base, 'reason': 'not_same_day_or_not_yet_available'}
    all_rows, expected = [], None
    preceding = _stamp(receipt['started_at'])
    for page, observation in enumerate(receipt['pages'], 1):
        if observation['raw_file'] != f'page_{page:03d}.raw.json':
            raise ValueError('Official page identity changed')
        raw = (source / observation['raw_file']).read_bytes()
        if len(raw) != observation['byte_count'] or sha256(raw).hexdigest() != observation['sha256']:
            raise ValueError('Original official report bytes changed')
        meta, rows = _tab(json.loads(raw), receipt['day'], page)
        began, ended = _stamp(observation['requested_at']), _stamp(observation['response_completed_at'])
        if not preceding <= began <= ended <= _stamp(receipt['source_available_at']) <= _stamp(receipt['completed_at']):
            raise ValueError('Saved official source acquisition clocks changed')
        preceding = ended
        inventory = tuple(int(meta[key]) for key in ('pagesize', 'recordcount', 'pagecount'))
        if observation['status_code'] != 200 or (expected is not None and expected != inventory):
            raise ValueError('Official report page set changed')
        expected = inventory; all_rows.extend(rows)
    codes = [row['zqdm'] for row in all_rows]
    ordinary = [code + '.SZSE' for code in codes if code.startswith(('0', '3'))]
    if (expected is None or len(receipt['pages']) != expected[2] or len(codes) != expected[1]
            or len(codes) != len(set(codes)) or codes != receipt['all_reported_codes']
            or ordinary != receipt['ordinary_risk_symbols'] or receipt['full_report_complete'] is not True
            or receipt['reported_count'] != expected[1] or receipt['reported_page_count'] != expected[2]
            or preceding != _stamp(receipt['source_available_at'])):
        raise ValueError('Saved official risk report completeness differs from the source pages')
    return {**base, 'status': 'saved_same_day_official_positive_risk_warning_report',
        'full_report_complete': True, 'ordinary_risk_symbols': ordinary, 'reported_count': len(codes),
        'current_ST_false_inference_admitted': False, 'tradable_inference_admitted': False}


def assess(source, symbol, *, as_of=None):
    value = load(source, as_of=as_of)
    matched = value['status'] == 'saved_same_day_official_positive_risk_warning_report' and symbol in value['ordinary_risk_symbols']
    return dict(symbol=symbol, day=value['day'], source=value['source'], source_available_at=value['source_available_at'],
        source_status=value['status'], reported_risk_warning_member=True if matched else None,
        is_st=True if matched else None, tradable=None,
        reason='official_positive_risk_warning_member' if matched else 'not_proven_by_this_positive_report', **FLAGS)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    c = commands.add_parser('collect'); c.add_argument('--day', required=True); c.add_argument('--out', type=Path, required=True)
    q = commands.add_parser('assess'); q.add_argument('--source', type=Path, required=True); q.add_argument('--symbol', required=True)
    args = parser.parse_args(argv)
    result = collect(args.day, args.out) if args.action == 'collect' else assess(args.source, args.symbol)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 1 if result.get('status') == 'failed_single_pass_no_retry' else 0


if __name__ == '__main__':
    raise SystemExit(main())
