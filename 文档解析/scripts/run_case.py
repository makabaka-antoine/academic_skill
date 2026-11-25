"""Run a research-document case from its inputs through an auditable result."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
import case_contract as contract
import delivery_audit
from parse_pdf import VERSION as PARSER_VERSION, dependency_versions, file_hash, write_json


VERSION = "1.4.0"
HERE = Path(__file__).resolve().parent
OFFICE = {".docx", ".doc", ".odt", ".xlsx", ".xls", ".ods", ".pptx", ".ppt", ".odp"}
PREVIEW_SOURCES = OFFICE | {".svg", ".dot", ".mmd", ".mermaid", ".drawio", ".graphml"}
EXPECTED_FILES = {
    "SKILL.md", "GITHUB_URL.txt", "requirements.txt", "scripts/case_contract.py",
    "scripts/parse_pdf.py", "scripts/run_case.py",
    "scripts/delivery_audit.py", "scripts/validation_guard.py",
}
INPUT_KEYS = ("input_files", "inputs", "materials", "files", "attachments", "artifacts")
PATH_KEYS = ("path", "file_path", "relative_path", "file", "filename")
INPUT_SUFFIXES = {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".tsv",
                  ".json", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}


def json_object(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"Non-finite JSON value: {value}")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            invalid_constant(value)
        return number

    data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique,
                      parse_constant=invalid_constant, parse_float=finite_float)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def emit(data):
    print(json.dumps(data, ensure_ascii=True, allow_nan=False))


def inside(root, value, mount=None):
    if not isinstance(value, (str, Path)):
        raise ValueError("Expected a file path")
    raw = str(value)
    if not raw or "://" in raw:
        raise ValueError(f"Expected a local path: {raw}")
    if mount and raw.startswith(mount + "/"):
        raw = raw[len(mount) + 1:]
    path = (root / raw).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Path is outside {mount or root}: {value}")
    return path


def scoped_pages(spec):
    """Parse and merge 1-based physical-page intervals, including an open last interval."""
    if isinstance(spec, int) and not isinstance(spec, bool):
        spec = str(spec)
    if isinstance(spec, list):
        spec = ",".join(str(item) for item in spec)
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("Physical pages must be like 1-3,7,12-")
    result = []
    for part in spec.split(","):
        match = re.fullmatch(r"([1-9][0-9]*)(?:[–-]([1-9][0-9]*)?)?", part.strip())
        if not match:
            raise ValueError(f"Invalid physical-page interval: {part}")
        first, last = int(match[1]), int(match[2]) if match[2] else (
            float("inf") if part.strip().endswith(("-", "–")) else int(match[1]))
        if last < first:
            raise ValueError(f"Invalid physical-page interval: {part}")
        result.append((first, last))
    merged = []
    for first, last in sorted(result):
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def page_spec(intervals):
    def item(first, last):
        if first == last:
            return str(first)
        return f"{first}-" if last == float("inf") else f"{first}-{last}"
    return ",".join(item(first, last) for first, last in intervals)


def file_entries(item, alias=None, depth=0):
    """Use declared local paths only. Labels and role names supply navigation."""
    if depth > 5:
        return
    if isinstance(item, str):
        if Path(item).suffix.lower() in INPUT_SUFFIXES:
            yield item, alias, {}
    elif isinstance(item, list):
        for entry in item:
            yield from file_entries(entry, None, depth + 1)
    elif isinstance(item, dict):
        path = next((item[name] for name in PATH_KEYS if isinstance(item.get(name), str)), None)
        if path:
            yield path, item.get("id", alias), item
        else:
            for name, value in item.items():
                if isinstance(value, (dict, list, str)):
                    yield from file_entries(value, name, depth + 1)


def case_sources(spec, root, extra_files, task_file):
    entries = []
    containers = [spec.get("input", {})]
    containers.extend(spec[name] for name in INPUT_KEYS if name in spec)
    containers.extend(spec[name] for name in ("pdf", "pdf_path", "paper", "document")
                      if name in spec)
    for item in containers + [extra_files]:
        entries.extend(file_entries(item))
    sources = {}
    for name, alias, info in entries:
        filepath = inside(root, name, "/input")
        if filepath == task_file:
            continue
        relative = filepath.relative_to(root).as_posix()
        scope = info.get("allowed_physical_pages", info.get("physical_pages"))
        bound = page_spec(scoped_pages(scope)) if scope is not None else None
        record = sources.setdefault(relative, {"path": relative, "exists": filepath.is_file()})
        if alias is not None and isinstance(alias, (str, int)):
            record.setdefault("ids", [])
            if str(alias) not in record["ids"]:
                record["ids"].append(str(alias))
        if isinstance(info.get("role"), str):
            record["role"] = info["role"]
        if bound is not None:
            if "allowed_physical_pages" in record and bound != record["allowed_physical_pages"]:
                raise ValueError(f"Conflicting physical-page ranges for {relative}")
            record["allowed_physical_pages"] = bound
        if filepath.suffix.lower() == ".pdf":
            record["kind"] = "pdf"
        elif filepath.is_file():
            record["sha256"] = file_hash(filepath)

    declared_scope = spec.get("allowed_physical_pages")
    if isinstance(declared_scope, dict):
        for name, bound in declared_scope.items():
            record = select_source(list(sources.values()), name)
            narrow = scoped_pages(bound)
            if "allowed_physical_pages" in record:
                require_subset(narrow, scoped_pages(record["allowed_physical_pages"]))
            record["allowed_physical_pages"] = page_spec(narrow)
    elif declared_scope is not None:
        pdfs = [source for source in sources.values() if source.get("kind") == "pdf"]
        if len(pdfs) != 1:
            raise ValueError("A case-level physical-page scope must identify its PDF")
        narrow = scoped_pages(declared_scope)
        if "allowed_physical_pages" in pdfs[0]:
            require_subset(narrow, scoped_pages(pdfs[0]["allowed_physical_pages"]))
        pdfs[0]["allowed_physical_pages"] = page_spec(narrow)
    return list(sources.values())


def require_subset(selected, allowed):
    for first, last in selected:
        if not any(lower <= first and last <= upper for lower, upper in allowed):
            raise ValueError(f"Physical-page selection exceeds the case scope: {page_spec(selected)}")


def select_source(sources, name):
    if name.startswith("/input/"):
        name = name[len("/input/"):]
    exact = [entry for entry in sources if entry["path"] == name]
    if exact:
        return exact[0]
    matches = [entry for entry in sources if name in entry.get("ids", [])]
    if len(matches) != 1:
        raise ValueError(f"Source must be a declared, unambiguous case path or ID: {name}")
    return matches[0]


def case_state(args):
    ctx = json_object(args.work / "case_context.json")
    if args.case != Path(ctx["case_json"]) or args.output != Path(ctx["output_dir"]):
        raise ValueError("Use the same case and output directory as prepare")
    if ctx["case_hash"] != file_hash(args.case) or ctx["task_hash"] != file_hash(Path(ctx["task_file"])):
        raise ValueError("Case or task bytes changed. Use an isolated work and output directory.")
    return ctx


def audit_destination(args, ctx=None):
    location = (ctx or {}).get("audit_location", getattr(args, "audit_location", None) or "output")
    return args.work / "delivery_audit.json" if location == "work" else args.output / "audit.json"


def pending_result(args, case_id, schema, reason, kind="indeterminate", ctx=None):
    audit = delivery_audit.incomplete(case_id, reason, kind)
    data = contract.incomplete_result(case_id, schema, reason, kind)
    destination = audit_destination(args, ctx)
    if destination.parent == args.work:
        if contract.allows_field(schema, "audit"):
            data["audit"] = audit
        else:
            for field in delivery_audit.FIELDS[1:]:
                if contract.allows_field(schema, field):
                    data[field] = audit[field]
    write_json(destination, audit)
    return data


def diagnostic(args, case_id, schema, reason, kind="indeterminate", ctx=None):
    path = args.output / "result.json"
    if not path.exists():
        write_json(path, pending_result(args, case_id, schema, reason, kind, ctx))
    return path


def prepare(args):
    case_id, schema = None, None
    context_file = args.work / "case_context.json"
    task_loaded = False
    start = time.time()
    try:
        case = json_object(args.case)
        case_id = case.get("case_id")
        if not ((isinstance(case_id, str) and case_id.strip()) or type(case_id) is int):
            case_id = None
            raise ValueError("case.json has no usable case_id")
        raw_contract = case.get("output_contract")
        schema = contract.with_case_id(contract.result_schema(raw_contract))
        if raw_contract is None or raw_contract == "":
            raise ValueError("case.json has no output_contract")
        task_ref = case.get("task_file", case.get("task_path", case.get("task_md", case.get("task"))))
        if isinstance(task_ref, dict):
            task_ref = next((task_ref[k] for k in PATH_KEYS if isinstance(task_ref.get(k), str)), None)
        if isinstance(task_ref, str) and not task_ref.lower().endswith(".md"):
            task_ref = None
        task = inside(args.case.parent, task_ref or "task.md", "/input")
        task_text = task.read_text(encoding="utf-8-sig")
        if not task_text.strip():
            raise ValueError("Task Markdown is empty")
        task_loaded = True
        sources = case_sources(case, args.case.parent, args.file, task)
        case_hash, task_hash = file_hash(args.case), file_hash(task)
        if context_file.exists():
            old = case_state(args)
            if old["case_hash"] != case_hash or old["task_hash"] != task_hash:
                raise ValueError("Case or task bytes changed. Use separate work and output directories.")
            # Inferred paths explicitly supplied by the agent survive repeated prepare.
            known = {item["path"] for item in sources}
            fresh = {item["path"]: item for item in sources}
            for item in old["sources"]:
                if (item.get("sha256") and item["path"] in fresh
                        and item["sha256"] != fresh[item["path"]].get("sha256")):
                    raise ValueError(f"Input attachment changed after prepare: {item['path']}")
            sources.extend(item for item in old["sources"] if item["path"] not in known)
            start, end = old["start_epoch"], old["deadline_epoch"]
        else:
            old = {}
            end = start + args.budget_seconds
        audit_location = old.get("audit_location", args.audit_location or "output")
        if args.audit_location and args.audit_location != audit_location:
            raise ValueError("Audit location is frozen by prepare; keep using the same case context")
        result_file = args.output / "result.json"
        needs_checkpoint = not result_file.exists()
        if result_file.exists():
            prior = json_object(result_file)
            needs_checkpoint = (prior.get("case_id") is None and not context_file.exists()
                                and contract.status_kind(prior.get("status")) == "indeterminate")
            if not needs_checkpoint and (type(prior.get("case_id")) is not type(case_id)
                                         or prior.get("case_id") != case_id):
                raise ValueError("Output directory already contains a result for another or unknown case")
        ctx = {
            "case_id": case_id, "case_json": str(args.case), "case_hash": case_hash,
            "task_file": str(task), "task_hash": task_hash, "output_dir": str(args.output),
            "schema": schema, "required_artifacts": contract.required_artifacts(raw_contract),
            "sources": sources, "start_epoch": start, "deadline_epoch": end,
            "versions": {"runner": VERSION, "parser": PARSER_VERSION},
            "max_repairs": old.get("max_repairs", args.max_repairs),
            "parse_attempts": old.get("parse_attempts", []),
            "audit_location": audit_location,
        }
        if "model_usage" in old:
            ctx["model_usage"] = old["model_usage"]
        if needs_checkpoint:
            pending = "Case opened. Evidence analysis and contract review are pending."
            write_json(result_file, pending_result(args, case_id, schema, pending, ctx=ctx))
        write_json(context_file, ctx)
        emit({"status": "ready", "case_id": case_id, "context": str(context_file),
              "task": str(task), "sources": sources, "structured_contract": schema is not None,
              "deadline_epoch": end, "seconds_left": round(max(0, end - time.time()), 1)})
        return 0
    except (OSError, ValueError, TypeError, AttributeError) as err:
        reason = f"Case preparation failed: {err}"
        result_file = diagnostic(args, case_id, schema, reason)
        emit({"status": "indeterminate", "case_id": case_id, "task_read": task_loaded,
              "result": str(result_file), "reason": reason})
        return 2


def available_time(ctx, reserve=True):
    span = ctx["deadline_epoch"] - ctx["start_epoch"]
    buffer = min(90, span / 5) if reserve else 0
    return ctx["deadline_epoch"] - time.time() - buffer


def check_model_budget(ctx, for_new_work=False):
    usage = ctx.get("model_usage", {})
    calls, tokens = usage.get("requests"), usage.get("max_response_tokens")
    if calls is not None and calls > 64 or tokens is not None and tokens > 16384:
        raise ValueError("Measured model usage exceeds the case budget; preserve a documented partial result")
    if for_new_work and calls is not None and calls >= 56:
        raise ValueError("Eight-request reserve reached. Verify and finalize existing evidence.")


def budget(args):
    ctx = case_state(args)
    old = ctx.get("model_usage", {})
    if args.model_requests is not None or args.max_response_tokens is not None:
        if not args.meter_source:
            raise ValueError("Name the real harness/log source; never estimate measured usage")
        usage = dict(old)
        for name, value in (("requests", args.model_requests), ("max_response_tokens", args.max_response_tokens)):
            if value is not None:
                if value < 0 or value < old.get(name, 0):
                    raise ValueError("Actual usage counters cannot be negative or reset")
                usage[name] = value
        usage["source"] = args.meter_source
        ctx["model_usage"] = usage
        write_json(args.work / "case_context.json", ctx)
    usage = ctx.get("model_usage", {"requests": None, "max_response_tokens": None, "source": None})
    over = ((usage.get("requests") or 0) > 64 or (usage.get("max_response_tokens") or 0) > 16384)
    emit({"seconds_left": round(max(0, available_time(ctx, False)), 1), "model_usage": usage,
          "status": "over_budget" if over else "measured" if usage.get("source") else "model_meter_unavailable",
          "stop_new_work": over or available_time(ctx) <= 0 or (usage.get("requests") or 0) >= 56})
    return 1 if over else 0


def parse_source(args):
    ctx = case_state(args)
    check_model_budget(ctx, for_new_work=True)
    record = select_source(ctx["sources"], args.source)
    source = inside(args.case.parent, record["path"], "/input")
    if source.suffix.lower() != ".pdf" or not source.is_file():
        raise ValueError(f"Declared PDF is unavailable: {record['path']}")
    pages = scoped_pages(args.pages) if args.pages else None
    allowed = record.get("allowed_physical_pages")
    if allowed:
        bounds = scoped_pages(allowed)
        if args.all_pages:
            require_subset([(1, float("inf"))], bounds)
        if pages:
            require_subset(pages, bounds)
        else:
            pages = bounds
    elif not (pages or args.all_pages):
        raise ValueError("Use --pages or --all-pages after reading the task's allowed scope")
    if args.lang and not re.fullmatch(r"[a-zA-Z0-9_]+(?:\+[a-zA-Z0-9_]+)*", args.lang):
        raise ValueError("Use installed Tesseract language codes, such as eng+chi_sim")

    layout = inside(args.work, args.layout, "/app/work") if args.layout else None
    options = {
        "pages": page_spec(pages) if pages else None, "lang": args.lang, "ocr": args.ocr,
        "render": args.render, "dpi": args.dpi, "ocr_timeout": args.ocr_timeout,
        "layout": file_hash(layout) if layout else None, "table_regions": args.table_region,
        "ocr_regions": args.ocr_region,
    }
    fingerprint = {
        "file": record["path"], "sha256": file_hash(source), "options": options,
        "version": PARSER_VERSION, "script": file_hash(HERE / "parse_pdf.py"),
        "dependencies": dependency_versions(),
        "tesseract": shutil.which("tesseract"), "tessdata": os.getenv("TESSDATA_PREFIX"),
    }
    key = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:24]
    parent = args.work / "evidence" / key
    parent.mkdir(parents=True, exist_ok=True)
    if not (args.password_env or args.refresh):
        for manifest in sorted(parent.glob("*/manifest.json"), reverse=True):
            previous = json_object(manifest)
            if ("elapsed_seconds" in previous and previous.get("status") in {"extracted", "review_required"} and
                    (manifest.parent / "document.md").is_file() and
                    all((manifest.parent / page["data"]).is_file() for page in previous["pages"])):
                if args.lang and args.ocr == "auto" and any(
                    page.get("ocr_status") in {"pending", "failed", "pending_or_unreadable", "partial",
                                                "render_unavailable"}
                    for page in previous["pages"]):
                    continue
                emit_parse(ctx, record, manifest, previous, True, False)
                return 0
    seconds = available_time(ctx)
    if seconds < 2:
        raise ValueError("Parsing window ended. Finalize available evidence or record the time limit.")
    selected_intervals = pages or [(1, float("inf"))]
    attempts = ctx.setdefault("parse_attempts", [])
    repair = any(previous["source"] == record["path"] and any(
        max(a, c) <= min(b, d) for a, b in selected_intervals
        for c, d in scoped_pages(previous["pages"])) for previous in attempts)
    if repair and sum(item["repair"] for item in attempts) >= ctx.get("max_repairs", 2):
        raise ValueError("Frozen repair budget exhausted. Preserve available evidence and document remaining gaps.")
    # Count attempts before launching: failed/timed-out runs also consume the same repair budget.
    attempts.append({"source": record["path"], "key": key, "pages": page_spec(selected_intervals), "repair": repair})
    write_json(args.work / "case_context.json", ctx)
    out = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    command = [sys.executable, str(HERE / "parse_pdf.py"), str(source), "--out", str(out),
               "--dpi", str(args.dpi), "--ocr", args.ocr, "--render", args.render,
               "--ocr-timeout", str(args.ocr_timeout), "--time-budget", f"{seconds - 1:.2f}"]
    for option, value in (("--pages", options["pages"]), ("--lang", args.lang), ("--layout", layout),
                          ("--password-env", args.password_env)):
        if value:
            command.extend([option, str(value)])
    for option, values in (("--table-region", args.table_region), ("--ocr-region", args.ocr_region)):
        for value in values:
            command.extend([option, value])
    timed_out, returncode, error = False, 2, None
    try:
        proc = subprocess.run(command, cwd=args.work, encoding="utf-8", errors="replace",
                              capture_output=True, timeout=seconds, check=False)
        returncode = proc.returncode
        error = proc.stderr.strip()[-350:] if returncode else None
    except subprocess.TimeoutExpired:
        timed_out, error = True, "Case parsing time window exhausted"
    path = out / "manifest.json"
    data = json_object(path) if path.is_file() else {}
    emit_parse(ctx, record, path, data, False, timed_out, error)
    return 1 if timed_out else returncode


def emit_parse(ctx, record, path, data, cached, timed_out, error=None):
    pages = data.get("pages", [])
    emit({"case_id": ctx["case_id"], "source": record["path"],
          "parser_status": ("partial" if pages else "indeterminate") if timed_out
                           else data.get("status", "failed"),
          "cached": cached, "manifest": str(path) if path.is_file() else None,
          "physical_pages_read": len(pages), "page_count": data.get("page_count"),
          "status_counts": data.get("status_counts", {}),
          "seconds_left": round(max(0, available_time(ctx, reserve=False)), 1),
          "error": error or data.get("error")})


def run_renderer(command, ctx, work, env=None):
    seconds = min(90, available_time(ctx))
    if seconds < 1:
        raise ValueError("Rendering window ended. Finalize with a documented limitation.")
    try:
        result = subprocess.run(command, cwd=work, env=env, capture_output=True, check=False,
                                encoding="utf-8", errors="replace", timeout=seconds)
    except subprocess.TimeoutExpired as err:
        raise ValueError("Preview renderer timed out") from err
    if result.returncode:
        hint = (result.stderr or result.stdout).strip()[-250:]
        raise ValueError(f"Preview renderer exited {result.returncode}: {hint}")


def preview_metadata(path):
    if path.suffix.lower() == ".pdf":
        from pypdfium2 import PdfDocument
        with PdfDocument(path) as document:
            if len(document) == 0:
                raise ValueError("Preview PDF contains no pages")
            return {"pages": len(document)}
    from PIL import Image
    with Image.open(path) as picture:
        if picture.format != "PNG":
            raise ValueError("Expected a genuine PNG preview")
        size = picture.size
        picture.verify()
    return {"pixels": list(size)}


def preview_record(work, artifact):
    key = hashlib.sha256(artifact.encode()).hexdigest()[:24]
    return work / "previews" / f"{key}.json"


def sharp_runtime():
    node = os.getenv("CODEX_PRIMARY_RUNTIME_NODE") or shutil.which("node")
    env = os.environ.copy()
    modules = env.get("CODEX_PRIMARY_RUNTIME_NODE_MODULES")
    if modules:
        env["NODE_PATH"] = os.pathsep.join(part for part in (modules, env.get("NODE_PATH")) if part)
    return node, env


def svg_renderer(source, generated):
    inkscape = shutil.which("inkscape")
    if inkscape:
        return [inkscape, str(source), f"--export-filename={generated}"], "Inkscape", None
    node, env = sharp_runtime()
    if generated.suffix == ".png" and node:
        snippet = ('require("sharp")(process.argv[1]).png().toFile(process.argv[2])'
                   '.catch(error => { console.error(error.message); process.exitCode = 1; });')
        return [node, "-e", snippet, str(source), str(generated)], "Sharp/libvips", env
    raise ValueError("SVG renderer unavailable. Use Inkscape for PDF or Inkscape/Sharp for PNG.")


def render_preview(args):
    ctx = case_state(args)
    check_model_budget(ctx, for_new_work=True)
    source = inside(args.output, args.artifact, "/app/output")
    source_type = source.suffix.lower()
    if source_type not in PREVIEW_SOURCES or not source.is_file():
        raise ValueError("Expected an Office or SVG artifact already saved under the output directory")
    extension = ".pdf" if source_type in OFFICE else ".png"
    preview = inside(args.output, args.to or source.with_suffix(f".preview{extension}"), "/app/output")
    if preview.suffix.lower() not in ({".pdf"} if source_type in OFFICE else {".pdf", ".png"}):
        raise ValueError("Use PDF for Office, or PDF/PNG for SVG")
    source_hash = file_hash(source)
    with tempfile.TemporaryDirectory(prefix="render-", dir=args.work) as directory:
        staging = Path(directory)
        custom_env = None
        if source_type in OFFICE:
            tool = shutil.which("soffice") or shutil.which("libreoffice")
            if not tool:
                raise ValueError("LibreOffice renderer unavailable. Record the missing preview in the result.")
            generated = staging / f"{source.stem}.pdf"
            command = [tool, f"-env:UserInstallation={(staging / 'profile').as_uri()}",
                       "--headless", "--convert-to", "pdf", "--outdir", str(staging), str(source)]
            renderer = "LibreOffice"
        elif source_type == ".svg":
            generated = staging / f"preview{preview.suffix.lower()}"
            command, renderer, custom_env = svg_renderer(source, generated)
        else:
            generated = staging / f"preview{preview.suffix.lower()}"
            tool_name = {".dot": "dot", ".mmd": "mmdc", ".mermaid": "mmdc", ".drawio": "drawio"}.get(source_type)
            tool = shutil.which(tool_name) if tool_name else None
            if not tool:
                raise ValueError(f"No actual renderer for {source_type}; document the preview limitation")
            if source_type == ".dot":
                command = [tool, "-Tpng", str(source), "-o", str(generated)]
            elif source_type in {".mmd", ".mermaid"}:
                command = [tool, "-i", str(source), "-o", str(generated)]
            else:
                command = [tool, "--export", "--format", "png", "--output", str(generated), str(source)]
            renderer = tool_name
        run_renderer(command, ctx, args.work, custom_env)
        if not generated.is_file() or generated.stat().st_size == 0:
            raise ValueError("Renderer produced no preview file")
        metadata = preview_metadata(generated)
        if source_hash != file_hash(source):
            raise ValueError("Artifact changed during rendering. Render its current version.")
        preview.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=preview.parent, delete=False) as handle:
            staged = Path(handle.name)
        try:
            shutil.copyfile(generated, staged)
            staged.replace(preview)
        finally:
            staged.unlink(missing_ok=True)
    relative = source.relative_to(args.output).as_posix()
    proof = {"artifact": relative, "artifact_sha256": source_hash,
             "preview": preview.relative_to(args.output).as_posix(), "preview_sha256": file_hash(preview),
             "renderer": renderer, **metadata}
    receipt = preview_record(args.work, relative)
    receipt.parent.mkdir(exist_ok=True)
    write_json(receipt, proof)
    emit(proof)
    return 0


def artifact_issues(args, ctx):
    issues = []
    for value in ctx["required_artifacts"]:
        path = inside(args.output, value, "/app/output")
        if not path.is_file() or path.stat().st_size == 0:
            issues.append(f"Required output is absent or empty: {value}")
    for path in sorted(args.output.rglob("*")):
        if path.suffix.lower() not in PREVIEW_SOURCES or not path.is_file():
            continue
        relative = path.relative_to(args.output).as_posix()
        artifact = inside(args.output, relative, "/app/output")
        receipt = preview_record(args.work, relative)
        if receipt.is_file():
            proof = json_object(receipt)
            preview = inside(args.output, proof["preview"], "/app/output")
            if (proof["artifact"] == relative and proof["artifact_sha256"] == file_hash(artifact)
                    and preview.is_file() and proof["preview_sha256"] == file_hash(preview)):
                continue
        issues.append(f"Rendered preview missing or stale: {relative}")
    return issues


def finalize(args):
    ctx = case_state(args)
    result_path = args.output / "result.json"
    if result_path.exists():
        prior_id = json_object(result_path).get("case_id")
        if type(prior_id) is not type(ctx["case_id"]) or prior_id != ctx["case_id"]:
            raise ValueError("Existing result belongs to a different or unknown case")
    schema = ctx["schema"]
    data = json_object(inside(args.work, args.draft, "/app/work"))
    if "case_id" in data and (type(data["case_id"]) is not type(ctx["case_id"]) or
                              data["case_id"] != ctx["case_id"]):
        raise ValueError("Draft case_id does not match the input case")
    data["case_id"] = ctx["case_id"]
    audit_path = inside(args.work, args.audit, "/app/work")
    embedded_audit = data.get("audit", data)
    audit = (embedded_audit if ctx.get("audit_location") == "work" else
             json_object(audit_path) if audit_path.is_file() else embedded_audit)
    issues, method = contract.schema_errors(data, schema)
    if "status" in data or contract.allows_field(schema, "status"):
        status_record = {**data, "status_reason": audit.get("reason")} if audit.get("reason") else data
        issues.extend(contract.enforce_status(status_record, schema))
    issues.extend(delivery_audit.validate(audit, data, ctx, args, inside, json_object, file_hash, scoped_pages))
    if method == "agent_review" and not args.reviewed_contract:
        issues.append("Review the prose output_contract against the draft, then pass --reviewed-contract")
    files = artifact_issues(args, ctx)
    success = contract.status_kind(audit.get("status")) == "success"
    try:
        check_model_budget(ctx)
        if success and available_time(ctx, reserve=False) <= 0:
            raise ValueError("Case deadline exceeded; a successful delivery is no longer within budget")
    except ValueError as err:
        if success:
            issues.append(str(err))
    if files and not success and not audit.get("uncertainties"):
        issues.append("Record actual missing/stale artifacts in audit uncertainties")
    if issues or (success and files):
        diagnostic(args, ctx["case_id"], schema, "Final result pending contract or artifact corrections.", ctx=ctx)
        emit({"status": "invalid_draft", "validator": method, "errors": issues[:20],
              "artifact_issues": files[:20], "result": str(result_path)})
        return 2
    if not args.check:
        # Publish proof first: interrupted publication cannot leave a new success without its audit.
        proof = {key: audit.get(key) for key in (*delivery_audit.FIELDS, "reason")}
        proof.update(case_id=ctx["case_id"], versions=ctx["versions"],
                     model_usage=ctx.get("model_usage", {"requests": None, "max_response_tokens": None, "source": None}),
                     elapsed_seconds=round(max(0, time.time() - ctx["start_epoch"]), 2),
                     artifact_issues=files, validation={"complete": True, "validator": method,
                     "scope_and_semantics": "agent_review_required; structural locations checked"})
        with tempfile.NamedTemporaryFile(dir=args.output, prefix=".result-", suffix=".json", delete=False) as handle:
            staged_result = Path(handle.name)
        try:
            write_json(staged_result, data)
            proof["result_sha256"] = file_hash(staged_result)
            write_json(audit_destination(args, ctx), proof)
            if success and available_time(ctx, reserve=False) <= 0:
                raise ValueError("Case deadline exceeded during publication; success was not published")
            staged_result.replace(result_path)
        finally:
            staged_result.unlink(missing_ok=True)
    emit({"status": audit.get("status"), "case_id": ctx["case_id"], "validator": method,
          "result": str(result_path), "written": not args.check, "artifact_issues": files[:20],
          "elapsed_seconds": round(max(0, time.time() - ctx["start_epoch"]), 2)})
    return 1 if files or not success else 0


def checkpoint(args):
    ctx = case_state(args)
    path = args.output / "result.json"
    if path.exists():
        prior = json_object(path)
        if type(prior.get("case_id")) is not type(ctx["case_id"]) or prior.get("case_id") != ctx["case_id"]:
            raise ValueError("Existing result belongs to a different or unknown case")
        if contract.status_kind(prior.get("status")) in {"success", "partial"}:
            raise ValueError("Use finalize to update a result that already contains verified findings")
        audit_path = audit_destination(args, ctx)
        if audit_path.is_file():
            proof = json_object(audit_path)
            if (proof.get("case_id") == ctx["case_id"] and proof.get("result_sha256") == file_hash(path)
                    and contract.status_kind(proof.get("status")) in {"success", "partial"}):
                raise ValueError("Use finalize to update a closed-contract result with verified findings")
    data = pending_result(args, ctx["case_id"], ctx["schema"], args.reason, args.status, ctx)
    issues, method = contract.schema_errors(data, ctx["schema"])
    issues.extend(contract.enforce_status(data, ctx["schema"]))
    write_json(path, data)
    emit({"status": data["status"], "result": str(path), "validator": method, "errors": issues[:20]})
    return 2 if issues else 1


def probe(command, parameters, env=None):
    executable = shutil.which(command) if command else None
    if not executable:
        return {"available": False}
    try:
        call = subprocess.run([executable, *parameters], env=env, text=True, encoding="utf-8", errors="replace",
                              capture_output=True, timeout=6, check=False)
        lines = (call.stdout or call.stderr).splitlines()
        return {"available": call.returncode == 0, "output": [line[:220] for line in lines[:20]]}
    except (OSError, subprocess.TimeoutExpired) as err:
        return {"available": False, "reason": type(err).__name__}


def doctor(args):
    installed = dependency_versions()
    for name in ("jsonschema", "PyYAML"):
        try:
            installed[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            installed[name] = None
    office = "soffice" if shutil.which("soffice") else "libreoffice"
    node, env = sharp_runtime()
    emit({"python": sys.version.split()[0], "runner": VERSION, "parser": PARSER_VERSION,
          "python_packages": installed, "tesseract": probe("tesseract", ["--version"]),
          "ocr_languages": probe("tesseract", ["--list-langs"]),
          "office_renderer": probe(office, ["--version"]), "inkscape": probe("inkscape", ["--version"]),
          "sharp": probe(node, ["-e", 'console.log(require("sharp").versions.sharp)'], env)})
    return 0


def check_bundle(args):
    root = args.directory.resolve()
    if (not root.is_dir() or args.directory.is_symlink()
            or getattr(args.directory.lstat(), "st_reparse_tag", None) == 0xA0000003):
        raise ValueError("Bundle must be a real directory")
    sizes, issues = {}, []
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            stat = path.lstat()
            # Also reject Windows directory junctions, which the archive cannot carry portably.
            if path.is_symlink() or getattr(stat, "st_reparse_tag", None) == 0xA0000003:
                issues.append(f"Link is prohibited: {relative}")
                if name in dirs:
                    dirs.remove(name)
                continue
            if name not in files:
                continue
            sizes[relative] = stat.st_size
            if not path.is_file() or stat.st_size > 500_000:
                issues.append(f"Not a regular text file within 500,000 bytes: {relative}")
                continue
            try:
                if "\x00" in path.read_text(encoding="utf-8"):
                    issues.append(f"Unexpected binary content: {relative}")
            except UnicodeDecodeError:
                issues.append(f"Not UTF-8: {relative}")
    total = sum(sizes.values())
    if len(sizes) > 50 or total > 2_000_000:
        issues.append("File count or total bytes exceed the submission limits")
    issues.extend(f"Required skill file absent: {p}" for p in sorted(EXPECTED_FILES - sizes.keys()))
    issues.extend(f"Unexpected submission file: {p}" for p in sorted(sizes.keys() - EXPECTED_FILES))
    if "GITHUB_URL.txt" in sizes:
        lines = (root / "GITHUB_URL.txt").read_text(encoding="utf-8-sig").splitlines()
        if (not 1 <= len(lines) <= 2 or not re.fullmatch(
                r"https://github\.com/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+(?:/[^\s?#]+)?/?",
                lines[0] if lines else "")):
            issues.append("GITHUB_URL.txt needs a GitHub HTTPS link on line 1 and at most one relative file path")
        elif len(lines) == 2:
            try:
                target = inside(root, lines[1])
                if Path(lines[1]).is_absolute() or not target.is_file():
                    raise ValueError("Line 2 must identify an existing submission-relative file")
            except (OSError, ValueError) as err:
                issues.append(f"Invalid GITHUB_URL.txt relative file: {err}")
    if "SKILL.md" in sizes and not any("SKILL.md" in issue for issue in issues):
        parts = (root / "SKILL.md").read_text(encoding="utf-8").split("\n---", 1)
        valid_yaml = (len(parts) == 2 and parts[0].startswith("---\n") and re.fullmatch(
            r"---\nname: (?=.{1,64}$)[a-z0-9]+(?:-[a-z0-9]+)*\ndescription: >-\n"
            r"(?:  \S[^\n]*(?:\n|$))+", parts[0], flags=re.MULTILINE))
        if not valid_yaml:
            issues.append("SKILL.md must open with a valid name and folded description")
    resource_paths = []
    manifest_review = "not_supplied"
    if args.resource and not args.resource_manifest:
        issues.append("Pass --resource-manifest with the organizer's environment/resources.yaml")
    if args.resource_manifest:
        manifest = args.resource_manifest.resolve()
        if (not manifest.is_file() or args.resource_manifest.is_symlink() or manifest.name != "resources.yaml"
                or manifest.parent.name != "environment"):
            issues.append("Expected an existing environment/resources.yaml")
        else:
            try:
                import yaml
                class UniqueLoader(yaml.SafeLoader):
                    pass
                def unique_mapping(loader, node):
                    result = {}
                    for key, value in node.value:
                        name = loader.construct_object(key)
                        if name in result:
                            raise ValueError(f"Duplicate resource manifest key: {name}")
                        result[name] = loader.construct_object(value)
                    return result
                UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
                text = manifest.read_text(encoding="utf-8-sig")
                if len(text.encode("utf-8")) > 500_000:
                    raise ValueError("Resource manifest is too large for bounded preflight")
                if any(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(text)):
                    raise ValueError("Expand YAML aliases before resource validation")
                declaration = yaml.load(text, Loader=UniqueLoader)
                scalars = set()
                def declared_paths(node, container=False, depth=0):
                    if depth > 20:
                        raise ValueError("Resource manifest nesting is too deep")
                    if isinstance(node, str):
                        scalars.add(node)
                        if container and "://" not in node:
                            resource_paths.append(node)
                    elif isinstance(node, list):
                        for item in node:
                            declared_paths(item, container, depth + 1)
                    elif isinstance(node, dict):
                        for key, value in node.items():
                            declared_paths(value, key in {"source", "source_path", "local_path", "path", "file", "files"}, depth + 1)
                declared_paths(declaration)
                for value in args.resource:
                    if value not in scalars:
                        issues.append(f"Resource argument is not declared in resources.yaml: {value}")
                resource_paths = sorted(set(resource_paths + args.resource))
                if not resource_paths:
                    issues.append("No local resources resolved; review the organizer's actual manifest schema")
                manifest_review = "paths_checked; official_schema_review_required"
            except ImportError:
                issues.append("PyYAML is required to read resources.yaml; no resource validation was performed")
            except Exception as err:
                issues.append(f"Resource manifest could not be validated: {type(err).__name__}: {str(err)[:220]}")
        for value in resource_paths:
            path = Path(value)
            resolved = manifest.parent / path
            if (path.is_absolute() or not resolved.is_file() or resolved.is_symlink()
                    or not resolved.resolve().is_relative_to(manifest.parent.parent)):
                issues.append(f"Resource must resolve to a regular file relative to resources.yaml: {value}")
            else:
                for ancestor in [resolved, *resolved.parents]:
                    if ancestor == manifest.parent.parent:
                        break
                    if ancestor.is_symlink() or getattr(ancestor.lstat(), "st_reparse_tag", None) == 0xA0000003:
                        issues.append(f"Resource path traverses a prohibited link: {value}")
                        break
    emit({"status": "invalid" if issues else "ok", "file_count": len(sizes),
          "bytes": total, "files": sizes, "resource_manifest_checked": bool(args.resource_manifest and not issues),
          "resource_paths": resource_paths, "resource_schema_status": manifest_review,
          "errors": issues})
    return 2 if issues else 0


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--case", type=Path, default=Path("/input/case.json"))
    parser.add_argument("--work", type=Path, default=Path("/app/work"))
    parser.add_argument("--output", type=Path, default=Path("/app/output"))
    commands = parser.add_subparsers(required=True)

    command = commands.add_parser("prepare", help="read the case and Markdown task; initialize a result checkpoint")
    command.add_argument("--file", action="append", default=[], help="extra local file explicitly named by the task")
    command.add_argument("--budget-seconds", type=int, choices=range(1, 901), default=900, metavar="1-900")
    command.add_argument("--max-repairs", type=int, choices=range(0, 65), default=2, metavar="0-64",
                         help="freeze the case-wide reparse limit at first prepare; failed repairs count")
    command.add_argument("--audit-location", choices=("output", "work"),
                         help="use work when result.json embeds the audit and extra output files are forbidden")
    command.set_defaults(handler=prepare)

    command = commands.add_parser("parse", help="read a declared PDF and write page evidence")
    command.add_argument("source", help="case-relative path or declared ID")
    selection = command.add_mutually_exclusive_group()
    selection.add_argument("--pages", help="case-permitted physical pages, e.g. 1-3,7,12-")
    selection.add_argument("--all-pages", action="store_true", help="use only if the task permits the full document")
    command.add_argument("--lang", help="installed Tesseract language(s), e.g. eng+chi_sim")
    command.add_argument("--ocr", choices=("auto", "off"), default="auto")
    command.add_argument("--ocr-timeout", type=int, choices=range(1, 61), default=30, metavar="1-60")
    command.add_argument("--render", choices=("auto", "all", "none"), default="auto")
    command.add_argument("--dpi", type=int, choices=range(72, 451), default=160, metavar="72-450")
    command.add_argument("--layout")
    command.add_argument("--table-region", action="append", default=[])
    command.add_argument("--ocr-region", action="append", default=[])
    command.add_argument("--password-env")
    command.add_argument("--refresh", action="store_true", help="run again without reusing a cached parse")
    command.set_defaults(handler=parse_source)

    command = commands.add_parser("preview", help="render a PDF or PNG preview from an output artifact")
    command.add_argument("artifact", help="path within the output directory")
    command.add_argument("--to", help="target PDF/PNG path within output, if the contract names it")
    command.set_defaults(handler=render_preview)

    command = commands.add_parser("finalize", help="validate the answer and publish result.json atomically")
    command.add_argument("--draft", default="draft.json", help="draft path within the work directory")
    command.add_argument("--reviewed-contract", action="store_true", help="agent verified a prose contract")
    command.add_argument("--check", action="store_true", help="validate only")
    command.add_argument("--audit", default="draft.audit.json", help="runtime evidence/coverage audit within work")
    command.set_defaults(handler=finalize)

    command = commands.add_parser("checkpoint", help="record a reason when no verified answer can be assembled")
    command.add_argument("--status", choices=("indeterminate", "refused"), default="indeterminate")
    command.add_argument("--reason", required=True)
    command.set_defaults(handler=checkpoint)

    command = commands.add_parser("budget", help="read time reserve and optionally record real model meter values")
    command.add_argument("--model-requests", type=int)
    command.add_argument("--max-response-tokens", type=int)
    command.add_argument("--meter-source", help="actual harness counter or log path; no estimates")
    command.set_defaults(handler=budget)

    commands.add_parser("doctor", help="report actual local dependencies").set_defaults(handler=doctor)
    command = commands.add_parser("check-bundle", help="verify the exact UTF-8 submission directory")
    command.add_argument("--directory", type=Path, default=HERE.parent)
    command.add_argument("--resource-manifest", type=Path)
    command.add_argument("--resource", action="append", default=[], help="relative file declared by that manifest")
    command.set_defaults(handler=check_bundle)
    return parser.parse_args()


def main():
    args = arguments()
    try:
        if args.handler not in (doctor, check_bundle):
            args.case, args.work, args.output = (path.resolve() for path in (args.case, args.work, args.output))
            roots = (args.case.parent, args.work, args.output)
            if any(a.is_relative_to(b) for a in roots for b in roots if a is not b):
                raise ValueError("Input, work, and output must be distinct and must not contain each other")
            args.work.mkdir(parents=True, exist_ok=True)
            args.output.mkdir(parents=True, exist_ok=True)
        return args.handler(args)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError) as err:
        emit({"status": "error", "reason": f"{type(err).__name__}: {err}"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
