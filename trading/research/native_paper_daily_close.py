"""Optional end-of-day completion of existing native intraday PAPER accounts.

The caller supplies an explicit saved closing observation. The original
opening head owns D, E, plan and prior source; the new E->F plan is irrelevant.
Execution/recovery uses the existing one-shot daily command supervisor.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from .. import native_intraday_paper as paper

SCHEMA = 'existing_native_intraday_paper_close_plan_v1'


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _saved_ref(reference):
    if paper._ref(reference['file']) != reference:
        raise ValueError('Original PAPER opening input changed')
    return reference['file']


def prepare_step(run_dir, quote, day, *, now=None):
    """Read one actual opening head; never create accounts or fetch a quote."""
    directory = Path(run_dir).resolve()
    head_file = directory / 'head.json'
    base = dict(run_dir=str(directory), day=day, engine_called=False,
        execution_admitted=False, account_created=False, network_accessed=False)
    if not head_file.is_file():
        interrupted = any((directory / 'journal').glob('*.json'))
        return {**base, 'status': 'interrupted_paper_without_head' if interrupted else 'not_created_no_close_required',
            'complete': not interrupted, 'step': None}
    head = _read(head_file)
    paper._validate_shape(head, directory)
    paper._verify_last_journal(head, directory)
    if head['execution_day'] != day:
        return {**base, 'status': 'paper_session_day_differs_no_roll_forward', 'complete': False,
            'execution_day': head['execution_day'], 'step': None}
    moment = paper._now(now)
    if moment.date().isoformat() != day or (moment.hour, moment.minute, moment.second) < (15, 0, 0):
        raise ValueError('PAPER completion requires its actual same-day post-close observation')
    quote = Path(quote).resolve()
    for source in (quote, Path(head['inputs']['plan_ref']['file'])):
        if source == directory or directory in source.parents:
            raise ValueError('Closing inputs must remain outside PAPER accounts')
    quote_ref = paper._ref(quote)
    if head['phase'] == 'closing':
        if head['phase_sources']['closing']['quote_file'] != quote_ref:
            raise ValueError('A completed PAPER close cannot be rebound to another quote')
        return {**base, 'status': 'already_closed_saved_paper_session', 'complete': True, 'step': None,
            'head_ref': paper._ref(head_file)}
    inputs = head['inputs']
    plan = _saved_ref(inputs['plan_ref'])
    prior = inputs['source_close_proof']['source_folder']
    command = ['native-intraday-paper', '--phase', 'closing', '--run-dir', str(directory),
        '--plan', plan, '--prior-package', prior, '--quote', str(quote)]
    qualification = inputs['qualification_inputs']
    for flag, key in (('--ipo-proof', 'ipo_evidence'), ('--master-run', 'research_master_run')):
        if qualification.get(key):
            command += [flag, _saved_ref(qualification[key])]
    key = sha256(str(directory).encode()).hexdigest()[:16]
    return {**base, 'status': 'existing_opening_ready_for_explicit_close', 'complete': False,
        'source_day': head['source_day'], 'execution_day': head['execution_day'],
        'opening_head_ref': paper._ref(head_file), 'quote_ref': quote_ref,
        'opening_inputs': deepcopy(inputs),
        'step': dict(id='native_paper_close_' + key, command=command, expected_files=[str(head_file)])}


def validate_plan(plan, day, source):
    if (plan.get('schema') != SCHEMA or plan.get('day') != day or plan.get('source') != source
            or plan.get('execution_admitted') is not False):
        raise ValueError('Saved PAPER close plan belongs to another source day')
    if len(plan['requests']) != len(plan['observations']):
        raise ValueError('Every explicit PAPER close request needs its own saved observation')
    for request, observation in zip(plan['requests'], plan['observations']):
        if observation['run_dir'] != request[0] or observation['day'] != day:
            raise ValueError('Saved PAPER close observation differs from the requested account/day')
        step = observation.get('step')
        if step is None:
            continue
        inputs = observation['opening_inputs']
        if observation['quote_ref']['file'] != request[1]:
            raise ValueError('Saved PAPER close quote differs from the explicit request')
        directory = Path(observation['run_dir']).resolve()
        expected = ['native-intraday-paper', '--phase', 'closing', '--run-dir', str(directory),
            '--plan', _saved_ref(inputs['plan_ref']), '--prior-package', inputs['source_close_proof']['source_folder'],
            '--quote', _saved_ref(observation['quote_ref'])]
        for flag, key in (('--ipo-proof', 'ipo_evidence'), ('--master-run', 'research_master_run')):
            reference = inputs['qualification_inputs'].get(key)
            if reference: expected += [flag, _saved_ref(reference)]
        if (inputs['execution_day'] != day or observation['execution_day'] != day
                or observation['source_day'] != inputs['source_day'] or inputs['source_day'] >= day
                or step['command'] != expected or step['expected_files'] != [str(directory / 'head.json')]
                or step['id'] != 'native_paper_close_' + sha256(str(directory).encode()).hexdigest()[:16]):
            raise ValueError('Saved PAPER close step changed the original opening inputs')
    if plan['steps'] != [row['step'] for row in plan['observations'] if row.get('step')]:
        raise ValueError('PAPER closing steps differ from original saved observations')
    return plan


def refresh(requests, day, source, operation, registration, *, execute, now=None):
    """Reuse the ordinary command receipts; failed children are never retried."""
    from .system_daily_refresh import _atomic, _ref
    operation = Path(operation)
    plan_file = operation / 'native_paper_closing_plan.json'
    if not requests and not plan_file.is_file():
        return None
    if plan_file.is_file():
        plan = validate_plan(_read(plan_file), day, source)
        if requests and [[str(Path(a).resolve()), str(Path(b).resolve())] for a, b in requests] != plan['requests']:
            raise ValueError('An existing PAPER close plan cannot change its explicit account/quote inputs')
    else:
        normalized = [[str(Path(a).resolve()), str(Path(b).resolve())] for a, b in requests]
        if len({row[0] for row in normalized}) != len(normalized):
            raise ValueError('Each PAPER account may have only one explicit closing observation')
        observations = []
        for directory, quote in normalized:
            try:
                observations.append(prepare_step(directory, quote, day, now=now))
            except (OSError, KeyError, TypeError, ValueError) as error:
                observations.append(dict(run_dir=directory, day=day, status='paper_closing_input_unavailable',
                    complete=False, step=None, error=f'{type(error).__name__}: {error}', engine_called=False))
        plan = dict(schema=SCHEMA, day=day, source=deepcopy(source), requests=normalized,
            observations=observations, steps=[row['step'] for row in observations if row.get('step')],
            created_at=paper._now(now).isoformat(), execution_admitted=False)
        validate_plan(plan, day, source)
        _atomic(plan_file, plan)
    results = []
    for observation in plan['observations']:
        if observation.get('step') is None:
            results.append({k: v for k, v in observation.items() if k != 'step'})
            continue
        step = {**observation['step'], 'binding': dict(kind='native_paper_closing', registration=registration,
            source=source, plan=_ref(plan_file))}
        result = execute(step, operation / 'commands', day)
        results.append(dict(run_dir=observation['run_dir'], status=result['status'],
            complete=result['status'] == 'completed', command_result=result))
    return dict(status='completed_existing_native_paper_closing' if all(row['complete'] for row in results)
        else 'partial_existing_native_paper_closing', daily_complete=all(row['complete'] for row in results),
        rows=results, plan_ref=_ref(plan_file), execution_admitted=False, network_accessed=False)
