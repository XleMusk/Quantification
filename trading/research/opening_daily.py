"""Advance saved opening products only when one registered source head changes.

Status and unchanged inputs read small saved records only. Producers remain in
the lazy artifact factory; interrupted attempts never imply a live process or
permission to repeat work.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import importlib
import json
from pathlib import Path
import re
import uuid

from ..settings import DATA_ROOT, ROOT


SCHEMA = "saved_opening_daily_coordinator_v1"
METHOD = "source_head_change_only_opening_products_v1"
FAMILIES = {"fixed", "eight_type", "leader20", "alpha158", "native_preopen", "native_lowvol", "dated_catchup", "eight_type_residual", "known_identity", "registered_research"}
ADDENDUM_SCHEMA = "saved_opening_daily_route_addendum_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
         "selection_enabled": False, "strategy_validated": False, "prefill_allowed": False,
         "broker_interacted": False, "ledger_mutated": False, "engine_advanced": False}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)),
             *("LPT" + str(i) for i in range(1, 10))}


def _now(current=None):
    value = current or datetime.now(timezone(timedelta(hours=8)))
    if value.tzinfo is None:
        raise ValueError("Opening coordinator clocks require an explicit timezone")
    return value.isoformat()


def _read(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Saved coordinator record must be an object")
    return value


def _ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def _atomic(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _destination(path):
    path = Path(path).resolve()
    if path == ROOT or ROOT in path.parents or path == DATA_ROOT or DATA_ROOT not in path.parents:
        raise ValueError("Use a dedicated coordinator directory under QUANT_TRADING_HOME outside the repository")
    return path


def _name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value) or value.upper() in _RESERVED:
        raise ValueError("Route ID must be a safe explicit directory name")
    return value


def _separate(state, source):
    if state == source or state in source.parents or source in state.parents:
        raise ValueError("Coordinator state and source/adopted artifacts must not overlap")


def _factory():
    return importlib.import_module("trading.research.opening_daily_artifacts")


def _lock(path):
    # The existing writer lock imports trading machinery, so status and the
    # unchanged-source fast path never import it.
    from ..worker import lock
    return lock(path)


def _source(route):
    path = Path(route["source"]) / "head.json"
    if route.get("family") in {"native_preopen", "native_lowvol", "registered_research"}:
        if route["family"] == "native_preopen":
            from .native_saved_opening import source_dependencies_current
        elif route["family"] == "registered_research":
            from .registered_research_saved_opening import source_dependencies_current
        else:
            from .native_lowvol_saved_opening import source_dependencies_current
        if not source_dependencies_current(path):
            raise ValueError("Native saved opening source dependencies changed; explicitly refresh its saved source first")
    return _ref(path)


def _input_id(registration_ref, route_id, source_ref):
    value = [registration_ref["sha256"], route_id, source_ref]
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _attempt_path(directory, route_id, input_id):
    if not re.fullmatch(r"[a-f0-9]{64}", input_id):
        raise ValueError("Select an exact saved input_id")
    return directory / "attempts" / _name(route_id) / (input_id + ".json")


def _load(directory):
    head = _read(directory / "head.json")
    reference = head["registration"]
    if _ref(directory / "registration.json") != reference:
        raise ValueError("Registered opening routes or adoption references changed")
    registration = _read(reference["path"])
    if (head.get("schema") != SCHEMA or registration.get("schema") != SCHEMA
            or head.get("method_id") != METHOD or registration.get("method_id") != METHOD
            or registration.get("run_directory") != str(directory)
            or any(value.get(key) is not expected for value in (head, registration) for key, expected in FLAGS.items())):
        raise ValueError("Saved opening coordinator identity/boundaries changed")
    effective = deepcopy(registration)
    references = head.get("registration_addenda", [])
    anchors = head.get("route_registrations", {})
    if not isinstance(references, list) or not isinstance(anchors, dict):
        raise ValueError("Saved opening registration addenda must remain an ordered sealed list")
    routes = effective["routes"]
    route_ids = [route["id"] for route in routes]
    if len(set(route_ids)) != len(route_ids):
        raise ValueError("Saved opening routes contain duplicate identities")
    expected_anchors, previous = {}, []
    for reference in references:
        path = Path(reference["path"]).resolve()
        if (path.parent != directory / "registration_addenda" or _ref(path) != reference):
            raise ValueError("Saved opening registration addendum reference changed")
        addendum = _read(path)
        if (addendum.get("schema") != ADDENDUM_SCHEMA or addendum.get("method_id") != METHOD
                or addendum.get("run_directory") != str(directory)
                or addendum.get("base_registration") != head["registration"]
                or addendum.get("previous_addenda") != previous
                or any(addendum.get(key) is not expected for key, expected in FLAGS.items())):
            raise ValueError("Saved opening registration addendum chain or boundary changed")
        additions = addendum.get("routes")
        adopted = addendum.get("adopt_refs")
        source_heads = addendum.get("source_heads")
        if (not isinstance(additions, list) or not additions or not isinstance(adopted, dict)
                or not isinstance(source_heads, dict)):
            raise ValueError("Saved opening registration addendum routes or references are missing")
        new_ids = []
        for route in additions:
            route_id = _name(route["id"])
            source = Path(route["source"]).resolve()
            if route_id in route_ids or route_id in new_ids or route.get("family") not in FAMILIES:
                raise ValueError("Opening addenda cannot replace or repeat a registered route")
            _separate(directory, source)
            if source_heads.get(route_id, {}).get("path") != str(source / "head.json"):
                raise ValueError("Opening addendum source reference belongs to another route")
            if not re.fullmatch(r"[a-f0-9]{64}", source_heads[route_id].get("sha256", "")):
                raise ValueError("Opening addendum source reference requires its actual SHA256")
            artifacts = route.get("adopt")
            if (not isinstance(artifacts, dict) or not artifacts or route_id not in adopted
                    or set(adopted[route_id]) != set(artifacts)):
                raise ValueError("Opening addendum must retain its explicit adoption references")
            for key, value in artifacts.items():
                artifact = Path(value).resolve()
                _separate(directory, artifact)
                proof = adopted[route_id][key]
                if (proof.get("path") != str(artifact)
                        or not re.fullmatch(r"[a-f0-9]{64}", proof.get("sha256", ""))):
                    raise ValueError("Opening addendum adoption reference changed")
            new_ids.append(route_id)
            expected_anchors[route_id] = reference
        if set(adopted) != set(new_ids) or set(source_heads) != set(new_ids):
            raise ValueError("Opening addendum contains unrelated adoption or source references")
        routes.extend(deepcopy(additions))
        route_ids.extend(new_ids)
        effective.setdefault("adopt_refs", {}).update(deepcopy(adopted))
        previous.append(reference)
    if anchors != expected_anchors or list(head["routes"]) != route_ids:
        raise ValueError("Saved opening route order or registration anchors changed")
    return head, effective


def _registration_for(head, route_id):
    """Keep legacy attempt identities unchanged when later routes are added."""
    return head.get("route_registrations", {}).get(route_id, head["registration"])


def _attempt(directory, head, route, source_ref):
    registration = _registration_for(head, route["id"])
    identity = _input_id(registration, route["id"], source_ref)
    path = _attempt_path(directory, route["id"], identity)
    if not path.is_file():
        return identity, path, None
    value = _read(path)
    if (value.get("schema") != SCHEMA or value.get("method_id") != METHOD
            or value.get("registration") != registration or value.get("route_id") != route["id"]
            or value.get("input_id") != identity or value.get("source_head") != source_ref):
        raise ValueError("Saved opening attempt identity changed")
    return identity, path, value


def _product_boundary(source_ref, product):
    required = {key: FLAGS[key] for key in ("research_only", "candidate_panel_only", "execution_admitted",
                                           "selection_enabled", "strategy_validated", "prefill_allowed")}
    if (not isinstance(product, dict) or product.get("source_head") != source_ref
            or not isinstance(product.get("refs"), dict) or not isinstance(product.get("status"), str)
            or any(product.get(key) is not expected for key, expected in required.items())):
        raise ValueError("Opening product source reference or research-only boundary changed")


def _commit(directory, head, route, attempt, path, current=None):
    if _source(route) != attempt["source_head"]:
        raise ValueError("Source head changed before opening product commit")
    fresh_head, _ = _load(directory)
    if (_registration_for(fresh_head, route["id"]) != attempt["registration"]
            or fresh_head["routes"][route["id"]] != attempt["route_before"]):
        raise ValueError("This route advanced after the saved opening attempt")
    product = attempt["product"]
    _product_boundary(attempt["source_head"], product)
    fresh_head["routes"][route["id"]] = {"input_id": attempt["input_id"],
        "source_head": attempt["source_head"], "product": product,
        "attempt": str(path), "committed_at": _now(current), "adopted": attempt["operation"] == "adopt"}
    _atomic(directory / "head.json", fresh_head)
    attempt.update(status="committed", head_committed=True, execution_progress_unknown=False,
                   committed_at=_now(current))
    _atomic(path, attempt)


def _execute(directory, head, route, source_ref, *, operation, artifacts=None, current=None):
    identity, path, previous = _attempt(directory, head, route, source_ref)
    if previous is not None:
        return False, False
    path.parent.mkdir(parents=True, exist_ok=True)
    out = directory / "artifacts" / route["id"] / identity
    attempt = {"schema": SCHEMA, "method_id": METHOD, "registration": _registration_for(head, route["id"]),
        "route_id": route["id"], "input_id": identity, "source_head": source_ref,
        "operation": operation, "route_before": deepcopy(head["routes"][route["id"]]),
        "output_directory": str(out), "status": "started", "started_at": _now(current),
        "product": None, "product_validated": False, "head_committed": False,
        "execution_progress_unknown": True, **FLAGS}
    _atomic(path, attempt)
    factory_called = False
    try:
        if out.exists():
            raise FileExistsError("Unattributed output directory already exists; no overwrite or retry")
        factory = _factory()
        factory_called = True
        product = (factory.adopt(route, source_ref, artifacts) if operation == "adopt"
                   else factory.produce(route, source_ref, out))
        _product_boundary(source_ref, product)
        factory.validate_product(route, source_ref, product)
        attempt.update(product=product, product_validated=True, status="successful_product_saved",
                       product_saved_at=_now(current), execution_progress_unknown=False)
        _atomic(path, attempt)
        _commit(directory, head, route, attempt, path, current)
        return True, factory_called
    except Exception as error:
        committed = _read(directory / "head.json")["routes"][route["id"]]
        committed = committed is not None and committed["input_id"] == identity
        attempt.update(status="failed_after_commit" if committed else "failed_with_saved_product"
                       if attempt["product_validated"] else "failed_uncommitted",
                       failed_at=_now(current), error=f"{type(error).__name__}: {error}", head_committed=committed)
        _atomic(path, attempt)
        return committed, factory_called


def _normalize_routes(directory, routes, *, existing=()):
    if not isinstance(routes, list) or not routes:
        raise ValueError("Register an explicit nonempty routes list")
    normalized, adopted = [], {}
    for raw in routes:
        route = deepcopy(raw)
        route_id = _name(route["id"])
        if route_id in existing or route_id in adopted or route.get("family") not in FAMILIES:
            raise ValueError("Unique route IDs and supported existing families are required")
        source = Path(route["source"]).resolve()
        _separate(directory, source)
        route["source"] = str(source)
        if route["family"] == "fixed" and not isinstance(route.get("signal"), str):
            raise ValueError("The fixed route must retain its explicit signal")
        if not isinstance(route.get("adopt"), dict) or not route["adopt"]:
            raise ValueError("Registration adopts explicit existing artifacts; it does not produce them")
        artifacts = {}
        for key, path in route["adopt"].items():
            artifact = Path(path).resolve()
            _separate(directory, artifact)
            route["adopt"][key] = str(artifact)
            artifacts[key] = _ref(artifact)
        adopted[route_id] = artifacts
        normalized.append(route)
    return normalized, adopted


def register(run_dir, routes, *, current=None):
    directory = _destination(run_dir)
    if directory.exists():
        raise FileExistsError("Use a new explicit opening coordinator directory")
    normalized, adopted = _normalize_routes(directory, routes)
    directory.mkdir(parents=True, exist_ok=False)
    with _lock(directory / "coordinator.lock"):
        registration = {"schema": SCHEMA, "method_id": METHOD, "run_directory": str(directory),
            "registered_at": _now(current), "routes": normalized, "adopt_refs": adopted, **FLAGS}
        _atomic(directory / "registration.json", registration)
        head = {"schema": SCHEMA, "method_id": METHOD, "registration": _ref(directory / "registration.json"),
                "routes": {route["id"]: None for route in normalized}, **FLAGS}
        _atomic(directory / "head.json", head)
        for route in normalized:
            try:
                source_ref = _source(route)
            except (OSError, ValueError):
                continue
            _execute(directory, head, route, source_ref, operation="adopt", artifacts=adopted[route["id"]], current=current)
            head, _ = _load(directory)
    return status(directory)


def extend(run_dir, routes, *, current=None):
    """Append explicitly adopted routes without rewriting the base registration.

    Each addition owns an immutable registration reference. Existing route
    records and their attempt identifiers retain their original anchors.
    """
    directory = _destination(run_dir)
    if not (directory / "head.json").is_file():
        raise ValueError("Extend an existing saved opening coordinator; do not register it again")
    committed = []
    with _lock(directory / "coordinator.lock"):
        head, _ = _load(directory)
        normalized, adopted = _normalize_routes(directory, routes, existing=head["routes"])
        sources = {route["id"]: _source(route) for route in normalized}
        previous = deepcopy(head.get("registration_addenda", []))
        addendum = {"schema": ADDENDUM_SCHEMA, "method_id": METHOD, "run_directory": str(directory),
            "registered_at": _now(current), "base_registration": deepcopy(head["registration"]),
            "previous_addenda": previous, "routes": normalized, "adopt_refs": adopted,
            "source_heads": sources, **FLAGS}
        if _read(directory / "head.json") != head:
            raise ValueError("Opening coordinator changed before route registration")
        folder = directory / "registration_addenda"
        folder.mkdir(exist_ok=True)
        path = folder / (f"{len(previous) + 1:04d}_" + uuid.uuid4().hex + ".json")
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(addendum, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        reference = _ref(path)
        if (_read(directory / "head.json") != head
                or any(_source(route) != sources[route["id"]] for route in normalized)):
            raise ValueError("Opening coordinator or addition source changed before append; saved addendum is uncommitted")
        updated = deepcopy(head)
        updated["registration_addenda"] = [*previous, reference]
        updated["route_registrations"] = {**updated.get("route_registrations", {}),
                                         **{route["id"]: reference for route in normalized}}
        for route in normalized:
            updated["routes"][route["id"]] = None
        _atomic(directory / "head.json", updated)
        head, _ = _load(directory)
        for route in normalized:
            did_commit, _ = _execute(directory, head, route, sources[route["id"]], operation="adopt",
                                     artifacts=adopted[route["id"]], current=current)
            if did_commit:
                committed.append(route["id"])
            head, _ = _load(directory)
    return {**status(directory), "newly_registered_routes": [route["id"] for route in normalized],
            "newly_committed_routes": committed, "old_routes_reproduced": False}


def status(run_dir):
    directory = _destination(run_dir)
    head, registration = _load(directory)
    rows = []
    for route in registration["routes"]:
        committed = head["routes"][route["id"]]
        source_ref, error = None, None
        try:
            source_ref = _source(route)
        except (OSError, ValueError) as problem:
            error = f"{type(problem).__name__}: {problem}"
        current = committed is not None and source_ref == committed["source_head"]
        attempt = None
        if not current and source_ref is not None:
            try:
                identity, path, attempt = _attempt(directory, head, route, source_ref)
            except (OSError, ValueError) as problem:
                identity = _input_id(_registration_for(head, route["id"]), route["id"], source_ref)
                path = _attempt_path(directory, route["id"], identity)
                attempt = {"status": "invalid_saved_attempt", "error": f"{type(problem).__name__}: {problem}",
                           "execution_progress_unknown": True}
        else:
            identity, path = (committed["input_id"], Path(committed["attempt"])) if current else (None, None)
        state = ("current_saved_product" if current else "source_unavailable" if source_ref is None
                 else "recoverable_saved_product" if attempt and attempt.get("product_validated")
                 else "failed_same_input_no_retry" if attempt and attempt.get("error")
                 else "unresolved_same_input_no_retry" if attempt else "source_changed_product_required")
        rows.append({"id": route["id"], "family": route["family"], "status": state,
            "source_head": source_ref, "input_id": identity, "attempt": str(path) if path else None,
            "source_changed": not current, "stale": not current and committed is not None,
            "saved_product": deepcopy(committed["product"]) if committed else None,
            "last_committed_input_id": committed["input_id"] if committed else None,
            "attempt_status": attempt.get("status") if attempt else None,
            "error": error or (attempt.get("error") if attempt else None),
            "execution_progress_unknown": bool(attempt and attempt.get("execution_progress_unknown"))})
    return {"schema": SCHEMA, "method_id": METHOD, "run_directory": str(directory),
        "registration": head["registration"], "head": str(directory / "head.json"),
        "status": "current_saved_opening_products" if all(not row["source_changed"] for row in rows)
                  else "opening_products_need_attention", "routes": rows, "factory_called": False,
        "source_head_check_only": True, "saved_artifacts_revalidated": False, **FLAGS}


def run(run_dir, *, current=None):
    directory = _destination(run_dir)
    initial = status(directory)
    if all(not row["source_changed"] for row in initial["routes"]):
        return {**initial, "status": "no_changed_source", "newly_committed_routes": []}
    committed, called = [], []
    with _lock(directory / "coordinator.lock"):
        head, registration = _load(directory)
        for route in registration["routes"]:
            try:
                source_ref = _source(route)
            except (OSError, ValueError):
                continue
            prior = head["routes"][route["id"]]
            if prior is not None and prior["source_head"] == source_ref:
                continue
            try:
                did_commit, did_call = _execute(directory, head, route, source_ref, operation="produce", current=current)
            except (OSError, ValueError):
                # A malformed attempt is shown by status for this route; it
                # cannot authorize replay or prevent another route advancing.
                continue
            if did_call:
                called.append(route["id"])
            if did_commit:
                committed.append(route["id"])
            head, _ = _load(directory)
    return {**status(directory), "newly_committed_routes": committed,
            "factory_called": bool(called), "factory_called_routes": called}


def recover(run_dir, route_id, input_id, *, current=None):
    directory, route_id = _destination(run_dir), _name(route_id)
    with _lock(directory / "coordinator.lock"):
        head, registration = _load(directory)
        route = next((route for route in registration["routes"] if route["id"] == route_id), None)
        if route is None:
            raise ValueError("Unknown registered opening route")
        path = _attempt_path(directory, route_id, input_id)
        attempt = _read(path)
        identity, expected_path, _ = _attempt(directory, head, route, _source(route))
        if identity != input_id or expected_path != path:
            raise ValueError("Recovery requires the exact currently saved source head")
        committed = head["routes"][route_id]
        if committed and committed["input_id"] == input_id:
            return {**status(directory), "recovery_status": "already_committed", "producer_repeated": False}
        if not attempt.get("product_validated") or not isinstance(attempt.get("product"), dict):
            return {**status(directory), "recovery_status": "unknown_no_successful_saved_product",
                    "producer_repeated": False, "execution_progress_unknown": True}
        _product_boundary(attempt["source_head"], attempt["product"])
        _factory().validate_product(route, attempt["source_head"], attempt["product"])
        _commit(directory, head, route, attempt, path, current)
    return {**status(directory), "recovery_status": "recovered_saved_product", "producer_repeated": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("register", "extend", "run", "status", "recover"):
        part = sub.add_parser(command)
        if command == "extend":
            part.add_argument("--directory", "--run-dir", dest="run_dir", type=Path, required=True)
            part.add_argument("--routes", type=Path, required=True, help="JSON file containing the explicit new routes list")
        else:
            part.add_argument("--run-dir", type=Path, required=True)
        if command == "register":
            part.add_argument("--config", type=Path, required=True)
        elif command == "recover":
            part.add_argument("--route", required=True)
            part.add_argument("--input-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "register":
        result = register(args.run_dir, _read(args.config)["routes"])
    elif args.command == "extend":
        result = extend(args.run_dir, _read(args.routes)["routes"])
    elif args.command == "recover":
        result = recover(args.run_dir, args.route, args.input_id)
    else:
        result = (run if args.command == "run" else status)(args.run_dir)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0
