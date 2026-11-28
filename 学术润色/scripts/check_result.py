#!/usr/bin/env python3
"""Inspect a live Harbor case's result keys, simple types and named files."""

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path, PureWindowsPath


JSON_TYPES = {
    "object": (dict,), "array": (list,), "string": (str,), "integer": (int,),
    "number": (int, float), "boolean": (bool,), "null": (type(None),),
}
CONTRACT_META = {"description", "notes", "file", "filename", "format", "example", "examples", "title", "type"}
ARTIFACT_KEY = re.compile(r"(?:^|_)(?:artifacts?|deliverables?|files?|filename|paths?|previews?|outputs?|exports?)(?:$|_)", re.I)
SOURCE_KEY = re.compile(r"(?:^|_)(?:source|inputs?|evidence|provenance|locator|original|reason|claims?|changes?|issues|uncertainty)(?:$|_)", re.I)
ARTIFACT_PATH = re.compile(r"[^\r\n<>|?:*]{1,220}\.(?:md|txt|tex|bib|json|csv|tsv|docx|xlsx|pptx|pdf|png|svg|drawio|html)", re.I)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError(f"unsupported JSON constant: {value}")


def load_json(raw):
    return json.loads(
        raw, object_pairs_hook=unique_object, parse_constant=invalid_constant
    )


def fields_to_schema(fields, notes):
    properties = {}
    required = []
    if isinstance(fields, dict):
        fields = [
            {**value, "name": name} if isinstance(value, dict) else {"name": name, "type": value}
            for name, value in fields.items()
        ]
    for item in fields:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            notes.append("output_contract contains a field descriptor that needs manual reading")
            continue
        name = item.get("name") or item.get("field") or item.get("key")
        if not isinstance(name, str) or not name:
            notes.append("output_contract contains a descriptor without a recognized field name")
            continue
        specification = {key: value for key, value in item.items() if key in {
            "type", "enum", "const", "properties", "items", "additionalProperties", "minLength", "minItems",
        }}
        if isinstance(item.get("required"), list):
            specification["required"] = item["required"]
        properties[name] = specification
        if item.get("required") is not False:
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


def extract_schema(contract, notes, depth=0):
    if depth > 5:
        notes.append("output_contract nesting needs manual reading")
        return {}
    if isinstance(contract, str):
        try:
            content = load_json(contract)
        except ValueError:
            notes.append("output_contract is prose; check every stated requirement manually")
            return {}
        if isinstance(content, (dict, list)):
            return extract_schema(content, notes, depth + 1)
    if isinstance(contract, list):
        return fields_to_schema(contract, notes)
    if isinstance(contract, dict):
        if isinstance(contract.get("properties"), dict):
            return contract
        for key in ("result.json", "result_json", "json_schema", "schema"):
            value = contract.get(key)
            if isinstance(value, (dict, list, str)):
                return extract_schema(value, notes, depth + 1)
        required = contract.get("required_fields", contract.get("required"))
        fields = contract.get("fields")
        if isinstance(fields, (list, dict)):
            schema = fields_to_schema(fields, notes)
            if isinstance(required, list) and all(isinstance(key, str) for key in required):
                schema["required"] = required
            return schema
        if isinstance(required, list):
            return fields_to_schema(required, notes)
        mapping = {name: value for name, value in contract.items() if name not in CONTRACT_META}
        if mapping and all(isinstance(value, (str, dict)) for value in mapping.values()):
            notes.append("interpreted output_contract as a field mapping; confirm intended nesting")
            return fields_to_schema(mapping, notes)
    notes.append("unrecognized output_contract shape; use the actual contract to verify result fields")
    return {}


def inspect(value, schema, location, issues, notes, checked):
    if not isinstance(schema, dict):
        notes.append(f"{location}: schema needs manual reading")
        return
    if any(key in schema for key in ("allOf", "anyOf", "oneOf", "$ref", "pattern", "format", "minimum", "maximum")):
        notes.append(f"{location}: schema constraints beyond shape need manual review")
    declared = schema.get("type")
    types = declared if isinstance(declared, list) else [declared]
    if declared is not None:
        if all(isinstance(kind, str) and kind in JSON_TYPES for kind in types):
            if not any(type(value) in JSON_TYPES[kind] for kind in types):
                issues.append(f"{location}: expected {' or '.join(types)}, got {type(value).__name__}")
                return
        else:
            notes.append(f"{location}: type descriptor needs manual reading")
    if "const" in schema and value != schema["const"]:
        issues.append(f"{location}: value differs from the required constant")
    if isinstance(schema.get("enum"), list) and not any(
        type(value) is type(choice) and value == choice for choice in schema["enum"]
    ):
        issues.append(f"{location}: value is outside the allowed choices")
    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    issues.append(f"{location}.{key}: required field is missing")
        for key, subschema in properties.items():
            if key in value:
                child = f"{location}.{key}"
                checked.append(child)
                inspect(value[key], subschema, child, issues, notes, checked)
        if schema.get("additionalProperties") is False:
            for key in sorted(set(value) - set(properties)):
                issues.append(f"{location}.{key}: field is outside the contract")
    elif isinstance(value, list):
        if type(schema.get("minItems")) is int and len(value) < schema["minItems"]:
            issues.append(f"{location}: too few items")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                inspect(item, schema["items"], f"{location}[{index}]", issues, notes, checked)
    elif isinstance(value, str) and type(schema.get("minLength")) is int:
        if len(value) < schema["minLength"]:
            issues.append(f"{location}: string is shorter than minLength")


def artifact_record(root, name, issues):
    path = Path(name)
    relative = path.as_posix()
    win_path = PureWindowsPath(name)
    if path.is_absolute() or win_path.drive or win_path.root or ".." in win_path.parts or not name.strip():
        issues.append(f"artifact must be relative to the result directory: {name}")
        return None
    absolute = (root / path).resolve()
    if not absolute.is_relative_to(root) or not absolute.is_file() or not absolute.stat().st_size:
        issues.append(f"artifact is missing, empty or outside the result directory: {name}")
        return None
    suffix = absolute.suffix.lower()
    signatures = {".pdf": b"%PDF-", ".png": b"\x89PNG\r\n\x1a\n"}
    if suffix in signatures:
        with absolute.open("rb") as handle:
            if handle.read(len(signatures[suffix])) != signatures[suffix]:
                issues.append(f"artifact has the wrong file signature: {name}")
                return None
    office_parts = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}
    if suffix in office_parts:
        try:
            with zipfile.ZipFile(absolute) as office:
                office.getinfo(office_parts[suffix])
        except (zipfile.BadZipFile, KeyError):
            issues.append(f"artifact is not a usable {suffix} package: {name}")
            return None
    return {"file": relative, "bytes": absolute.stat().st_size}


def declared_artifacts(value, expected=False):
    if isinstance(value, dict):
        for key, part in value.items():
            name = re.sub(r"([a-z])([A-Z])", r"\1_\2", key)
            if not SOURCE_KEY.search(name):
                yield from declared_artifacts(part, expected or bool(ARTIFACT_KEY.search(name)))
    elif isinstance(value, list):
        for part in value:
            yield from declared_artifacts(part, expected)
    elif expected and isinstance(value, str) and ARTIFACT_PATH.fullmatch(value):
        yield value


def check_documents(raw_case, raw_result, root, additional=()):
    case = load_json(raw_case.decode("utf-8-sig"))
    result = load_json(raw_result.decode("utf-8-sig"))
    if not isinstance(case, dict) or not isinstance(result, dict):
        raise ValueError("case and result JSON must both be objects")
    issues, notes, checked = [], [], []
    if "case_id" not in result:
        issues.append("$.case_id: required field is missing")
    elif "case_id" not in case:
        notes.append("source case_id is not a top-level field; confirm its location manually")
    elif type(case["case_id"]) is not type(result["case_id"]) or case["case_id"] != result["case_id"]:
        issues.append("$.case_id: value or type differs from the input case")
    schema = extract_schema(case.get("output_contract"), notes)
    inspect(result, schema, "$", issues, notes, checked)
    names = dict.fromkeys([*declared_artifacts(result), *additional])
    artifacts = [artifact_record(root.resolve(), name, issues) for name in names]
    status = "issues" if issues else ("manual_review" if notes else "passed")
    report = {
        "status": status, "scope": "result shape, case_id and discoverable or explicitly named artifacts",
        "case_sha256": hashlib.sha256(raw_case).hexdigest(),
        "result_sha256": hashlib.sha256(raw_result).hexdigest(),
        "checked_fields": checked, "issues": list(dict.fromkeys(issues)),
        "manual_review": list(dict.fromkeys(notes)),
        "artifacts": [entry for entry in artifacts if entry],
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case")
    parser.add_argument("result")
    parser.add_argument("--artifact", action="append", default=[], help="Verify an additional relative output file")
    args = parser.parse_args()
    result_path = Path(args.result)
    report = check_documents(Path(args.case).read_bytes(), result_path.read_bytes(), result_path.parent, args.artifact)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["issues"] else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (OSError, UnicodeError, ValueError) as error:
        print(json.dumps({"status": "execution_failed", "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
