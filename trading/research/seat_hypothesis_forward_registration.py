"""Register the original four seat hypotheses' isolated persistent method.

This command only reads saved sources and writes a new explicit registration.
It does not create a research account, publish a dated candidate, or run Engine.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import seat_hypothesis_dated_inputs as inputs


def register(original_registration, dated_input_method, calendar, ipo_proof,
             research_master_run, first_decision_day, out):
    from . import seat_hypothesis_dated_forward as forward

    actual = forward._now()
    original, _, original_files = inputs._registered(original_registration, actual)
    references = {name: inputs._ref(file) for name, file in {
        'original_registration': original_registration,
        'dated_input_method_registration': dated_input_method,
        'calendar': calendar, 'ipo_evidence': ipo_proof,
        'research_master_run': research_master_run,
        'implementation': forward.__file__,
    }.items()}
    protected = [r['file'] for r in references.values()] + [r['file'] for r in original_files]
    qualification_files, _ = forward._qualification_files(ipo_proof, research_master_run)
    protected.extend(str(file) for file in qualification_files)
    target = inputs._protect(out, protected, [Path(p).resolve().parent for p in protected])
    record = {
        'schema': forward.REGISTRATION_SCHEMA, 'method_id': forward.METHOD_ID,
        'registered_at': actual.isoformat(), **references,
        'first_decision_day': inputs._day(first_decision_day),
        'arm_ids': list(inputs.ARMS), 'config': original['config'],
        'rule_spec': forward.RULE_SPEC, **forward.FLAGS,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    inputs._write(target, record)
    # This is a pure source readback, including the core implementation and
    # original source->dated input->forward registration chain. A failure keeps
    # the attempted registration for diagnosis and cannot create an account.
    checked = forward._registration(target, forward._now())
    return {'schema': 'public_seat_forward_method_registration_receipt_v1',
        'status': 'registered_research_method_only', 'registration': checked['reference'],
        'registered_at': record['registered_at'], 'method_id': forward.METHOD_ID,
        'first_decision_day': record['first_decision_day'], 'arm_ids': record['arm_ids'],
        'source_candidate_counts': {key: len(value['supported']) for key, value in checked['recipes'].items()},
        'account_created': False, 'engine_run': False, 'orders_generated': False, **forward.FLAGS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original-registration', required=True, type=Path)
    parser.add_argument('--dated-input-method', required=True, type=Path)
    parser.add_argument('--calendar', required=True, type=Path)
    parser.add_argument('--ipo-proof', required=True, type=Path)
    parser.add_argument('--research-master-run', required=True, type=Path)
    parser.add_argument('--first-decision-day', required=True)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    result = register(args.original_registration, args.dated_input_method, args.calendar,
        args.ipo_proof, args.research_master_run, args.first_decision_day, args.out)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
