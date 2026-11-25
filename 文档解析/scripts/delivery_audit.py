"""Validate runtime evidence records without supplying scientific answers."""
from __future__ import annotations

from pathlib import Path

import case_contract as contract


FIELDS = ("status", "evidence", "uncertainties", "changes", "coverage")


def pointer_exists(value, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return False
    try:
        for part in pointer[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            value = value[int(part)] if isinstance(value, list) and part.isdigit() else value[part]
        return True
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def page_list(value):
    return (isinstance(value, list) and all(type(p) is int and p > 0 for p in value)
            and len(set(value)) == len(value))


def validate(audit, result, ctx, args, inside, read_json, file_hash, scoped_pages):
    """Check locations against the actual PDF parse, and keep semantic review explicit."""
    issues = []
    if not isinstance(audit, dict):
        return ["Supply a runtime audit object using --audit or the result's audit fields"]
    issues.extend(f"Audit field is required: {key}" for key in FIELDS if key not in audit)
    if issues:
        return issues
    kind = contract.status_kind(audit["status"])
    if not kind:
        issues.append("Audit status must mean success, partial, indeterminate, or refused")
    result_kind = contract.status_kind(result.get("status"))
    if result_kind and result_kind != kind:
        issues.append("Result and audit completion states disagree")
    if kind != "success" and not contract.has_reason(audit.get("reason")):
        issues.append("Non-success audit needs the actual reason")
    for key in FIELDS[1:]:
        if not isinstance(audit[key], list):
            issues.append(f"Audit {key} must be an array")
    if issues:
        return issues

    sources = {item["path"]: item for item in ctx["sources"]}
    verified_pages, page_records, covered, native_reviewed = {}, {}, set(), set()
    for entry in audit["coverage"]:
        if not isinstance(entry, dict) or entry.get("source") not in sources:
            issues.append("Coverage must reference a declared source")
            continue
        name = entry["source"]
        if name in covered:
            issues.append(f"Merge coverage entries for source: {name}")
        covered.add(name)
        if entry.get("not_requested") is True:
            if not contract.has_reason(entry.get("reason")):
                issues.append(f"Explain the task scope excluding: {name}")
            continue
        if entry.get("unavailable") is True:
            if not contract.has_reason(entry.get("reason")):
                issues.append(f"Explain the unavailable source and handover location: {name}")
            if kind == "success":
                issues.append(f"Success cannot rely on an unavailable required source: {name}")
            continue
        if sources[name].get("kind") != "pdf":
            if not contract.has_reason(entry.get("locator")):
                issues.append(f"Non-PDF coverage needs actual paragraph/sheet/row locations: {name}")
                continue
            try:
                native = inside(args.case.parent, name, "/input")
                if not native.is_file():
                    raise ValueError("declared attachment is absent; use unavailable with an actual reason")
                expected = sources[name].get("sha256")
                if not expected or expected != file_hash(native):
                    raise ValueError("attachment hash is missing or differs from prepare; preserve the original input")
                native_reviewed.add(name)
            except (OSError, ValueError, TypeError) as err:
                issues.append(f"Invalid native coverage for {name}: {err}")
            continue
        try:
            manifest_path = inside(args.work, entry.get("manifest"), "/app/work")
            manifest = read_json(manifest_path)
            source_path = inside(args.case.parent, name, "/input")
            if manifest.get("source", {}).get("sha256") != file_hash(source_path):
                raise ValueError("manifest and current PDF hashes differ")
            parsed = {p["physical_page"]: p for p in manifest.get("pages", [])}
            required, reviewed = entry.get("required_pages"), entry.get("reviewed_pages")
            if not page_list(required) or not page_list(reviewed):
                raise ValueError("required_pages and reviewed_pages must be unique positive page lists")
            count = manifest.get("page_count")
            if type(count) is not int or any(p > count for p in required):
                raise ValueError("required pages exceed the actual PDF")
            allowed = sources[name].get("allowed_physical_pages")
            if allowed:
                bounds = scoped_pages(allowed)
                expected = {p for p in range(1, count + 1) if any(a <= p <= b for a, b in bounds)}
                if set(required) != expected:
                    raise ValueError("required_pages must retain the full declared evidence scope")
            if entry.get("scope_confirmed") is not True:
                raise ValueError("explicitly confirm the required scope against task.md")
            if not set(reviewed).issubset(parsed):
                raise ValueError("reviewed page was never parsed")
            if not set(reviewed).issubset(required):
                raise ValueError("reviewed page lies outside the audited task scope")
            verified_pages[name] = set(reviewed)
            if kind == "success" and set(required) != set(reviewed):
                issues.append(f"Success cannot omit required pages: {name}")
            for number in reviewed:
                record = read_json(inside(manifest_path.parent, parsed[number]["data"]))
                if record.get("physical_page") != number:
                    raise ValueError("page JSON has a conflicting physical page")
                page_records[(name, number)] = record
                if kind == "success" and record.get("error"):
                    issues.append(f"Success claims an unreadable page: {name} p{number}")
        except (OSError, ValueError, TypeError, KeyError) as err:
            issues.append(f"Invalid coverage for {name}: {err}")

    if kind == "success":
        for name in sources.keys() - covered:
            issues.append(f"Declare reviewed or explicitly out-of-task source coverage: {name}")
        if not audit["evidence"]:
            issues.append("A successful scientific result needs actual source evidence")

    evidence_ids = set()
    for item in audit["evidence"]:
        if not isinstance(item, dict):
            issues.append("Each evidence entry must be an object")
            continue
        name, eid = item.get("source"), item.get("id")
        if not isinstance(eid, str) or not eid.strip() or eid in evidence_ids:
            issues.append("Evidence IDs must be nonempty and unique")
        evidence_ids.add(eid if isinstance(eid, str) else "")
        supports = item.get("supports")
        if not isinstance(supports, list) or not supports or not all(pointer_exists(result, p) for p in supports):
            issues.append(f"Evidence {eid} needs valid JSON pointers to supported result fields")
        if name not in sources:
            issues.append(f"Evidence {eid} references an undeclared source")
            continue
        if item.get("method") not in {"text_layer", "ocr", "visual", "calculation", "native_file"}:
            issues.append(f"Evidence {eid} needs its actual extraction method")
        if item.get("verification") not in {"verified", "candidate"}:
            issues.append(f"Evidence {eid} needs a verification state")
        if kind == "success" and item.get("verification") != "verified":
            issues.append(f"Success cannot rely on unverified evidence: {eid}")
        if sources[name].get("kind") != "pdf":
            if name not in native_reviewed:
                issues.append(f"Evidence {eid} must reference an available, hash-checked native attachment")
            if not contract.has_reason(item.get("locator")):
                issues.append(f"Evidence {eid} needs a native file location")
            continue
        number = item.get("physical_page")
        if type(number) is not int or number not in verified_pages.get(name, set()):
            issues.append(f"Evidence {eid} must reference an actually reviewed physical PDF page")
            continue
        record = page_records.get((name, number), {})
        target = item.get("block_id")
        known_ids = {node.get("id") for key in ("blocks", "tables", "images")
                     for node in record.get(key, [])}
        known_ids.update(cell.get("id") for table in record.get("tables", []) for cell in table.get("cells", []))
        known_ids.update(line.get("id") for line in record.get("ocr", {}).get("lines", []))
        bbox = item.get("bbox")
        valid_box = (isinstance(bbox, list) and len(bbox) == 4
                     and all(type(n) in (int, float) for n in bbox)
                     and 0 <= bbox[0] < bbox[2] <= record.get("width_pt", 0)
                     and 0 <= bbox[1] < bbox[3] <= record.get("height_pt", 0))
        if (target is not None and target not in known_ids) or (not target and not valid_box):
            issues.append(f"Evidence {eid} needs an existing block/cell ID or a real page bbox")
        if bbox is not None and not valid_box:
            issues.append(f"Evidence {eid} has an invalid page bbox")

    for item in audit["uncertainties"]:
        if not isinstance(item, dict) or not contract.has_reason(item.get("issue")) or type(item.get("blocking")) is not bool:
            issues.append("Uncertainty entries need issue and an explicit blocking boolean")
        elif kind == "success" and item["blocking"]:
            issues.append("Success is incompatible with unresolved blocking uncertainty")
    for item in audit["changes"]:
        if not isinstance(item, dict) or not all(k in item for k in ("before", "after", "reason", "evidence_ids")):
            issues.append("Changes need before, after, reason, and evidence_ids")
        elif (not contract.has_reason(item["reason"]) or not isinstance(item["evidence_ids"], list)
              or not item["evidence_ids"] or not all(eid in evidence_ids for eid in item["evidence_ids"])):
            issues.append("A correction must cite its actual evidence and reason")
    return issues[:30]


def incomplete(case_id, reason, kind="indeterminate"):
    return {"case_id": case_id, "status": kind, "reason": reason, "evidence": [],
            "uncertainties": [{"issue": reason, "blocking": True}], "changes": [], "coverage": [],
            "validation": {"complete": False}}
