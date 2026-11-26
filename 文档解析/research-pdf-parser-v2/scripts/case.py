#!/usr/bin/env python3
"""Inspect the current case and publish an agent-written result against its contract."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
from pdf import digest, json_text, write_file


RESULT_KEYS = ("result.json", "/app/output/result.json", "result_json", "result_schema", "json_schema", "schema")
SCHEMA_KEYS = {"$schema", "$ref", "properties", "patternProperties", "allOf", "anyOf", "oneOf",
               "additionalProperties", "unevaluatedProperties"}


def unique_keys(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise ValueError(f"duplicate JSON key: {k}")
        obj[k] = v
    return obj


def reject_constant(token):
    raise ValueError(f"invalid JSON constant: {token}")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                      object_pairs_hook=unique_keys, parse_constant=reject_constant)


def load_case(path):
    case = load_json(path)
    if not isinstance(case, dict):
        raise ValueError("case.json must be a JSON object")
    case_id = case.get("case_id")
    if not (type(case_id) is int or isinstance(case_id, str) and case_id.strip()):
        raise ValueError("case.json has no usable case_id")
    return case


def results_from_files(collection):
    if isinstance(collection, dict):
        for name, spec in collection.items():
            if name in RESULT_KEYS:
                yield spec
    if isinstance(collection, list):
        for entry in collection:
            if not isinstance(entry, dict):
                continue
            if any(isinstance(entry.get(k), str) and entry[k] in RESULT_KEYS
                   for k in ("path", "file", "filename", "name")):
                yield entry


def field_names(seq):
    return (list(dict.fromkeys(seq)) if isinstance(seq, list) and seq
            and all(isinstance(f, str) and re.fullmatch(r"[\w.\-]+", f) for f in seq) else [])


def read_rules(value, depth=0):
    if depth > 5 or not isinstance(value, dict):
        return None, field_names(value)
    for key in RESULT_KEYS:
        if key in value:
            schema, fields = read_rules(value[key], depth + 1)
            if schema is not None or fields:
                return schema, fields
    if SCHEMA_KEYS.intersection(value) or value.get("type") == "object":
        return value, []
    for key in ("files", "outputs", "artifacts"):
        for spec in results_from_files(value.get(key)):
            schema, fields = read_rules(spec, depth + 1)
            if schema is not None or fields:
                return schema, fields
    for key in ("required_fields", "required", "fields"):
        fields = field_names(value.get(key))
        if fields:
            return None, fields
    return None, []


def inventory(root):
    items = []
    for path in sorted(root.rglob("*")):
        try:
            if path.is_file() and path.resolve().is_relative_to(root):
                items.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size})
        except OSError:
            continue
    return items


def inspect_case(args):
    path = Path(args.case).resolve(strict=True)
    case = load_case(path)
    contract = case.get("output_contract")
    schema, fields = read_rules(contract)
    task = Path(args.task).resolve()
    out = {"case_id": case["case_id"], "case_path": str(path), "case_sha256": digest(path),
           "task_path": str(task), "task_exists": task.is_file(), "case_keys": list(case),
           "contract_mode": "json_schema" if schema is not None else "field_names" if fields else "review_task",
           "output_contract": contract, "observed_files": inventory(path.parent)}
    print(json_text(out), end="")
    return 0


def closed_without_id(schema):
    props = schema.get("properties")
    return (isinstance(props, dict) and "case_id" not in props and "case_id" not in schema.get("required", [])
            and schema.get("additionalProperties") is False
            and not any(k in schema for k in ("patternProperties", "$ref", "allOf", "oneOf", "anyOf")))


def references(schema):
    if isinstance(schema, dict):
        for key, node in schema.items():
            if key in ("$ref", "$dynamicRef", "$recursiveRef") and isinstance(node, str):
                yield node
            elif isinstance(node, (dict, list)):
                yield from references(node)
    if isinstance(schema, list):
        for node in schema:
            yield from references(node)


def schema_errors(schema, result, checked_manually):
    if any(not ref.startswith("#") for ref in references(schema)):
        if checked_manually:
            return ["uses external JSON Schema references; manual contract check recorded"], []
        return [], ["external JSON Schema references require review against the supplied local contract"]
    try:
        from jsonschema import exceptions, validators
    except ImportError:
        if checked_manually:
            return ["jsonschema unavailable; manual contract check recorded"], []
        return [], ["jsonschema is unavailable; use --reviewed-contract after checking the full schema"]
    notices = []
    subject = result
    if closed_without_id(schema):
        subject = {k: v for k, v in result.items() if k != "case_id"}
        notices.append("result carries the required case_id; closed schema checked its declared object fields")
    try:
        cls = validators.validator_for(schema)
        cls.check_schema(schema)
        found = []
        for error in cls(schema).iter_errors(subject):
            pointer = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1")
                                      for part in error.absolute_path)
            found.append(f"{pointer}: {error.message[:450]}")
            if len(found) == 20:
                break
        return notices, found
    except exceptions.SchemaError as exc:
        return notices, [f"case contains an invalid JSON Schema: {exc.message}"]
    except Exception as exc:
        return notices, [f"schema validation could not run: {exc}"]


def check_contract(case, result, reviewed):
    schema, fields = read_rules(case.get("output_contract"))
    if schema is not None:
        notes, errors = schema_errors(schema, result, reviewed)
        return "json_schema", notes, errors
    if fields:
        missing = [f"contract field missing: {field}" for field in fields if field not in result]
        if not reviewed:
            missing.append("check types, field meanings and other prose requirements, then use --reviewed-contract")
        return "field_names", [], missing
    if reviewed:
        return "manual", ["agent recorded a review of prose or unrecognized contract form"], []
    return "manual", [], ["review case.json and task.md, then use --reviewed-contract"]


def check_artifacts(root, names):
    failures = []
    for name in names:
        rel = Path(name)
        if rel.is_absolute() or ".." in rel.parts:
            failures.append(f"expected an artifact path relative to --output-dir: {name}")
            continue
        p = (root / rel).resolve()
        if not (p.is_relative_to(root) and p.name != "result.json" and p.is_file() and p.stat().st_size > 0):
            failures.append(f"missing or empty artifact: {name}")
    return failures


def check_pdf_anchor(entry, actual, page):
    n = entry.get("physical_page")
    if type(n) is not int or n < 1:
        raise ValueError("PDF source check needs a 1-based physical_page")
    line_ids, cell_ids, word_ids = (entry.get(k, []) for k in ("line_ids", "cell_ids", "word_ids"))
    if any(not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)
           for ids in (line_ids, cell_ids, word_ids)):
        raise ValueError("line_ids, cell_ids and word_ids must be lists of known IDs")
    bbox = entry.get("bbox")
    if bbox is not None and not (isinstance(bbox, list) and len(bbox) == 4
                                 and all(type(v) in (float, int) and math.isfinite(v) for v in bbox)
                                 and 0 <= bbox[0] < bbox[2] and 0 <= bbox[1] < bbox[3]):
        raise ValueError("bbox must be x0,y0,x1,y1 in displayed page points")
    bounds = None
    if index := entry.get("index"):
        index = (page / index).resolve()
        manifest = load_json(index / "manifest.json")
        if manifest["source"]["sha256"] != actual:
            raise ValueError("source hash differs from the page index")
        match = next((p for p in manifest["pages"] if p["physical_page"] == n), None)
        if not match:
            raise ValueError(f"physical page {n} is absent from the index")
        data = load_json(index / match["file"])
        if data["physical_page"] != n or not set(line_ids).issubset({ln["id"] for ln in data["lines"]}):
            raise ValueError("one or more source line IDs are absent on the indexed page")
        bounds = [0, 0, data["width_pt"], data["height_pt"]]
    elif line_ids:
        raise ValueError("line IDs require the source page index")
    if sidecar := entry.get("evidence_data"):
        info = load_json(page / sidecar)
        if info["sha256"] != actual or info["physical_page"] != n:
            raise ValueError("source hash or physical page differs from the evidence data")
        if not set(cell_ids).issubset({c["id"] for t in info.get("tables", []) for c in t["cells"]}):
            raise ValueError("one or more source cell IDs are absent on the indicated page")
        if not set(word_ids).issubset({w["id"] for w in info.get("words", [])}):
            raise ValueError("one or more source OCR word IDs are absent on the indicated page")
        if "image" in info and not Path(info["image"]).is_file():
            raise ValueError("render metadata points to a missing image")
        if bounds is None:
            bounds = info.get("region_bbox", info.get("bbox"))
    elif cell_ids or word_ids:
        raise ValueError("cell IDs or word IDs require the corresponding evidence_data JSON")
    if not (line_ids or cell_ids or word_ids or bbox is not None):
        raise ValueError("PDF source check needs line IDs, cell IDs, OCR word IDs or a bbox")
    if bbox is not None and (bounds is None or any((bbox[0] < bounds[0] - 0.05, bbox[1] < bounds[1] - 0.05,
                                                   bbox[2] > bounds[2] + 0.05, bbox[3] > bounds[3] + 0.05))):
        raise ValueError("bbox is outside the available source page or crop")


def embedded_anchor(result, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("result_pointer needs a JSON Pointer beginning with /")
    node = result
    for segment in pointer[1:].split("/"):
        key = segment.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
            node = node[int(key)]
        else:
            node = node[key]
    if not isinstance(node, dict):
        raise ValueError("result_pointer must point to an actual evidence object")
    return node


def check_embedded(entry, result):
    if "result_pointer" not in entry:
        return
    anchor = embedded_anchor(result, entry["result_pointer"])
    if anchor.get("source", anchor.get("file")) != entry["source"]:
        raise ValueError("embedded evidence source differs from the checked source")
    if "physical_page" in entry and anchor.get("physical_page") != entry["physical_page"]:
        raise ValueError("embedded evidence physical page differs from the checked page")
    if "bbox" in entry and anchor.get("bbox") != entry["bbox"]:
        raise ValueError("embedded evidence bbox differs from the checked region")
    if "locator" in entry and anchor.get("locator") != entry["locator"]:
        raise ValueError("embedded evidence locator differs from the checked locator")
    for key in ("line_id", "cell_id", "word_id"):
        plural = key + "s"
        if plural in entry and anchor.get(plural, [anchor.get(key)]) != entry[plural]:
            raise ValueError(f"embedded evidence {plural} differ from the checked IDs")


def source_errors(path, case_path, result):
    checks = load_json(path)
    if not isinstance(checks, list) or not checks:
        raise ValueError("--source-checks expects a nonempty JSON list")
    root = Path(case_path).resolve().parent
    work = Path(path).resolve().parent
    hashes, errors = {}, []
    for i, entry in enumerate(checks, 1):
        try:
            check_embedded(entry, result)
            source = (root / entry["source"]).resolve(strict=True)
            if not source.is_file():
                raise ValueError(f"source is not a regular file: {source}")
            if source not in hashes:
                hashes[source] = digest(source)
            actual = hashes[source]
            if source.suffix.casefold() == ".pdf" or "physical_page" in entry:
                check_pdf_anchor(entry, actual, work)
            elif entry.get("sha256") != actual or not str(entry.get("locator") or "").strip():
                raise ValueError("non-PDF evidence needs its current sha256 and a real field, paragraph or cell locator")
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            errors.append(f"source check {i}: {exc}")
    return errors


def publish(args):
    case = load_case(args.case)
    draft = load_json(args.draft)
    if not isinstance(draft, dict):
        raise ValueError("result draft must be a JSON object")
    case_id = case["case_id"]
    if "case_id" in draft and (type(draft["case_id"]) is not type(case_id) or draft["case_id"] != case_id):
        raise ValueError("draft case_id does not match original case.json")
    result = {**draft, "case_id": case_id}
    root = Path(args.output_dir).resolve()
    mode, notices, errors = check_contract(case, result, args.reviewed_contract)
    errors.extend(check_artifacts(root, args.artifact))
    if args.source_checks:
        errors.extend(source_errors(args.source_checks, args.case, result))
    report = {"case_id": case_id, "contract_check": mode, "notices": notices, "errors": errors,
              "source_checks_supplied": bool(args.source_checks), "result_path": None, "written": False}
    if not errors and args.action == "publish":
        path = root / "result.json"
        write_file(path, json_text(result))
        report.update(result_path=str(path), written=True)
    print(json_text(report), end="")
    return int(bool(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--case", default="/input/case.json")
    inspect.add_argument("--task", default="/input/task.md")
    inspect.set_defaults(run=inspect_case)
    for name in ("check", "publish"):
        p = sub.add_parser(name)
        p.add_argument("--case", default="/input/case.json")
        p.add_argument("--draft", default="/app/work/result.draft.json")
        p.add_argument("--output-dir", default="/app/output")
        p.add_argument("--artifact", action="append", default=[], help="required existing artifact relative to --output-dir")
        p.add_argument("--source-checks", help="JSON list of actual source paths and evidence anchors to verify")
        p.add_argument("--reviewed-contract", action="store_true", help="confirm you read and checked contract constraints not verified by the script")
        p.set_defaults(run=publish)
    args = parser.parse_args()
    try:
        return args.run(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(json_text({"operation": args.action, "written": False, "error": str(exc)}), end="", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
