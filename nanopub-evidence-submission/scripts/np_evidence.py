#!/usr/bin/env python3
"""Prepare, inspect, assemble and verify a paper's evidence-bound claim bundle."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True

from assess import audit
from core import Evidence, InputError, VERSION, atomic_bytes, fingerprint, json_bytes, read_json, words
from output import contract_fields, verify_bundle, write_bundle
from sources import prepare, render_page, verify_source_files

STATE = "snapshot.json"
RESULT = re.compile(r"\b(?:result|conclusion|discussion|finding|evaluation|abstract|experiment|analysis)\w*\b|"
                    r"结果|结论|讨论|摘要|实验|分析", re.I)
SIGNAL = re.compile(r"\b(?:increas\w*|decreas\w*|achiev\w*|improv\w*|outperform\w*|"
                    r"observ\w*|associat\w*|higher|lower|significan\w*|suggest\w*|"
                    r"report\w*|limit\w*|we found|we show)\b|提高|提升|降低|增加|减少|"
                    r"未发现|优于|高于|低于|无显著|相关|结果表明|可能|局限", re.I)
INPUT_CASE = Path("/input/case.json")


def tell(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


def load(work):
    target = Path(work) / STATE
    if not target.is_file():
        raise InputError("missing_snapshot", "Run prepare before reading or building the bundle.")
    return read_json(target)


def summary(state):
    return {
        "case_id": state["case_id"], "snapshot_id": state["snapshot_id"],
        "sources": [{**{k: s[k] for k in ("id", "name", "format", "pages", "backend", "state", "unreadable")},
                     "units": len(s["units"]), "metadata_candidates": s["metadata_candidates"]} for s in state["sources"]],
        "input_problems": state["problems"], "contract_fields": list(contract_fields(state["contract"])),
    }


def status_summary(result):
    return {"case_id": result["case_id"], "snapshot_id": result["snapshot_id"], "status": result["status"],
            "run_status": result["run_status"], "phase": result["phase"], "verification": result["verification"],
            "contract_unfilled": result["contract_unfilled"]}


def failure_state(error, args):
    path = Path(args.case) if args.case else INPUT_CASE
    case = {}
    if path.is_file():
        try:
            parsed = read_json(path)
            case = parsed if isinstance(parsed, dict) else {}
        except InputError:
            pass
    problems = [{"kind": getattr(error, "kind", "prepare_failed"), "message": str(error)[:600]}]
    return {"case_id": case.get("case_id", case.get("caseId")), "contract": case.get("output_contract"),
            "case": case, "task_file_text": "", "passage_hints": [], "_case_path": None, "_task_path": None,
            "snapshot_id": fingerprint([case.get("case_id"), problems]), "problems": problems, "sources": []}


def cmd_prepare(args):
    case_path = args.case or (INPUT_CASE if INPUT_CASE.is_file() else None)
    try:
        if not (case_path or args.task or args.input):
            raise InputError("input_missing", "Provide a case file, a task file, or an explicit material path.")
        state = prepare(case_path, args.task, args)
    except (InputError, OSError, UnicodeError, ValueError) as error:
        state = failure_state(error, args)
    atomic_bytes(Path(args.work) / STATE, json_bytes(state))
    result = write_bundle(state, audit(state, []), args.out, "prepared")
    tell({**summary(state), "status": result["status"], "result_json": str(Path(args.out) / "result.json")})
    return 1 if result["run_status"] == "failed" else 0


def cmd_outline(args):
    state = load(args.work)
    tell(summary(state))
    for source in state["sources"]:
        sections = {}
        for unit in source["units"]:
            sections.setdefault(unit["section"], []).append(unit)
        for name, units in sections.items():
            pages = sorted({u["page"] for u in units if u["page"] is not None})
            hints = sorted({u["label"] for u in units if u["label"]})
            print(f"{source['id']} | {name} | {units[0]['id']} to {units[-1]['id']} | pages {pages or '-'}"
                  + (f" | {', '.join(hints[:6])}" if hints else ""))


def cmd_contract(args):
    state = load(args.work)
    tell({"case_id": state["case_id"], "snapshot_id": state["snapshot_id"],
          "fields": contract_fields(state["contract"]), "output_contract": state["contract"],
          "task_fields": {k: state["case"][k] for k in (
              "task", "question", "instruction", "instructions", "prompt", "requirements", "scope", "constraints", "evidence_scope"
          ) if k in state["case"]},
          "task_md": state["task_file_text"], "passage_hints": state["passage_hints"]})


def selected_units(state, args):
    for source in state["sources"]:
        if args.source and args.source != source["id"]:
            continue
        for unit in source["units"]:
            if (args.page and args.page != unit["page"]) or (args.section and args.section.casefold() not in unit["section"].casefold()):
                continue
            if getattr(args, "kind", None) and unit["kind"] != args.kind:
                continue
            yield unit


def show_unit(unit, picked=None):
    place = f"p.{unit['page']}" if unit["page"] is not None else (
        "lines " + "-".join(map(str, unit["lines"])) if unit["lines"] else (unit["pointer"] or unit["extraction"])
    )
    print(f"\n{unit['id']} | {place} | {unit['section']} | {unit['kind']}")
    for j, (left, right) in enumerate(unit["spans"], 1):
        if picked is not None and abs(j - picked) > 1:
            continue
        s = unit["text"][left:right]
        print(f"  {unit['id']}.{j} @{left}:{right} {'[selected] ' if j == picked else ''}{s[:5200]}")
        if len(s) > 5200:
            print("    Display clipped. Use a smaller quote inside the anchor.")


def cmd_read(args):
    state = load(args.work)
    if args.anchors:
        index = Evidence(state["sources"])
        for anchor in args.anchors:
            _, unit, picked, _, _ = index.anchor(anchor)
            show_unit(unit, picked)
        return
    units = list(selected_units(state, args))
    for unit in units[args.skip:args.skip + args.limit]:
        show_unit(unit)
    print(f"\n{len(units)} matching units. Next --skip {args.skip + args.limit} if more are needed.")


def cmd_candidates(args):
    state = load(args.work)
    terms = set(words(" ".join(args.query or [])))
    hits = []
    for unit in selected_units(state, args):
        if not terms and unit["kind"] not in {"body", "table", "caption", "front"}:
            continue
        for number, (start, end) in enumerate(unit["spans"], 1):
            text = unit["text"][start:end]
            if terms:
                overlap = terms & set(words(text + " " + unit["section"]))
                if not overlap:
                    continue
                rank = 3 * len(overlap) / len(terms) + min(len(SIGNAL.findall(text)), 3) * 0.1
            else:
                matches = len(SIGNAL.findall(text))
                rank = (1.3 if RESULT.search(unit["section"]) else 0) + min(matches, 4) * 0.9
                rank += 0.35 if re.search(r"(?<![A-Za-z])\d+(?:\.\d+|%|\b)", text) else 0
                rank += 0.4 if unit["kind"] == "table" else 0
                if rank < 0.8 or len(text) < 14:
                    continue
            hits.append((rank, unit, number, text))
    hits.sort(key=lambda row: -row[0])
    for rank, unit, number, text in hits[:args.limit]:
        where = f"page {unit['page']}" if unit["page"] is not None else unit["kind"]
        print(f"{unit['id']}.{number} | {where} | {unit['section']} | {text[:520]}")
    print(f"\n{len(hits)} matching spans. Inspect the source around a hit before extracting.")


def cmd_page(args):
    state = load(args.work)
    verify_source_files(state)
    source = next((item for item in state["sources"] if item["id"] == args.source), None)
    if not source or source["format"] != "pdf" or not source["_path"]:
        raise InputError("pdf_source", "Select a PDF source from outline.")
    page = args.number
    if not (1 <= page <= source["pages"]):
        raise InputError("page_range", "Physical page is outside the PDF.")
    if args.render:
        atomic_bytes(args.render, render_page(Path(source["_path"]), page))
        tell({"page": page, "source_id": source["id"], "image": str(args.render)})
    if args.layout:
        tool = shutil.which("pdftotext")
        if tool is None:
            raise InputError("layout_backend", "pdftotext is unavailable. Read the indexed page or render it.")
        cmd = [tool, "-f", str(page), "-l", str(page), "-layout", "-enc", "UTF-8", source["_path"], "-"]
        layout = subprocess.run(cmd, capture_output=True, check=True, timeout=30).stdout.decode("utf-8")
        print(layout[:18000])
    if not args.render and not args.layout:
        for unit in source["units"]:
            if unit["page"] == page:
                show_unit(unit)


def cmd_build(args):
    state = load(args.work)
    try:
        verify_source_files(state)
    except InputError as error:
        invalid = {**state, "sources": [], "problems": [
            *state["problems"], {"kind": error.kind, "message": str(error)}
        ]}
        write_bundle(invalid, audit(invalid, []), args.out, "invalidated")
        raise
    evaluated = audit(state, [read_json(path) for path in args.draft])
    result = write_bundle(state, evaluated, args.out, "final" if args.final else "draft")
    report = {
        **status_summary(result),
        "claim_issues": [{"key": r["key"], "decision": r["decision"], "checks": r["checks"]} for r in evaluated[2] if r["checks"]],
        "source_issues": evaluated[1], "result_json": str(Path(args.out) / "result.json"),
    }
    tell(report)
    return {"success": 0, "partial": 2, "unable_to_determine": 2, "failed": 1}[result["run_status"]]


def cmd_verify(args):
    tell(verify_bundle(args.out))


def cmd_doctor(_):
    tell({"version": VERSION, "python": ".".join(map(str, sys.version_info[:3])),
          "modules": {name: importlib.util.find_spec(name) is not None for name in ("pymupdf", "fitz", "pypdf", "rdflib")},
          "commands": {name: shutil.which(name) is not None for name in ("pdftotext", "pdftoppm", "tesseract")}})


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    parser = sub.add_parser("prepare", help="index actual material and write the initial result")
    parser.add_argument("--work", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--case", help="original case.json; defaults to /input/case.json when present")
    parser.add_argument("--task", help="task.md path; defaults to task.md beside the case")
    parser.add_argument("--input", action="append", help="explicit source path; repeat for multiple sources")
    parser.add_argument("--ocr", choices=("auto", "off", "force"), default="auto")
    parser.add_argument("--ocr-language", default="eng", help="installed Tesseract language, e.g. chi_sim+eng")
    parser.add_argument("--ocr-limit", type=int, choices=range(0, 13), default=4)
    parser.set_defaults(action=cmd_prepare)
    for name, action in (("outline", cmd_outline), ("contract", cmd_contract), ("read", cmd_read),
                         ("candidates", cmd_candidates), ("page", cmd_page), ("build", cmd_build)):
        parser = sub.add_parser(name)
        parser.add_argument("--work", required=True)
        parser.set_defaults(action=action)
        if name in {"read", "candidates"}:
            parser.add_argument("--source")
            parser.add_argument("--page", type=int)
            parser.add_argument("--section")
            parser.add_argument("--kind")
            parser.add_argument("--limit", type=int, default=20 if name == "read" else 24)
        if name == "read":
            parser.add_argument("anchors", nargs="*")
            parser.add_argument("--skip", type=int, default=0)
        if name == "candidates":
            parser.add_argument("query", nargs="*")
        if name == "page":
            parser.add_argument("--source", required=True)
            parser.add_argument("--number", required=True, type=int)
            parser.add_argument("--render")
            parser.add_argument("--layout", action="store_true")
        if name == "build":
            parser.add_argument("--out", required=True)
            parser.add_argument("--draft", required=True, action="append")
            parser.add_argument("--final", action="store_true")
    parser = sub.add_parser("verify")
    parser.add_argument("--out", required=True)
    parser.set_defaults(action=cmd_verify)
    sub.add_parser("doctor").set_defaults(action=cmd_doctor)
    args = ap.parse_args(argv)
    try:
        return args.action(args) or 0
    except (InputError, OSError, ValueError, subprocess.SubprocessError, KeyError, IndexError, UnicodeError) as error:
        tell({"ok": False, "kind": getattr(error, "kind", "operation_failed"), "message": str(error)[:1000]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
