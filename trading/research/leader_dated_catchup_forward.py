"""Signal-module entry to the existing shared leader paper-account runner."""
import argparse
import json
from pathlib import Path

from . import leader_dated_catchup_paper as signal
from . import leader_eight_type_forward_run as shared


def run(publications,panel,ipo_evidence,research_master_run,run_dir,*,registration,through=None):
    registered,proof=signal._registered(registration,shared._now())
    if not publications and Path(run_dir).exists():
        raise ValueError('Existing research books require their complete original publication prefix')
    result=shared.run(publications,panel,ipo_evidence,research_master_run,run_dir,
        registration=registration,through=through,method=signal)
    result.update(signal_module=signal.METHOD_ID,shared_account_runner='leader_eight_type_forward_run',
        first_decision_day=registered['first_decision_day'],first_execution_day=registered['first_execution_day'],
        planned_case_count=len(signal.CASE_IDS),paper_registration=proof,
        separate_trading_framework_created=False)
    return result


def _result_export_path(path, *, publications, panel, ipo_evidence, research_master_run, registration, run_dir):
    """Restrict exports, including saved inputs referenced by publications."""
    if path is None:
        return None
    target = Path(path).resolve()
    if target.suffix.lower() != '.json' or target.exists():
        raise ValueError('A forward result requires a new external JSON file')
    queue = list(map(Path, [*publications, panel, ipo_evidence, research_master_run, registration]))
    roots, visited = [Path(run_dir).resolve()], set()
    while queue:
        source = queue.pop().resolve()
        if source in visited:
            continue
        visited.add(source)
        roots.append(source if source.is_dir() else source.parent)
        if source.suffix.lower() != '.json' or not source.is_file():
            continue
        value = json.loads(source.read_text(encoding='utf-8-sig'))
        def declared(item):
            if isinstance(item, dict):
                for child in item.values():
                    yield from declared(child)
            elif isinstance(item, list):
                for child in item:
                    yield from declared(child)
            elif isinstance(item, str) and '://' not in item and Path(item).is_absolute():
                yield Path(item)
        queue.extend(declared(value))
    if any(target == root or root in target.parents for root in roots):
        raise ValueError('A forward result must stay outside accounts and all declared source packages')
    return target


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--publication',action='append',type=Path,default=[])
    for key in ('panel','ipo-evidence','research-master-run','run-dir','registration'):
        p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--through')
    p.add_argument('--result-json',type=Path)
    a=p.parse_args(argv)
    export = _result_export_path(a.result_json, publications=a.publication, panel=a.panel,
        ipo_evidence=a.ipo_evidence, research_master_run=a.research_master_run,
        registration=a.registration, run_dir=a.run_dir)
    result=run(a.publication,a.panel,a.ipo_evidence,a.research_master_run,a.run_dir,
        registration=a.registration,through=a.through)
    if export is not None:
        export.parent.mkdir(parents=True,exist_ok=True)
        with export.open('x',encoding='utf-8') as handle:
            json.dump(result,handle,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    return 0
