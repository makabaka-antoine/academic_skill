#!/usr/bin/env python3
"""Author a new DOCX from reviewed prose, then render it with an Office tool."""

import argparse
import hashlib
from html import escape
from importlib.util import find_spec
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.+?)\s*$")
RICH_MARKDOWN = re.compile(
    r"(?m)^(?: {4}|\t| {0,3}(?:[>]|[-+*][ \t]|\d+[.)][ \t]|~{3,}|[-=]+[ \t]*$"
    r"|\[[^\]\n]+\]:|[|: -]+\|[|: -]*$))"
    r"|[\x60\\]|<[/!?A-Za-z]|!\[|\[[^\]\n]+\][ \t]*[\[(]"
    r"|(?<!\w)[*_]{1,3}(?=\S)|\$\S[^$\n]*\$"
)
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DeliveryError(Exception):
    """The requested artifact could not be prepared."""


def executable(explicit, *names):
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise DeliveryError(f"Tool path is unavailable: {path}")
        return path
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    return None


def run(argv, work, timeout, expected):
    args = list(map(str, argv))
    if os.name == "nt" and Path(args[0]).suffix.lower() in {".bat", ".cmd"}:
        raise DeliveryError("Use a native executable or a Python renderer adapter.")
    completed = subprocess.run(
        args, cwd=work, timeout=timeout, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    detail = (completed.stderr.strip() or completed.stdout.strip())[-1500:]
    if completed.returncode:
        raise DeliveryError(f"{Path(args[0]).name} exited {completed.returncode}. {detail}")
    if not expected.is_file():
        raise DeliveryError(f"{Path(args[0]).name} did not produce {expected.name}. {detail}")


def paragraphs(text):
    return [block for block in re.split(r"\n[ \t]*\n+", text.strip("\n")) if block.strip()]


def write_simple_docx(text, markdown, output):
    try:
        from docx import Document
        from docx.shared import RGBColor
    except ImportError as error:
        raise DeliveryError("Install python-docx in the selected runtime or use the declared Pandoc.") from error

    document = Document()
    if not markdown:
        for block in paragraphs(text):
            document.add_paragraph(block)
    else:
        # Put a boundary before ATX headings even if the draft has no blank line.
        separated = re.sub(r"(?m)^(?= {0,3}#{1,6}[ \t]+\S)", "\n", text)
        for block in paragraphs(separated):
            first, _, body = block.partition("\n")
            heading = HEADING.fullmatch(first)
            if heading:
                label = re.sub(r"[ \t]+#+$", "", heading[2])
                styled = document.add_heading(label, level=len(heading[1]))
                styled.style.font.color.rgb = RGBColor(0, 0, 0)
            else:
                body = block
            if body:
                document.add_paragraph(body)
    document.save(output)


def write_docx(text, markdown, reference, path, work, args):
    simple = not markdown or not RICH_MARKDOWN.search(text)
    if reference is None and not args.pandoc and simple and find_spec("docx") is not None:
        write_simple_docx(text, markdown, path)
        return "python-docx"

    pandoc = executable(args.pandoc, "pandoc")
    if pandoc is None:
        if reference is not None:
            raise DeliveryError("Applying this DOCX style reference requires a declared Pandoc executable.")
        if not simple:
            raise DeliveryError("This Markdown uses markup beyond headings and paragraphs. Use Pandoc.")
        write_simple_docx(text, markdown, path)
        return "python-docx"

    if markdown:
        draft = work / "prose.md"
        conversion_input = text
        reader = "markdown-raw_html-raw_tex-raw_attribute-citations"
    else:
        draft = work / "prose.html"
        blocks = [escape(block).replace("\n", "<br>\n") for block in paragraphs(text)]
        conversion_input = "\n".join(f"<p>{block}</p>" for block in blocks)
        reader = "html"
    draft.write_text(conversion_input, encoding="utf-8")
    command = [pandoc, "--sandbox", f"--from={reader}", "--to=docx", "--output", path]
    if reference is not None:
        command.append(f"--reference-doc={reference}")
    command.append(draft)
    run(command, work, args.timeout, path)
    return f"Pandoc ({pandoc})"


def validate_docx(path):
    with ZipFile(path) as package:
        package.getinfo("[Content_Types].xml")
        document = ElementTree.fromstring(package.read("word/document.xml"))
    body = document.find(f"{WORD_NS}body")
    if body is None or not any((node.text or "").strip() for node in body.iter(f"{WORD_NS}t")):
        raise DeliveryError("The generated DOCX has no editable body text.")


def render_pdf(docx_path, work, args):
    if args.renderer:
        # Explicit adapter contract: input DOCX and output PDF are separate argv entries.
        renderer = executable(args.renderer)
        pdf_path = work / "preview.pdf"
        prefix = [sys.executable, renderer] if renderer.suffix.lower() == ".py" else [renderer]
        run([*prefix, docx_path, pdf_path], work, args.timeout, pdf_path)
        return pdf_path, f"declared renderer ({renderer})"

    soffice = executable(args.soffice, "soffice", "libreoffice")
    if soffice is None:
        raise DeliveryError("Office PDF renderer unavailable. Supply the case's renderer or LibreOffice.")
    profile = (work / "office-profile").as_uri()
    pdf_path = work / f"{docx_path.stem}.pdf"
    run([
        soffice, f"-env:UserInstallation={profile}", "--headless", "--norestore",
        "--convert-to", "pdf:writer_pdf_Export", "--outdir", work, docx_path,
    ], work, args.timeout, pdf_path)
    return pdf_path, f"LibreOffice ({soffice})"


def validate_pdf(path):
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise DeliveryError("The Office renderer returned a file without a PDF header.")
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - 1024))
        if b"%%EOF" not in stream.read():
            raise DeliveryError("The PDF preview is incomplete.")


def publish(path, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".delivery-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as target, path.open("rb") as source:
            shutil.copyfileobj(source, target)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "file": str(destination),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Reviewed UTF-8 .txt, .md or .markdown draft")
    parser.add_argument("--docx", type=Path, required=True, help="Editable output .docx")
    parser.add_argument("--pdf", type=Path, required=True, help="Rendered PDF preview")
    parser.add_argument("--reference-doc", type=Path, help="DOCX style reference for Pandoc")
    parser.add_argument("--pandoc", help="Explicit path to the case's Pandoc")
    renderers = parser.add_mutually_exclusive_group()
    renderers.add_argument("--soffice", help="Explicit path to the case's LibreOffice executable")
    renderers.add_argument("--renderer", help="Declared native command or Python adapter accepting DOCX and PDF paths")
    parser.add_argument("--work-dir", type=Path, help="Parent directory for temporary files")
    parser.add_argument("--timeout", type=int, default=120, help="Per-process timeout in seconds")
    args = parser.parse_args()

    report = {"status": "failed", "docx": None, "pdf": None}
    stage = "input"
    try:
        source, docx, pdf = [path.expanduser().resolve() for path in (args.source, args.docx, args.pdf)]
        reference = args.reference_doc.expanduser().resolve() if args.reference_doc else None
        paths = [source, docx, pdf, *([reference] if reference else [])]
        if len(set(paths)) != len(paths):
            raise DeliveryError("Use separate input, reference and output files.")
        if docx.suffix.lower() != ".docx" or pdf.suffix.lower() != ".pdf":
            raise DeliveryError("The output filenames must have .docx and .pdf extensions.")
        if args.timeout < 1:
            raise DeliveryError("The timeout must be positive.")
        if source.suffix.lower() not in {".txt", ".md", ".markdown"}:
            raise DeliveryError("Provide a reviewed UTF-8 text or Markdown draft.")
        if reference is not None and (reference.suffix.lower() != ".docx" or not reference.is_file()):
            raise DeliveryError("The requested DOCX style reference is unavailable or has the wrong format.")
        text = source.read_text(encoding="utf-8-sig")
        if not text.strip():
            raise DeliveryError("The draft contains no prose.")

        stage = "docx"
        parent = args.work_dir.expanduser().resolve() if args.work_dir else None
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="docx-delivery-", dir=parent) as folder:
            work = Path(folder).resolve()
            candidate = work / "editable.docx"
            writer = write_docx(text, source.suffix.lower() != ".txt", reference, candidate, work, args)
            validate_docx(candidate)
            report["docx"] = publish(candidate, docx)
            report["writer"] = writer
            report["status"] = "partial"

            stage = "pdf"
            rendered, renderer = render_pdf(candidate, work, args)
            validate_pdf(rendered)
            report["pdf"] = publish(rendered, pdf)
            report["renderer"] = renderer
            report["status"] = "generated"
            report["visual_review"] = "required"
    except (DeliveryError, OSError, ValueError, KeyError, BadZipFile,
            ElementTree.ParseError, subprocess.TimeoutExpired) as error:
        report["issue"] = {"type": "execution_failed", "stage": stage, "reason": str(error)}

    print(json.dumps(report, indent=2))
    return {"generated": 0, "partial": 1, "failed": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
