"""Score an existing native model generation without a training entry point."""
from datetime import datetime
import argparse
import csv
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .baostock_daily_slice import digest
from .shortline_frozen_model import CN, FEATURES, FLAGS, load_generation, score_day

PUBLICATION_SCHEMA = 'native_shortline_raw_score_publication_v1'


def _clock(value):
    stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.utcoffset() is None:
        raise ValueError('Computed score clocks require an explicit timezone')
    return stamp.astimezone(CN)


def publish_saved_score_batch(batch_result, publication_out):
    """Seal existing raw score files at a new actual publication clock.

    All bound files have been closed before ``published_at`` is captured. The
    exclusive publication receipt is written last and is the publication
    record; its timestamp alone is not a replacement for that saved receipt.
    Neither historical computation clocks nor old payload bytes are rewritten.
    """
    result_file, publication_file = Path(batch_result).resolve(), Path(publication_out).resolve()
    if publication_file.exists():
        raise FileExistsError('Publication requires an explicitly new JSON receipt')
    if publication_file.suffix.lower() != '.json':
        raise ValueError('publication_out must name a new JSON file, not a directory')
    result = json.loads(result_file.read_text(encoding='utf-8'))
    if (result.get('schema') != 'native_shortline_frozen_score_batch_v1'
            or result.get('status') != 'completed_frozen_probabilities_only'
            or result.get('model_refit') is not False or result.get('target_generation_allowed') is not False
            or any(result.get(key) != value for key, value in FLAGS.items())):
        raise ValueError('Only a saved research-only frozen raw-probability batch may be published')
    root, files, clocks, summaries = result_file.parent, [result_file], [], []
    batch_day, batch_identities = None, None
    batches = result.get('score_batches', [])
    if len(batches) != 2 or {row.get('model_kind') for row in batches} != {'lr', 'lgbm'}:
        raise ValueError('Both frozen model outputs must remain part of the same raw-score publication')
    for row in batches:
        if (row.get('model_refit') is not False or row.get('target_generation_allowed') is not False
                or row.get('paper_candidate_flags_true') != 0):
            raise ValueError('Raw probability publication cannot admit candidate or target generation')
        score_file, proof_file = Path(row['score_file']).resolve(), Path(row['provenance_file']).resolve()
        for file in (score_file, proof_file):
            file.relative_to(root)
        for file, key in ((score_file, 'score_sha256'), (proof_file, 'provenance_sha256')):
            if row.get(key) is not None and digest(file) != row[key]:
                raise ValueError('A declared saved raw-score payload digest changed before publication')
        proof = json.loads(proof_file.read_text(encoding='utf-8'))
        if (proof.get('schema') != 'shortline_frozen_single_date_score_v1'
                or proof.get('status') not in {'completed_frozen_single_date_probabilities_only',
                    'partial_known_stage_frozen_single_date_probabilities_only'}
                or proof.get('model_kind') != row['model_kind'] or proof.get('day') != row['day']
                or proof.get('model_refit') is not False or proof.get('score_only_not_candidate_batch') is not True
                or proof.get('target_generation_allowed') is not False
                or any(proof.get(key) != value for key, value in FLAGS.items())
                or proof.get('feature_source', {}).get('source_scope') != result.get('source_scope')):
            raise ValueError('Saved score provenance must preserve its exact raw-score scope and research boundary')
        with score_file.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            if not {'symbol', 'date', 'stage', 'paper_candidate_only', 'model_hit5_probability'} <= set(reader.fieldnames or []):
                raise ValueError('Saved probability CSV lacks its raw research identities')
            rows = list(reader)
        if (not rows or len(rows) != row['rows'] or len(rows) != proof['score_rows']
                or any(item['date'] != row['day'] or item['paper_candidate_only'].lower() not in {'false', '0'} for item in rows)):
            raise ValueError('Only complete saved raw-probability rows with false candidate flags may be published')
        identities = {(item['symbol'], item['date'], item['stage']) for item in rows}
        if len(identities) != len(rows) or any(item['stage'] not in {'1', '2', '3'} for item in rows):
            raise ValueError('A raw-score publication requires unique known-stage event identities')
        if batch_day is None:
            batch_day, batch_identities = row['day'], identities
        elif row['day'] != batch_day or identities != batch_identities:
            raise ValueError('Both frozen model outputs must describe the same day and event identities')
        files.extend((score_file, proof_file))
        clocks.extend(_clock(proof[key]) for key in ('scored_at', 'score_available_at', 'model_available_at'))
        summaries.append({'model_kind': row['model_kind'], 'day': row['day'], 'rows': len(rows),
            'score_available_at': proof['score_available_at'], 'score_clock_basis': 'in_memory_probability_computation_not_publication',
            'stage_membership_complete': proof.get('stage_membership_complete'),
            'unknown_stage_symbols': proof.get('unknown_stage_symbols', []),
            'score_status': proof['status'], 'target_generation_allowed': False})
    if len(set(files)) != len(files) or publication_file in files:
        raise ValueError('Publication inputs and the new receipt must have distinct identities')
    # No raw panel/model-data rescan: bind only the small generated CSV/JSON
    # payloads. No receipt self-hash or rewrite of an already hashed result.
    bindings = [{'path': file.relative_to(root).as_posix(), 'sha256': digest(file), 'byte_count': file.stat().st_size}
        for file in files]
    publication_file.parent.mkdir(parents=True, exist_ok=True)
    published = datetime.now(CN)
    if any(stamp > published for stamp in [*clocks, _clock(result['completed_at'])]):
        raise ValueError('A publication cannot precede its actual model, score or completed payload clock')
    receipt = {'schema': PUBLICATION_SCHEMA, 'status': 'published_frozen_probabilities_only',
        'published_at': published.isoformat(), 'publication_file': str(publication_file),
        'publication_clock_basis': 'actual_local_now_after_closed_payloads_before_exclusive_receipt_write',
        'publication_requires_saved_receipt': True, 'receipt_written_after_all_bound_payloads': True,
        'score_batch_directory': str(root), 'batch_result_file': str(result_file),
        'files': bindings, 'score_batches': summaries, 'source_scope': result['source_scope'],
        'payload_hash_basis': 'existing_declared_payload_digests_checked_when_present_then_current_saved_bytes_bound',
        'previous_payload_hashes_present': all(row.get('score_sha256') and row.get('provenance_sha256') for row in batches),
        'old_payloads_modified': False, 'payload_computation_time_is_publication': False,
        'model_refit': False, 'target_generation_allowed': False,
        'all_false_candidate_flags_are_not_an_empty_portfolio_target': True, **FLAGS}
    with publication_file.open('x', encoding='utf-8') as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2, allow_nan=False)
    return receipt


def load_score_input(panel_result):
    path = Path(panel_result).resolve()
    result = json.loads(path.read_text(encoding='utf-8'))
    if (result.get('feature_contract_id') != 'native_active_known_nonST_observed_nine_features_v1'
            or result.get('execution_admitted') is not False
            or result.get('strategy_validated') is not False):
        raise ValueError('A saved independent native feature contract is required')
    file = Path(result['latest_features_path']).resolve()
    if digest(file) != result['latest_features_sha256']:
        raise ValueError('Native D-feature bytes changed after production')
    required = ['event_id', 'date', 'symbol', 'stage', 'stage_reference_known', 'decision_eligible', *FEATURES]
    # Labels are never read into the inference process, even when present in a
    # broader source file. Membership depends only on frozen own-D facts.
    latest = pd.read_csv(file, usecols=required, dtype={'symbol': str, 'date': str})
    if latest.empty or not latest.date.eq(result['last_source_day']).all():
        raise ValueError('One exact nonempty last-day feature cross-section is required')
    for column in ('decision_eligible', 'stage_reference_known'):
        if not latest[column].map(lambda value: isinstance(value, (bool, np.bool_))).all():
            raise ValueError('D eligibility and stage knowledge must remain explicit booleans')
    known = latest.loc[latest.decision_eligible & latest.stage_reference_known
        & latest.stage.isin((1, 2, 3))].copy()
    if known.empty:
        raise ValueError('No known eligible events; do not manufacture a score or an empty liquidation target')
    known['stage'] = known.stage.astype(int)
    scope = result['latest_scope']
    proof = {'day': result['last_source_day'], 'available_at': result['available_at'],
        'feature_contract_id': result['feature_contract_id'], 'feature_semantics': result['feature_semantics'],
        'feature_names': list(FEATURES), 'source_sha256': result['latest_features_sha256'],
        'source_file': str(file), 'source_scope': scope,
        'stage_membership_complete': scope['stage_membership_complete'],
        'unknown_stage_symbols': scope['unknown_stage_symbols'],
        'known_stage_partial_research_only': not scope['stage_membership_complete'],
        'scored_scope': 'all_current_decision_eligible_known_stage_1_2_3_native_events_only',
        'full_market_stage_membership_complete': False}
    return result, known, proof


def score_saved_generation(model, panel_result, out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError('A frozen score batch requires an explicitly new output directory')
    started = datetime.now(CN).isoformat()
    source, frame, proof = load_score_input(panel_result)
    generation = load_generation(model)
    batches = [(kind, score_day(generation, frame, day=source['last_source_day'],
        model_kind=kind, feature_provenance=proof)) for kind in ('lr', 'lgbm')]
    out.mkdir(parents=True, exist_ok=False)
    summaries = []
    for kind, batch in batches:
        score_file = out / (source['last_source_day'].replace('-', '') + '_' + kind + '_raw_scores.csv')
        batch['scores'].to_csv(score_file, index=False, encoding='utf-8-sig')
        batch['provenance']['score_available_at_basis'] = 'in_memory_probability_computation_not_persisted_publication'
        batch['provenance']['publication_file'] = str(out / 'publication.json')
        provenance_file = score_file.with_suffix('.json')
        with provenance_file.open('x', encoding='utf-8') as handle:
            json.dump(batch['provenance'], handle, ensure_ascii=False, indent=2, allow_nan=False)
        summaries.append({'model_kind': kind, 'day': source['last_source_day'],
            'rows': len(batch['scores']), 'score_file': str(score_file),
                'provenance_file': str(provenance_file), 'score_sha256': digest(score_file),
                'provenance_sha256': digest(provenance_file), 'stage_counts': batch['provenance']['stage_counts'],
            'model_refit': False, 'paper_candidate_flags_true': int(batch['scores'].paper_candidate_only.sum()),
            'target_generation_allowed': False})
    result = {'schema': 'native_shortline_frozen_score_batch_v1',
        'status': 'completed_frozen_probabilities_only', 'started_at': started,
        'completed_at': datetime.now(CN).isoformat(), 'model_manifest': str(generation.path),
        'panel_result': str(Path(panel_result).resolve()), 'source_scope': source['latest_scope'],
        'score_batches': summaries, 'model_refit': False, 'target_generation_allowed': False,
        'publication_file': str(out / 'publication.json'), 'publication_clock_requires_saved_receipt': True,
        'all_false_candidate_flags_are_not_an_empty_portfolio_target': True, **FLAGS}
    with (out / 'result.json').open('x', encoding='utf-8') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    publication = publish_saved_score_batch(out / 'result.json', out / 'publication.json')
    result['published_at'] = publication['published_at']
    return result


def publication_main(argv=None):
    parser = argparse.ArgumentParser(description='Publish an existing frozen raw-score batch without refit or rescoring')
    parser.add_argument('--batch', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    receipt = publish_saved_score_batch(args.batch, args.out)
    print(json.dumps({'schema': receipt['schema'], 'published_at': receipt['published_at'],
        'receipt': receipt['publication_file'], 'model_refit': False, 'execution_admitted': False}, ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--panel', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    result = score_saved_generation(args.model, args.panel, args.out)
    print(json.dumps({'status': result['status'], 'score_batches': result['score_batches'],
        'publication_file': result['publication_file'], 'published_at': result['published_at'],
        'model_refit': False, 'execution_admitted': False}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
