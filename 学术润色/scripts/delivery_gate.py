#!/usr/bin/env python3
"""Validate source locations, scope and actual artifacts before installing result.json."""

import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path

from case_io import load_units, relative_file, save_json, sha
from check_result import check_documents, load_json
from edit_audit import atomic_write, ensure_separate


EDITABLE_WITH_PREVIEW = {".docx", ".pptx", ".xlsx", ".svg", ".drawio"}
OPEN_KINDS = {"material_missing", "judgment_unresolved", "execution_failed"}


def read_json(path):
    return load_json(path.read_bytes().decode("utf-8-sig"))


def present(quote, units, at=None):
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("an exact nonempty excerpt is required")
    if at is not None:
        if type(at) is int:
            at = [at, at]
        if not isinstance(at, list) or len(at) != 2 or not all(type(item) is int for item in at):
            raise ValueError("locator must be an index or an inclusive index range")
        first, last = at
        if not 1 <= first <= last <= len(units):
            raise ValueError("locator is outside the captured source or result")
        units = units[first - 1:last]
    joined = "\n".join(units)
    if quote.replace("\r\n", "\n") not in joined:
        raise ValueError("excerpt not found at the stated location")
    return quote


def source_reader(intake, case_root, work):
    by_file = {item["file"]: item for item in intake["files"] if item.get("snapshot")}

    @lru_cache
    def units(name):
        if name not in by_file:
            raise ValueError(f"no source snapshot for {name!r}")
        record = by_file[name]
        original = relative_file(case_root, name).read_bytes()
        snapshot = relative_file(work, record["snapshot"]).read_bytes()
        if sha(original) != record["sha256"] or sha(snapshot) != record["snapshot_sha256"]:
            raise ValueError(f"source changed after intake: {name}")
        captured = load_units(snapshot.decode("utf-8"), record["locator"])
        if record["locator"] != "line" and len(captured) != record["units"]:
            raise ValueError(f"ambiguous native unit markers in {name}")
        return captured

    def resolve(pointer):
        return present(pointer["quote"], units(pointer["file"]), pointer["at"])

    return resolve


def result_reader(result, output, artifacts):
    @lru_cache
    def lines(name):
        if name not in artifacts:
            raise ValueError(f"target is not a checked artifact: {name}")
        return relative_file(output, name).read_text(encoding="utf-8-sig").splitlines()

    def resolve(pointer):
        if "field" in pointer:
            field = pointer["field"]
            if not isinstance(field, list) or not field:
                raise ValueError("a JSON field locator is a nonempty list of keys and indices")
            value = result
            for part in field:
                if type(part) is int and part < 0:
                    raise ValueError("array indices must be nonnegative")
                value = value[part]
            if not isinstance(value, str):
                raise ValueError("an excerpt locator must point to prose")
            return present(pointer["quote"], value.splitlines())
        return present(pointer["quote"], lines(pointer["file"]), pointer.get("at"))

    return resolve


def verify_binding(unit, sources, target):
    """An explicit local attachment lock. The writer still checks meaning."""
    for number in unit.get("bindings", []):
        index = number.get("source_index", 0)
        if type(index) is not int or not 0 <= index < len(sources):
            raise ValueError("invalid binding source_index")
        for side, excerpt in (("source", sources[index]), ("target", target)):
            owner = number.get(f"{side}_owner")
            value = number.get(f"{side}_value", number.get("value"))
            if not all(isinstance(item, str) and item.strip() and item in excerpt for item in (owner, value)):
                raise ValueError(f"{side} excerpt separates the declared owner and value")
            rivals = number.get(f"{side}_exclude", [])
            if not isinstance(rivals, list) or any(not isinstance(rival, str) or not rival.strip() for rival in rivals):
                raise ValueError(f"{side}_exclude must be a list of competing names")
            if any(rival in excerpt for rival in rivals):
                raise ValueError(f"{side} measurement excerpt includes a competing owner; select the one clause")


def verify_replay(job, work, case_root, output, artifacts, result):
    report = read_json(relative_file(work, job["report"]))
    original = relative_file(case_root, job["source"]).read_bytes()
    if job["target"] not in artifacts:
        raise ValueError("the replay target is not a checked artifact")
    candidate = relative_file(output, job["target"]).read_bytes()
    if sha(original) != report["source"]["sha256"] or sha(candidate) != report["candidate"]["sha256"]:
        raise ValueError("replay fingerprints disagree with the input or delivered manuscript")
    text = original.decode("utf-8-sig")
    cursor = 0
    parts = []
    ids = set()
    for change in report["changes"]:
        start, end = change["start"], change["end"]
        if not (
            0 <= cursor <= start < end <= len(text) and text[start:end] == change["before"]
            and change["id"] not in ids and change["reason"].strip()
        ):
            raise ValueError("missing reason, duplicate edit ID, or incorrect source span")
        first = text.count("\n", 0, start) + 1
        last = text.count("\n", 0, end - 1) + 1
        allowed = report.get("allowed_lines")
        if allowed is not None and not any(lo <= first <= last <= hi for lo, hi in allowed):
            raise ValueError("replay exceeds the authorized source range")
        ids.add(change["id"])
        parts.extend((text[cursor:start], change["after"]))
        cursor = end
    parts.append(text[cursor:])
    encoding = "utf-8-sig" if original.startswith(b"\xef\xbb\xbf") else "utf-8"
    if "".join(parts).encode(encoding) != candidate:
        raise ValueError("the change record does not reconstruct the delivered bytes")
    records = result.get("changes")
    if isinstance(records, list):
        reported = {}
        for change in records:
            if isinstance(change, dict):
                identifier = change.get("id", change.get("change_id", change.get("edit_id")))
                if isinstance(identifier, str):
                    if identifier in reported:
                        raise ValueError(f"duplicate reported edit: {identifier}")
                    reported[identifier] = change
        for edit in report["changes"]:
            record = reported.get(edit["id"], {})
            before = record.get("before", record.get("original"))
            after = record.get("after", record.get("revised"))
            if (before, after) != (edit["before"], edit["after"]):
                raise ValueError(f"result change list omits or misquotes {edit['id']}")
    return ids


def verify(args):
    work = args.intake.resolve().parent
    output = args.output.resolve()
    raw_case = args.case.read_bytes()
    draft = args.draft.read_bytes()
    intake, scope, ledger = map(read_json, (args.intake, args.scope, args.ledger))
    if intake["case_sha256"] != sha(raw_case):
        raise ValueError("case input changed after intake")
    result = load_json(draft.decode("utf-8-sig"))
    expected = scope["units"]
    if not isinstance(expected, list) or not expected or any(not isinstance(key, str) or not key for key in expected):
        raise ValueError("scope.units is a nonempty array of unique requirement IDs")
    if len(set(expected)) != len(expected):
        raise ValueError("duplicate requested unit")
    contract = check_documents(raw_case, draft, output, ledger.get("artifacts", []))
    problems = list(contract["issues"])
    artifacts = {item["file"] for item in contract["artifacts"]}
    from_source = source_reader(intake, args.case.resolve().parent, work)
    from_target = result_reader(result, output, artifacts)
    done, blocked = [], []
    seen = set()
    for unit in ledger["units"]:
        identifier = unit["id"]
        if identifier in seen:
            problems.append(f"duplicate unit {identifier}")
            continue
        seen.add(identifier)
        if unit.get("state") == "blocked" and unit.get("kind") in OPEN_KINDS and unit.get("reason", "").strip():
            blocked.append(identifier)
            continue
        try:
            if unit.get("state") != "delivered":
                raise ValueError("state must be delivered or a documented blocked unit")
            target = from_target(unit["target"])
            pointers = unit.get("sources", [])
            if not pointers and unit.get("kind") != "format_only":
                raise ValueError("at least one source pointer is required")
            sources = [from_source(pointer) for pointer in pointers]
            verify_binding(unit, sources, target)
            done.append(identifier)
        except (OSError, KeyError, IndexError, TypeError, ValueError) as error:
            problems.append(f"{identifier}: {error}")
    if set(expected) != seen:
        problems.append(f"scope mismatch; missing {sorted(set(expected) - seen)}, extra {sorted(seen - set(expected))}")
    partial = ledger.get("partial") is True
    status = result.get("status")
    if partial and isinstance(status, str) and status.casefold() in {"complete", "completed", "success", "succeeded", "完整", "成功", "完成"}:
        problems.append("reported completion conflicts with a partial delivery")
    if blocked and not partial:
        problems.append("blocked units require an honest partial delivery")
    suffixes = {Path(name).suffix.lower() for name in artifacts}
    if suffixes & EDITABLE_WITH_PREVIEW and not suffixes & {".pdf", ".png"} and not partial:
        problems.append("editable Office or diagram delivery requires its real PDF or PNG preview")
    ids = set()
    if "replay" in ledger:
        try:
            ids = verify_replay(ledger["replay"], work, args.case.resolve().parent, output, artifacts, result)
        except (OSError, KeyError, TypeError, ValueError) as error:
            problems.append(f"edit replay: {error}")
    elif scope.get("require_replay") is True and not partial:
        problems.append("same-format scope requires an edit replay")
    assessment = {
        "status": "issues" if problems else ("partial" if partial else "checked"),
        "issues": problems, "contract": contract, "delivered": done, "blocked": blocked,
        "replayed_edits": sorted(ids), "result_sha256": sha(draft),
    }
    ensure_separate([args.case, args.draft, args.intake, args.scope, args.ledger], [args.report, output / "result.json"])
    save_json(args.report, assessment)
    if not problems:
        atomic_write(output / "result.json", draft)
    return assessment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("case", "intake", "scope", "ledger", "draft", "output", "report"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    report = verify(parser.parse_args())
    print(json.dumps({"status": report["status"], "issues": report["issues"], "delivered": report["delivered"]}, ensure_ascii=False))
    return 1 if report["issues"] else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({"status": "execution_failed", "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
