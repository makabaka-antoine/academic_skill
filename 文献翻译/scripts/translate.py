"""Prepare, plan and check an Agent-assisted literature translation."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile
from time import monotonic

from protection import check_response, compile_unit, digest, issue_for, protected_spans, read_glossary, sentence_spans, term_patterns
from sources import SourceProblem, read_source


VERSION = "1.0.0"
MAX_UNIT_CHARS = 6000
RUN_OUTPUTS = {"source_map.json", "plan.json", "glossary.json", "alignment.jsonl", "bilingual.md", "result.json", "last_error.json"}
SOURCE_GAPS = {"visual_unreadable", "required_material_missing", "layout_uncertain", "source_conflict"}


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key!r}.")
        result[key] = value
    return result


def invalid_constant(value: str):
    raise ValueError(f"Invalid JSON value: {value}.")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object, parse_constant=invalid_constant)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False)
    staging = Path(handle.name)
    try:
        with handle:
            handle.write(content)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def write_json(path: Path, value) -> None:
    write_text(path, json_text(value))


def split_long_unit(unit: dict) -> list[dict]:
    text = unit["text"]
    if not unit["translate"] or len(text) <= MAX_UNIT_CHARS:
        return [unit]
    sentences = sentence_spans(unit["id"], text, protected_spans(text))
    cuts = [0]
    for sentence in sentences[1:]:
        if sentence["start"] > cuts[-1] and sentence["end"] - cuts[-1] > MAX_UNIT_CHARS:
            cuts.append(sentence["start"])
    cuts.append(len(text))
    if len(cuts) == 2:
        return [unit]
    return [
        {**unit, "id": f"{unit['id']}-part{index:03d}", "parent_unit_id": unit["id"], "text": text[start:end],
         "location": {**unit["location"], "char_start_in_block": start, "char_end_in_block": end}}
        for index, (start, end) in enumerate(zip(cuts, cuts[1:]), 1)
    ]


def transcriptions(data: bytes | None, source: dict) -> list[dict]:
    if data is None:
        return []
    if not source["pages"] or source["extractor"].startswith("pypdf"):
        raise ValueError("Transcription evidence requires explicitly page-marked text.")
    records = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_object, parse_constant=invalid_constant)
    required = {"page", "before", "after", "evidence"}
    if not isinstance(records, list):
        raise ValueError("Transcription evidence must be an array.")
    for record in records:
        if not isinstance(record, dict) or set(record) != required or type(record["page"]) is not int:
            raise ValueError("Each correction needs page, before, after and evidence.")
        if not all(isinstance(record[field], str) and record[field].strip() for field in ("before", "after", "evidence")):
            raise ValueError("Provide actual transcription excerpts and an original-page locator.")
        found = any(record["after"] in unit["text"] for unit in source["units"] if unit["location"]["page"] == record["page"])
        if not found:
            raise ValueError(f"Corrected text was not found on in-scope page {record['page']}.")
    return records


def prepare(args) -> tuple[dict, int]:
    run_dir, path = args.run_dir, args.source.resolve()
    if path.parent == run_dir.resolve() and path.name.lower() in RUN_OUTPUTS:
        raise ValueError("Choose a run directory that does not share an output name with the source.")
    try:
        data = path.read_bytes()
    except FileNotFoundError as error:
        raise SourceProblem("material_missing", f"Source material is missing: {path.name}") from error
    corrections = args.transcriptions.read_bytes() if args.transcriptions else None
    identity = {
        "case_id": args.case_id or path.name, "filename": path.name, "sha256": sha256(data).hexdigest(),
        "scope": {"pages": args.pages, "section": args.section, "page_markers": args.page_markers},
        "transcriptions_sha256": sha256(corrections).hexdigest() if corrections is not None else None,
    }
    source_path = run_dir / "source_map.json"
    if source_path.exists():
        source = read_json(source_path)
        if source["input"] != identity or source["version"] != VERSION:
            raise ValueError("This run belongs to a different source, scope or version. Use a new run directory.")
    else:
        source = read_source(path, data, args.pages, args.section, args.page_markers)
        source["transcriptions"] = transcriptions(corrections, source)
        source["units"] = [part for unit in source["units"] for part in split_long_unit(unit)]
        source.update(input=identity, version=VERSION)
        write_json(source_path, source)
    report = {"status": "prepared", "input": identity, "source_map": "source_map.json",
              "unit_count": len(source["units"]), "pages": source["pages"], "extractor": source["extractor"]}
    if source["units"]:
        return report, 0
    report.update(
        status="blocked", reason="source_unreadable", main_result=None,
        source_evidence={"source_map": "source_map.json", "pages": source["pages"]}, uncertainties=[],
        issues=[{"code": page["status"], "location": {"page": page["page"]}} for page in source["pages"]],
    )
    write_json(run_dir / "result.json", report)
    return report, 1


def adjacent_context(units: list[dict], index: int) -> list[dict]:
    context = []
    for offset, side in ((-1, "previous"), (1, "next")):
        neighbor_index = index + offset
        if 0 <= neighbor_index < len(units):
            neighbor = units[neighbor_index]
            if "column" not in neighbor["location"] and neighbor["kind"] != "code":
                excerpt = neighbor["text"][-400:] if offset < 0 else neighbor["text"][:400]
                context.append({"side": side, "unit_id": neighbor["id"], "text": excerpt})
    return context


def write_batch(run_dir: Path, number: int, units: list[dict]) -> dict:
    path = f"requests/chunk-{number:04d}.json"
    fields = ("id", "input_sha256", "location", "kind", "section", "context", "sentences", "registry")
    write_json(run_dir / path, {"source_language": "en", "target_language": "zh-CN",
                              "units": [{key: unit[key] for key in fields} for unit in units]})
    return {"request_file": path, "unit_ids": [unit["id"] for unit in units]}


def plan(args) -> tuple[dict, int]:
    run_dir = args.run_dir
    source = read_json(run_dir / "source_map.json")
    if source["version"] != VERSION:
        raise ValueError("Source map version differs from this skill. Use a new run directory.")
    if not source["units"]:
        return read_json(run_dir / "result.json"), 1
    glossary_path = run_dir / "glossary.json"
    glossary_file = args.glossary or (glossary_path if glossary_path.exists() else None)
    glossary = read_glossary(read_json(glossary_file) if glossary_file else None)
    if args.chunk_chars < 1:
        raise ValueError("--chunk-chars must be positive.")
    keep = set(args.keep_unit)
    if keep - {unit["id"] for unit in source["units"]}:
        raise ValueError("--keep-unit must refer to selected source unit IDs.")
    rules = term_patterns(glossary["terms"])
    exceptions = {(entry["term_id"], entry["sentence_id"], entry["occurrence"]): entry for entry in glossary["exceptions"]}
    consumed = set()
    units = []
    for index, original in enumerate(source["units"], 1):
        unit = {**original, "index": index}
        if unit["id"] in keep or not unit["translate"]:
            unit["retention"] = "user_preserve" if unit["id"] in keep else "source_literal"
        else:
            unit.update(compile_unit(unit, rules, exceptions, consumed))
            unit["context"] = adjacent_context(source["units"], index - 1)
        unit["input_sha256"] = digest({"version": VERSION, "language": "zh-CN", "unit": unit})
        units.append(unit)
    if set(exceptions) - consumed:
        raise ValueError(f"Exceptions with no in-scope occurrence: {sorted(set(exceptions) - consumed)}")
    batches, current = [], []
    current_length = 0
    for unit in units:
        if "retention" in unit:
            continue
        length = len(unit["text"])
        if current and current_length + length > args.chunk_chars:
            batches.append(write_batch(run_dir, len(batches) + 1, current))
            current, current_length = [], 0
        current.append(unit)
        current_length += length
    if current:
        batches.append(write_batch(run_dir, len(batches) + 1, current))
    revision = digest(glossary)
    write_json(glossary_path, {**glossary, "version": revision})
    write_json(run_dir / "plan.json", {
        "input": source["input"], "source_map_sha256": digest(source), "version": VERSION,
        "glossary_sha256": revision, "units": units, "batches": batches,
    })
    return {"status": "planned", "input": source["input"], "source_units": len(units),
            "glossary_sha256": revision, "batches": batches}, 0


def load_responses(folder: Path, known: dict) -> tuple[dict, list[dict]]:
    by_unit, issues = defaultdict(list), []
    for path in sorted(folder.glob("*.json")):
        try:
            data = read_json(path)
            if not isinstance(data, dict) or set(data) != {"units"} or not isinstance(data["units"], list):
                raise ValueError("Each response file must contain a units array.")
        except (OSError, ValueError) as error:
            issues.append({"code": "response_file_invalid", "file": path.name, "detail": str(error)})
            continue
        for entry in data["units"]:
            unit_id = entry.get("unit_id") if isinstance(entry, dict) else None
            if not isinstance(unit_id, str) or unit_id not in known:
                issues.append({"code": "unexpected_unit", "file": path.name, "unit_id": unit_id})
            elif "retention" in known[unit_id]:
                issues.append(issue_for(known[unit_id], "unexpected_retained_unit", "Preserved material is copied from the source."))
            else:
                by_unit[unit_id].append(entry)
    return by_unit, issues


def load_reviews(path: Path | None, known: dict) -> tuple[dict, list[dict]]:
    if path is None:
        return {}, []
    try:
        data = read_json(path)
        if (not isinstance(data, dict) or set(data) - {"reviews", "source_issues", "method"}
                or not isinstance(data.get("reviews"), list) or not isinstance(data.get("source_issues", []), list)):
            raise ValueError("Review file needs a reviews array and optional source_issues.")
        method = data.get("method", "model_review")
        if method not in ("model_review", "human_review", "fixture_review"):
            raise ValueError("Review method must be model_review, human_review or fixture_review.")
        rows = {}
        for entry in data["reviews"]:
            if not isinstance(entry, dict) or set(entry) != {"unit_id", "review_sha256", "findings"}:
                raise ValueError("Each review needs unit_id, review_sha256 and findings.")
            unit_id = entry["unit_id"]
            if (not isinstance(unit_id, str) or unit_id not in known or unit_id in rows or "retention" in known[unit_id]
                    or not isinstance(entry["review_sha256"], str) or not isinstance(entry["findings"], list)):
                raise ValueError("Reviews must have distinct, translatable unit IDs, fingerprints and findings arrays.")
            rows[unit_id] = {**entry, "method": method}
        gaps = data.get("source_issues", [])
        for issue in gaps:
            if (not isinstance(issue, dict) or set(issue) != {"code", "location", "detail"}
                    or not isinstance(issue["code"], str) or issue["code"] not in SOURCE_GAPS
                    or not all(isinstance(issue[key], str) and issue[key].strip() for key in ("location", "detail"))):
                raise ValueError("Each source issue needs a supported code, a readable locator and an explanation.")
        return rows, gaps
    except (OSError, ValueError) as error:
        return {}, [{"code": "review_file_invalid", "file": path.name, "detail": str(error)}]


def review_row(unit: dict, row: dict, review: dict | None) -> tuple[dict, list[dict]]:
    pending = {"status": "pending", "method": "model_review"}
    if review is None or review["review_sha256"] != row["review_sha256"]:
        return pending, []
    errors, findings = [], []
    source = {sentence["id"]: sentence["text"] for sentence in unit["sentences"]}
    keys = {"type", "severity", "source_sentence_ids", "source_excerpt", "target_excerpt", "detail"}
    for entry in review["findings"]:
        if not isinstance(entry, dict) or set(entry) != keys:
            errors.append(issue_for(unit, "review_finding_invalid", "Provide the finding's severity, source and target evidence."))
            continue
        ids = entry["source_sentence_ids"]
        valid_ids = isinstance(ids, list) and ids and all(isinstance(value, str) and value in source for value in ids)
        valid_text = all(isinstance(entry[key], str) for key in ("type", "detail", "source_excerpt", "target_excerpt"))
        if (not valid_ids or not valid_text or entry["severity"] not in ("major", "minor")
                or not entry["type"].strip() or not entry["detail"].strip() or not entry["source_excerpt"].strip()
                or entry["source_excerpt"] not in "".join(source[value] for value in ids)
                or entry["target_excerpt"] not in row["target"]):
            errors.append(issue_for(unit, "review_finding_invalid", "Finding excerpts cannot be located in the supplied sentences."))
            continue
        findings.append({**entry, "unit_id": unit["id"], "location": unit["location"]})
    if errors:
        return pending, errors
    return {"status": "recorded", "method": review["method"], "findings": findings}, []


def alignment_row(unit: dict, responses: list[dict], owners: dict, review: dict | None) -> dict:
    row = {
        "unit_id": unit["id"], "block_id": unit["block_id"], "location": unit["location"], "kind": unit["kind"],
        "section": unit["section"], "source": unit["text"], "input_sha256": unit["input_sha256"],
        "source_sentences": [{key: value for key, value in sentence.items() if key != "masked"}
                             for sentence in unit.get("sentences", [])],
        "target": None, "target_sentences": [], "machine_issues": [], "signals": [],
        "registry": unit.get("registry", []), "semantic_review": {"status": "not_run"},
    }
    if "retention" in unit:
        row.update(status="preserved", target=unit["text"], retention=unit["retention"])
        return row
    problem = None
    if not responses:
        problem = ("missing_unit", "No translation was supplied for this source unit.")
    elif len(responses) > 1:
        problem = ("duplicate_unit", "Multiple responses were supplied for the same source unit.")
    elif responses[0].get("input_sha256") != unit["input_sha256"]:
        problem = ("stale_unit", "The source, adjacent context or applicable terminology changed.")
    if problem:
        row.update(status="needs_repair", machine_issues=[issue_for(unit, *problem)])
        return row
    checked = check_response(unit, responses[0], owners)
    row.update(machine_issues=checked["errors"], signals=checked["signals"], registry=checked["registry"])
    if checked["errors"]:
        row["status"] = "needs_repair"
        return row
    target = checked["sentences"]
    row.update(status="translated", target="\n".join(sentence["text"] for sentence in target), target_sentences=target,
               review_sha256=digest({"input": unit["input_sha256"], "target": target}))
    row["semantic_review"], errors = review_row(unit, row, review)
    row["machine_issues"].extend(errors)
    if any(entry["severity"] == "major" for entry in row["semantic_review"].get("findings", [])):
        row["status"] = "review_flagged"
    return row


def location_text(location: dict) -> str:
    if location["page"] is None:
        text = f"原文第 {location['line_start']}–{location['line_end']} 行"
    else:
        name = "PDF 物理页" if location["page_origin"] == "pdf_physical" else "供稿页锚点"
        text = f"{name} {location['page']} · 块 {location['block']}"
    if "column" in location:
        text += f" · 列 {location['column']}"
    return text


def bilingual(rows: list[dict], status: str, pages: list[dict]) -> str:
    lines = ["# 双语对照", "", f"运行状态 **{status}**", ""]
    for row in rows:
        lines.extend([f"## {row['unit_id']}", "", location_text(row["location"]), "", "**原文**", ""])
        lines.extend(f"> {line}" for line in row["source"].splitlines())
        lines.extend(["", "**译文**", ""])
        if row["status"] == "preserved":
            lines.append("按原文保留，见上。")
        elif row["target"] is not None:
            lines.append(row["target"])
            if row["status"] == "review_flagged":
                lines.extend(["", "**待返修** 语义复审发现实质问题，见对齐记录。"])
            elif row["semantic_review"]["status"] == "pending":
                lines.extend(["", "**待复审** 该段已完成保护项检查。"])
        else:
            codes = ", ".join(sorted({item["code"] for item in row["machine_issues"]}))
            lines.append(f"该段尚未通过组装检查（{codes}）。")
        lines.append("")
    if pages:
        lines.extend(["## 页面覆盖", ""])
        lines.extend(f"- 页 {page['page']} · {page['status']}" for page in pages)
        lines.append("")
    return "\n".join(lines)


def check(args) -> tuple[dict, int]:
    started = monotonic()
    run_dir = args.run_dir
    source = read_json(run_dir / "source_map.json")
    planned = read_json(run_dir / "plan.json")
    if digest(source) != planned["source_map_sha256"] or planned["version"] != VERSION:
        raise ValueError("Plan differs from the frozen source or installed workflow. Run plan again.")
    glossary = read_glossary(read_json(run_dir / "glossary.json"))
    if digest(glossary) != planned["glossary_sha256"]:
        raise ValueError("Glossary changed after planning. Run plan again before checking.")
    known = {unit["id"]: unit for unit in planned["units"]}
    if list(known) != [unit["id"] for unit in source["units"]] or len(known) != len(planned["units"]):
        raise ValueError("Plan unit IDs differ from the source map.")
    for original, unit in zip(source["units"], planned["units"]):
        if any(unit.get(key) != value for key, value in original.items()):
            raise ValueError(f"Plan unit {unit['id']} differs from the frozen source.")
        payload = {key: value for key, value in unit.items() if key != "input_sha256"}
        if unit["input_sha256"] != digest({"version": VERSION, "language": "zh-CN", "unit": payload}):
            raise ValueError(f"Plan fingerprint differs for {unit['id']}. Regenerate the plan.")
    by_unit, issues = load_responses(args.responses or (run_dir / "responses"), known)
    default_reviews = run_dir / "reviews.json"
    reviews, review_issues = load_reviews(args.reviews or (default_reviews if default_reviews.exists() else None), known)
    issues.extend(review_issues)
    owners = {item["token"]: unit["id"] for unit in known.values() for item in unit.get("registry", [])}
    rows = [alignment_row(unit, by_unit[unit_id], owners, reviews.get(unit_id)) for unit_id, unit in known.items()]
    issues.extend(item for row in rows for item in row["machine_issues"])
    issues.extend({"code": page["status"], "location": {"page": page["page"]}, "detail": page.get("reason")}
                  for page in source["pages"] if page["status"] != "readable")
    pending = [row["unit_id"] for row in rows if row["status"] not in {"translated", "preserved"}]
    review_pending = [row["unit_id"] for row in rows if row["semantic_review"]["status"] == "pending"]
    counts = Counter(row["status"] for row in rows)
    status = "blocked" if not rows else ("partial" if pending or review_pending or issues else "success")
    terms = [entry for row in rows for entry in row["registry"] if entry["kind"] == "TERM"]
    protected = [entry for row in rows for entry in row["registry"] if entry["kind"] != "TERM"]
    report = {
        "version": VERSION, "input": source["input"], "status": status, "extractor": source["extractor"],
        "main_result": {"bilingual": "bilingual.md", "alignment": "alignment.jsonl", "glossary": "glossary.json"},
        "source_evidence": {"source_map": "source_map.json", "pages": source["pages"], "transcriptions": source["transcriptions"]},
        "glossary_sha256": planned["glossary_sha256"],
        "coverage": {
            "requested_pages": len(source["pages"]) if source["pages"] else None,
            "pages_with_text": sum(page["status"] in {"readable", "layout_uncertain"} for page in source["pages"]) if source["pages"] else None,
            "extracted_units": len(rows), "required_translations": len(rows) - counts["preserved"],
            "translated_units": counts["translated"], "preserved_units": counts["preserved"],
            "reviews_recorded": dict(Counter(row["semantic_review"]["method"] for row in rows
                                            if row["semantic_review"]["status"] == "recorded")),
        },
        "term_occurrences": {"registered": len(terms), "restored": sum(entry["recovery_status"] == "restored" for entry in terms),
                             "listed_exceptions": sum("exception" in entry for entry in terms)},
        "protected_occurrences": {"registered": len(protected),
                                  "restored": sum(entry["recovery_status"] == "restored" for entry in protected)},
        "numeric_bindings": dict(Counter(entry.get("binding_status", "pending") for entry in protected if "expected_group" in entry)),
        "numeric_occurrences_without_machine_binding": sum(entry["kind"] == "NUM" and "expected_group" not in entry for entry in protected),
        "units_to_repair": pending, "units_awaiting_review": review_pending,
        "issues": issues,
        "uncertainties": [signal for row in rows for signal in row["signals"]]
            + [finding for row in rows for finding in row["semantic_review"].get("findings", [])],
        "check_seconds": round(monotonic() - started, 3),
    }
    write_text(run_dir / "alignment.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    write_text(run_dir / "bilingual.md", bilingual(rows, status, source["pages"]))
    write_json(run_dir / "result.json", report)
    return report, 0 if status == "success" else 1


def failure_report(args, status: str, reason: str, detail: str) -> dict:
    source_file = args.run_dir / "source_map.json"
    source = {}
    if source_file.exists() and args.command != "prepare":
        try:
            saved = read_json(source_file)
            if isinstance(saved, dict):
                source = saved
        except (OSError, ValueError):
            pass
    if args.command == "prepare":
        identity = {"case_id": args.case_id or args.source.name, "filename": args.source.name,
                    "scope": {"pages": args.pages, "section": args.section}}
    else:
        identity = source.get("input", {"case_id": args.run_dir.name})
    report = {
        "input": identity, "status": status, "reason": reason, "detail": detail, "main_result": None,
        "source_evidence": {"source_map": source_file.name, "pages": source.get("pages", [])} if source else {},
        "issues": [{"code": reason, "detail": detail}], "uncertainties": [],
    }
    path = args.run_dir / "result.json"
    if path.exists():
        path = args.run_dir / "last_error.json"
    try:
        write_json(path, {**report, "report_file": path.name})
    except OSError:
        return report
    return {**report, "report_file": path.name}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="command", required=True)
    prepare_parser = modes.add_parser("prepare", help="Freeze source units and locations.")
    prepare_parser.add_argument("--source", type=Path, required=True)
    prepare_parser.add_argument("--case-id")
    prepare_parser.add_argument("--pages")
    prepare_parser.add_argument("--section")
    prepare_parser.add_argument("--page-markers", action="store_true")
    prepare_parser.add_argument("--transcriptions", type=Path)
    plan_parser = modes.add_parser("plan", help="Register protected occurrences and write translation requests.")
    plan_parser.add_argument("--glossary", type=Path)
    plan_parser.add_argument("--chunk-chars", type=int, default=5000)
    plan_parser.add_argument("--keep-unit", action="append", default=[])
    check_parser = modes.add_parser("check", help="Check responses and write the case deliverables.")
    check_parser.add_argument("--responses", type=Path)
    check_parser.add_argument("--reviews", type=Path)
    for mode, handler in ((prepare_parser, prepare), (plan_parser, plan), (check_parser, check)):
        mode.add_argument("--run-dir", type=Path, required=True)
        mode.set_defaults(handler=handler)
    args = parser.parse_args(argv)
    try:
        report, code = args.handler(args)
    except SourceProblem as error:
        status = "failed" if error.code == "pdf_dependency_missing" else "blocked"
        report = failure_report(args, status, error.code, str(error))
        code = 2 if status == "failed" else 1
    except (OSError, ValueError, TypeError, KeyError) as error:
        report, code = failure_report(args, "failed", "execution_or_contract_error", str(error)), 2
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json_text(report), end="")
    return code


if __name__ == "__main__":
    sys.exit(main())
