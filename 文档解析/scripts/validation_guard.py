"""Freeze external development annotations and count repair/tuning attempts fairly.

No cases, scientific answers, gold annotations, or results are bundled here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
from parse_pdf import file_hash, write_json


OPERATIONS = {
    "first_parse": 0,
    "read_existing": 0,
    "reuse_cache": 0,
    "report_format": 0,
    "second_grid": 1,
    "new_parse_or_ocr": 1,
    "change_scored_content": 1,
    "parameter_candidate": 1,
}
ERROR_KINDS = {"known_missed", "new_mechanism_same_consequence", "rule_undecidable"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False).encode()).hexdigest()


def load(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"Duplicate JSON key: {key}")
            value[key] = item
        return value
    def nonfinite(value):
        raise ValueError(f"Non-finite JSON number: {value}")
    value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique,
                       parse_constant=nonfinite)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def bundle_hash(root):
    paths = [root / "SKILL.md", root / "requirements.txt", *sorted((root / "scripts").glob("*.py"))]
    return digest({p.relative_to(root).as_posix(): file_hash(p) for p in paths})


def check_plan(plan):
    if plan.get("split") not in {"development", "holdout"}:
        raise ValueError("Declare development or holdout")
    if type(plan.get("max_rounds")) is not int or not 0 <= plan["max_rounds"] <= 64:
        raise ValueError("Explicitly freeze max_rounds in 0..64 before attempting repairs")
    if not isinstance(plan.get("parameter_grid"), list) or len(plan["parameter_grid"]) > 64:
        raise ValueError("Freeze a finite parameter_grid (empty means no search)")
    if not all(isinstance(p, dict) for p in plan["parameter_grid"]):
        raise ValueError("Parameter candidates must be objects")
    if len({digest(p) for p in plan["parameter_grid"]}) != len(plan["parameter_grid"]):
        raise ValueError("Parameter candidates must be unique")
    if not isinstance(plan.get("cases"), list) or not plan["cases"] or not all(isinstance(c, str) and c for c in plan["cases"]):
        raise ValueError("Freeze the development/holdout case IDs")
    tables = plan.get("logical_tables")
    if not isinstance(tables, list):
        raise ValueError("Freeze logical_tables from original-page annotations, independently of parser output")
    ids = set()
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("id"), str) or table["id"] in ids:
            raise ValueError("Logical table IDs must be unique strings")
        ids.add(table["id"])
        if table.get("case_id") not in plan["cases"]:
            raise ValueError("Each logical table needs a frozen case_id")
        fragments = table.get("fragments")
        if (not isinstance(fragments, list) or not fragments
                or not all(isinstance(f, str) and f for f in fragments) or len(set(fragments)) != len(fragments)):
            raise ValueError("Each table needs all independently annotated fragment IDs")
        if not re.fullmatch(r"[a-f0-9]{64}", table.get("source_sha256", "")):
            raise ValueError("Each table needs the actual source SHA-256")
    for key in ("baseline_complete_ids", "conditional_ids"):
        value = plan.get(key)
        if not isinstance(value, list) or not all(isinstance(i, str) for i in value) or not set(value).issubset(ids) or len(set(value)) != len(value):
            raise ValueError(f"Freeze {key} as a unique subset of independently annotated tables")
    if set(plan["conditional_ids"]) & set(plan["baseline_complete_ids"]):
        raise ValueError("Conditional missed-table denominator cannot include baseline complete tables")
    rules = plan.get("critical_error_rules")
    if not isinstance(rules, dict) or not rules or not all(isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in rules.items()):
        raise ValueError("Freeze named critical-error definitions shared by development and holdout")
    if plan["split"] == "holdout" and ("selected_parameters" not in plan or
            plan["parameter_grid"] and plan["selected_parameters"] not in plan["parameter_grid"]):
        raise ValueError("Select the final parameters before opening holdout")


def open_ledger(path):
    ledger = load(path)
    if digest(ledger["plan"]) != ledger["plan_sha256"]:
        raise ValueError("Frozen plan changed; start a separately reported development experiment")
    previous = ledger["plan_sha256"]
    for record in ledger["events"]:
        if record["previous"] != previous or digest({k: v for k, v in record.items() if k != "sha256"}) != record["sha256"]:
            raise ValueError("Event history changed")
        previous = record["sha256"]
    return ledger


def record_event(ledger, event, actual_bundle):
    plan, events = ledger["plan"], ledger["events"]
    operation, case_id = event.get("operation"), event.get("case_id")
    if operation not in OPERATIONS or case_id not in plan["cases"]:
        raise ValueError("Use one of the eight frozen operations and a frozen case_id")
    score_hash = event.get("scored_output_sha256", "")
    if not re.fullmatch(r"[a-f0-9]{64}", score_hash):
        raise ValueError("Record the actual scored-content hash, excluding report formatting")
    prior = [r for r in events if r["event"]["case_id"] == case_id]
    if operation == "first_parse" and prior:
        raise ValueError("first_parse is allowed only once per frozen case")
    if not prior and operation != "first_parse":
        raise ValueError("Record the first parse before reading/revising a case")
    cost = OPERATIONS[operation]
    previous_bundle = events[-1]["bundle_sha256"] if events else ledger["bundle_sha256"]
    if not cost and actual_bundle != previous_bundle:
        raise ValueError("A changed implementation cannot be recorded as a free operation")
    if not cost and prior and score_hash != prior[-1]["event"]["scored_output_sha256"]:
        raise ValueError("A changed scored answer cannot be recorded as reading/cache/report formatting")
    if not cost and prior and any(event.get(key, {} if key == "detected_fragments" else []) !=
            prior[-1]["event"].get(key, {} if key == "detected_fragments" else [])
            for key in ("detected_fragments", "critical_errors", "parameters")):
        raise ValueError("A free operation cannot change measurements, errors, or parameters")
    if sum(r["cost"] for r in events) + cost > plan["max_rounds"]:
        raise ValueError("Frozen repair/tuning round budget exhausted")
    if plan["split"] == "holdout" and (cost or actual_bundle != ledger["bundle_sha256"]):
        raise ValueError("Holdout cannot be used to tune, repair, change parameters, or select another implementation")
    if operation == "parameter_candidate" and event.get("parameters") not in plan["parameter_grid"]:
        raise ValueError("Parameter candidate lies outside the frozen grid")
    if plan["split"] == "holdout" and event.get("parameters", plan["selected_parameters"]) != plan["selected_parameters"]:
        raise ValueError("Holdout parameters differ from the frozen final selection")
    detections = event.get("detected_fragments", {})
    if not isinstance(detections, dict):
        raise ValueError("detected_fragments must be a complete current snapshot")
    tables = {t["id"]: t for t in plan["logical_tables"] if t["case_id"] == case_id}
    for table_id, found in detections.items():
        if (table_id not in tables or not isinstance(found, list)
                or not all(isinstance(f, str) for f in found) or not set(found).issubset(tables[table_id]["fragments"])):
            raise ValueError("Detected fragments must match the frozen original-page table annotations")
    if not isinstance(event.get("critical_errors", []), list):
        raise ValueError("critical_errors must be an array")
    for error in event.get("critical_errors", []):
        if (not isinstance(error, dict) or error.get("kind") not in ERROR_KINDS
                or not error.get("location") or not error.get("reason")):
            raise ValueError("Critical-error records need kind, original location, and reason")
        if error["kind"] != "rule_undecidable" and error.get("rule_id") not in plan["critical_error_rules"]:
            raise ValueError("Known/new-mechanism errors must reference the same frozen consequence rule")
        if error["kind"] == "rule_undecidable" and not error.get("why_no_rule"):
            raise ValueError("Explain why no frozen consequence rule can decide the error")
    record = {"previous": events[-1]["sha256"] if events else ledger["plan_sha256"],
              "cost": cost, "bundle_sha256": actual_bundle, "event": event}
    record["sha256"] = digest(record)
    events.append(record)


def summary(ledger):
    plan = ledger["plan"]
    latest = {}
    for record in ledger["events"]:
        latest[record["event"]["case_id"]] = record["event"]
    complete, partial = set(), set()
    for table in plan["logical_tables"]:
        found = set(latest.get(table["case_id"], {}).get("detected_fragments", {}).get(table["id"], []))
        if found == set(table["fragments"]):
            complete.add(table["id"])
        elif found:
            partial.add(table["id"])
    conditional = set(plan["conditional_ids"])
    current_errors = [err for event in latest.values() for err in event.get("critical_errors", [])]
    all_errors = [err for r in ledger["events"] for err in r["event"].get("critical_errors", [])]
    unresolved = sum(e["kind"] == "rule_undecidable" for e in current_errors)
    return {"split": plan["split"], "plan_sha256": ledger["plan_sha256"],
            "rounds_used": sum(r["cost"] for r in ledger["events"]), "max_rounds": plan["max_rounds"],
            "logical_table_denominator": len(plan["logical_tables"]), "complete_tables": sorted(complete),
            "partial_tables": sorted(partial), "logical_table_recall": len(complete) / len(plan["logical_tables"]) if plan["logical_tables"] else None,
            "conditional_denominator": len(conditional), "conditional_new_complete": len(complete & conditional),
            "conditional_recall": len(complete & conditional) / len(conditional) if conditional else None,
            "current_critical_errors": sum(e["kind"] != "rule_undecidable" for e in current_errors),
            "unresolved_rule_cases": unresolved, "historical_error_observations": len(all_errors),
            "all_cases_observed": set(latest) == set(plan["cases"]),
            "certification": "measurement_only; semantic correctness and original annotations require review"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "record", "summary"))
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--event", type=Path)
    parser.add_argument("--development-ledger", type=Path, help="mandatory before freezing holdout")
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    try:
        if args.action == "freeze":
            if args.ledger.exists() or not args.plan:
                raise ValueError("Supply an external plan and a new ledger path; never reset an existing ledger")
            plan = load(args.plan)
            check_plan(plan)
            if plan["split"] == "holdout":
                if not args.development_ledger:
                    raise ValueError("Link the frozen development ledger before opening holdout")
                development = open_ledger(args.development_ledger)
                if development["plan"]["split"] != "development":
                    raise ValueError("Expected a development ledger")
                for key in ("critical_error_rules", "parameter_grid"):
                    if development["plan"][key] != plan[key]:
                        raise ValueError(f"Holdout cannot revise the frozen {key}")
                final_bundle = development["events"][-1]["bundle_sha256"] if development["events"] else development["bundle_sha256"]
                if final_bundle != bundle_hash(args.bundle):
                    raise ValueError("Holdout implementation differs from the last recorded development version")
                plan["development_plan_sha256"] = development["plan_sha256"]
            ledger = {"plan": plan, "plan_sha256": digest(plan), "bundle_sha256": bundle_hash(args.bundle), "events": []}
        else:
            ledger = open_ledger(args.ledger)
            if args.action == "record":
                if not args.event:
                    raise ValueError("Supply the actual event record")
                record_event(ledger, load(args.event), bundle_hash(args.bundle))
        if args.action != "summary":
            args.ledger.parent.mkdir(parents=True, exist_ok=True)
            write_json(args.ledger, ledger)
        print(json.dumps(summary(ledger), ensure_ascii=True, allow_nan=False))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as err:
        print(json.dumps({"status": "invalid", "reason": str(err)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
