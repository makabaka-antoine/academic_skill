#!/usr/bin/env python3
"""Inventory Harbor inputs and extract citable source units."""

import argparse
import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PureWindowsPath
from xml.etree import ElementTree as ET

from check_result import extract_schema, load_json
from edit_audit import atomic_write


TEXT_EXTENSIONS = {".txt", ".md", ".tex", ".bib", ".csv", ".tsv", ".json", ".yaml", ".yml", ".rst", ".qmd", ".sty", ".cls"}
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_SOURCE = 40_000_000


def sha(data):
    return hashlib.sha256(data).hexdigest()


def relative_file(root, name):
    if not isinstance(name, str) or not name.strip() or "\\" in name:
        raise ValueError(f"invalid relative file: {name!r}")
    path = Path(name)
    selected = (root / path).resolve()
    windows = PureWindowsPath(name)
    if path.is_absolute() or windows.drive or windows.root or ".." in path.parts or not selected.is_relative_to(root):
        raise ValueError(f"file leaves its designated directory: {name}")
    return selected


def save_json(path, document):
    atomic_write(path, (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def decode_source(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-32" if raw.startswith(b"\xff\xfe\0\0") else "utf-16"
    elif raw.startswith(b"\0\0\xfe\xff"):
        encoding = "utf-32"
    else:
        encoding = "utf-8-sig"
    text = raw.decode(encoding)
    if "\0" in text:
        raise ValueError("binary content or undeclared encoding")
    return text, encoding


def docx_paragraphs(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        info = package.getinfo("word/document.xml")
        if info.file_size > MAX_SOURCE:
            raise ValueError("document XML exceeds the extraction limit")
        body = ET.fromstring(package.read(info)).find(f"{WORD}body")
    if body is None:
        raise ValueError("Word main body is missing")
    paragraphs = []
    for paragraph in body.iter(f"{WORD}p"):
        text = "".join(item.text or "" for item in paragraph.iter(f"{WORD}t"))
        paragraphs.append(text)
    return paragraphs


def pdf_pages(path, executable):
    program = executable or shutil.which("pdftotext")
    if program:
        try:
            completed = subprocess.run(
                [program, "-layout", "-enc", "UTF-8", str(path), "-"],
                capture_output=True, check=True, timeout=75,
            )
            pages = completed.stdout.decode("utf-8").split("\f")
            if pages and not pages[-1].strip() and len(pages) > 1:
                pages.pop()
            return pages, "pdftotext"
        except (OSError, UnicodeError, subprocess.SubprocessError):
            if importlib.util.find_spec("pypdf") is None:
                raise
    if importlib.util.find_spec("pypdf") is None:
        raise ValueError("no PDF text extractor available")
    from pypdf import PdfReader
    return [page.extract_text() or "" for page in PdfReader(str(path)).pages], "pypdf"


def extract(path, raw, executable):
    extension = path.suffix.lower()
    if extension == ".pdf":
        pages, method = pdf_pages(path, executable)
        return "page", pages, method
    if extension == ".docx":
        return "paragraph", docx_paragraphs(raw), "main document text; tables in XML order"
    text, method = decode_source(raw)
    return "line", text.splitlines(), method


def format_snapshot(kind, units):
    if kind == "line":
        return "\n".join(units) + "\n"
    return "\n\n".join(f"[[{kind.upper()} {index}]]\n{text}" for index, text in enumerate(units, 1)) + "\n"


def inventory(case_path, work, task_path=None, pdftotext=None, selected=()):
    root = case_path.resolve().parent
    work = work.resolve()
    if work == root or work.is_relative_to(root):
        raise ValueError("the work directory must be outside the case input directory")
    if task_path and not task_path.resolve().is_relative_to(root):
        raise ValueError("the task file must be inside the case input directory")
    raw_case = case_path.read_bytes()
    case = load_json(raw_case.decode("utf-8-sig"))
    if not isinstance(case, dict):
        raise ValueError("case JSON must be an object")
    notes = []
    schema = extract_schema(case.get("output_contract"), notes)
    selected = {relative_file(root, name).relative_to(root).as_posix() for name in selected}
    absent = sorted(name for name in selected if not (root / name).is_file())
    result = {
        "case_id": case.get("case_id"),
        "case_sha256": sha(raw_case),
        "files": [],
        "missing_selected": absent,
        "contract": {"required": schema.get("required", []), "properties": schema.get("properties", {}), "notes": notes},
    }
    detected_task = task_path or root / "task.md"
    result["task"] = detected_task.relative_to(root).as_posix() if detected_task.is_file() else None
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            continue
        name = path.relative_to(root).as_posix()
        entry = {"file": name, "size": path.stat().st_size}
        result["files"].append(entry)
        if path.resolve() in {case_path.resolve(), detected_task.resolve()}:
            entry["status"] = "case_instruction"
            continue
        if selected and name not in selected:
            entry["status"] = "listed_only"
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS | {".pdf", ".docx"}:
            entry["status"] = "native_inspection_required"
            continue
        try:
            if entry["size"] > MAX_SOURCE:
                raise ValueError("use scoped extraction for this large file")
            raw = path.read_bytes()
            kind, units, method = extract(path, raw, pdftotext)
        except Exception as error:
            entry.update(status="execution_failed", reason=str(error))
            continue
        key = sha(name.encode("utf-8"))[:16]
        snapshot = f"sources/{key}.txt"
        formatted = format_snapshot(kind, units).encode("utf-8")
        atomic_write(work / snapshot, formatted)
        missing = [index for index, text in enumerate(units, 1) if not text.strip() and kind == "page"]
        entry.update(
            status="needs_page_readback" if missing else "extracted",
            sha256=sha(raw), snapshot=snapshot, snapshot_sha256=sha(formatted),
            locator=kind, units=len(units), empty_pages=missing, method=method,
        )
    save_json(work / "intake.json", result)
    return result


def load_units(text, kind):
    if kind == "line":
        return text.splitlines()
    marker = rf"(?m)^\[\[{kind.upper()} \d+\]\]\n"
    return re.split(marker, text)[1:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, default=Path("/input/case.json"))
    parser.add_argument("--task", type=Path)
    parser.add_argument("--work", type=Path, default=Path("/app/work"))
    parser.add_argument("--pdftotext", help="Path of the runtime's declared PDF extractor")
    parser.add_argument("--source", action="append", default=[], help="Extract only this named input; repeat for more files")
    args = parser.parse_args()
    task = args.task.resolve() if args.task else None
    result = inventory(args.case, args.work, task, args.pdftotext, args.source)
    print(json.dumps({
        "intake": str(args.work / "intake.json"), "files": result["files"],
        "task": result["task"], "missing_selected": result["missing_selected"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except (OSError, UnicodeError, ValueError) as error:
        print(json.dumps({"status": "execution_failed", "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
