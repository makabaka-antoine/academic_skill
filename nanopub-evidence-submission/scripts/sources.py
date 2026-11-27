"""Discover actual case material and make addressable source snapshots."""

from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from core import InputError, META_FIELDS, file_hash, fingerprint, hash_bytes, read_json, sentences

EXTENSIONS = {".pdf", ".md", ".txt", ".tex", ".docx", ".json", ".html", ".htm", ".csv", ".tsv"}
INSTRUCTION_NAMES = {"case.json", "task.md", "readme.md", "config.json", "challenge.json", "resources.yaml"}
INPUT_KEYS = {
    "input", "inputs", "files", "input_file", "input_files", "material", "materials",
    "source", "sources", "document", "documents", "paper", "papers", "pdf", "pdf_path",
    "file", "path", "local_path", "source_file", "attachments", "artifact", "artifacts",
    "filename", "file_name", "file_path", "source_path", "url", "input_artifacts",
}
TEXT_KEYS = {"paragraph", "passage", "excerpt", "source_text", "paper_text", "input_text"}
BIBLIOGRAPHY = re.compile(r"^(?:references|bibliography|literature cited|works cited|参考文献|参考资料)\s*[:：]?$", re.I)
BACK_MATTER = re.compile(r"^(?:acknowledg(?:e)?ments?|funding|author contributions?|competing interests?|利益冲突|致谢|资助)\s*[:：]?$", re.I)
HEADING = re.compile(
    r"^(?:abstract|summary|introduction|background|related work|materials and methods|"
    r"(?:experimental )?methods?|methodology|results?(?: and discussion)?|experiments?|"
    r"evaluation|discussion|conclusions?|limitations?|supplementary (?:material|information)|"
    r"appendix(?:\s+[A-Z])?|摘要|引言|背景|相关工作|材料与方法|研究方法|方法|实验|实验结果|结果|"
    r"结果与讨论|讨论|结论|局限性|补充材料|附录)\s*[:：]?$", re.I,
)
CAPTION = re.compile(r"^\s*((?:fig(?:ure)?\.?|tab(?:le)?\.?|图|表)\s*(?:[A-Z]?\d+(?:[.-]\d+)*[a-z]?|[IVXLCDM]{1,6}))"
                     r"(?:\s*[:：.．]|\s*[\u2013\u2014]|\s{2,}|\s*\n|$)", re.I)
DOI = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
ARXIV = re.compile(r"\barXiv\s*:\s*((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7}))(v\d+)?", re.I)


def _key(value):
    return str(value).replace("-", "_").casefold()


def _path_hint(value):
    if not isinstance(value, str) or "\n" in value or len(value) > 900:
        return False
    return Path(unquote(urlsplit(value.replace("\\", "/")).path)).suffix.lower() in EXTENSIONS


def _case_materials(case):
    paths, passages = [], []

    def visit(value, key="", in_input=False):
        if isinstance(value, dict):
            for k, child in value.items():
                normalized = _key(k)
                if normalized in {"output", "outputs", "output_contract", "expected_output", "task", "instruction", "prompt",
                                  "metadata", "provenance", "reference", "relationships"}:
                    continue
                visit(child, normalized, in_input or normalized in INPUT_KEYS)
        elif isinstance(value, list):
            for child in value:
                visit(child, key, in_input)
        elif isinstance(value, str) and value.strip():
            if key in INPUT_KEYS and _path_hint(value):
                paths.append(value)
            elif key in TEXT_KEYS or (in_input and key in {"text", "content"}) or (
                key in {"input", "source"} and len(value) > 35 and not _path_hint(value)
            ):
                passages.append(value.strip())

    visit(case)
    return list(dict.fromkeys(paths)), list(dict.fromkeys(passages))


def _listed_path(value, root):
    normalized = value.replace("\\", "/")
    parts = urlsplit(normalized)
    if len(parts.scheme) > 1:
        if parts.scheme in {"http", "https"}:
            path = Path(unquote(parts.path))
            closest = [p for p in root.rglob(path.name) if p.is_file() and p.resolve().is_relative_to(root)] if path.name else []
            return closest[0].resolve() if len(closest) == 1 else None
        return None
    relative = normalized.removeprefix("/input/") if normalized.startswith("/input/") else normalized
    candidate = (root / relative).resolve()
    return candidate if candidate.is_relative_to(root) and candidate.is_file() else None


def discover(case, root, explicit, ignored):
    hints, passages = _case_materials(case)
    problems = []
    found = []
    for value in explicit or hints:
        path = Path(value).expanduser().resolve() if explicit else _listed_path(value, root)
        if path is None or not path.is_file():
            problems.append({"kind": "input_missing", "input": value, "message": "Named material is unavailable in the input bundle."})
        elif path.suffix.lower() not in EXTENSIONS:
            problems.append({"kind": "unsupported_input", "input": path.name, "message": "This file type has no reader."})
        else:
            found.append(path)
    if not explicit and not hints and root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.name.lower() in INSTRUCTION_NAMES or path.suffix.lower() not in EXTENSIONS or not path.is_file():
                continue
            real = path.resolve()
            if (real.is_relative_to(root) and not any(part.startswith(".") for part in path.relative_to(root).parts)
                    and not any(real == area or real.is_relative_to(area) for area in ignored)):
                found.append(real)
    if found and not explicit and not hints:
        # A supplied PDF and its extracted text are different artifacts. Surface both
        # so the agent can follow the case's scope instead of guessing a preferred one.
        found.sort(key=lambda p: (p.suffix.lower() != ".pdf", str(p).casefold()))
    return list(dict.fromkeys(found)), ([] if explicit else passages), problems


def _xml_tag(node):
    return node.tag.rsplit("}", 1)[-1]


def _order_page(blocks, width):
    left = [b for b in blocks if b["bbox"][2] < width * 0.515 and b["bbox"][2] - b["bbox"][0] > width * .20]
    right = [b for b in blocks if b["bbox"][0] > width * .48 and b["bbox"][2] - b["bbox"][0] > width * .20]
    if len(left) < 2 or len(right) < 2:
        return sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))
    dividers = sorted((b for b in blocks if b["bbox"][0] < width * .41 and b["bbox"][2] > width * .59),
                      key=lambda b: b["bbox"][1])
    rest, ordered = [b for b in blocks if b not in dividers], []
    for divider in dividers + [None]:
        y = divider["bbox"][1] if divider else float("inf")
        band = [b for b in rest if b["bbox"][1] < y]
        ordered.extend(sorted(band, key=lambda b: ((b["bbox"][0] + b["bbox"][2]) / 2 > width / 2,
                                                    b["bbox"][1], b["bbox"][0])))
        rest = [b for b in rest if b not in band]
        if divider:
            ordered.append(divider)
    return ordered


def _fitz():
    for name in ("pymupdf", "fitz"):
        try:
            module = importlib.import_module(name)
            if hasattr(module, "open") and hasattr(module, "TEXTFLAGS_DICT"):
                return module
        except (ImportError, OSError):
            continue
    raise ImportError("PyMuPDF is unavailable")


def _pymupdf_pages(path):
    fitz = _fitz()
    with fitz.open(str(path)) as doc:
        if doc.needs_pass:
            raise InputError("encrypted_pdf", "PDF requires a password.")
        for i, page in enumerate(doc, 1):
            blocks = []
            flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
            extracted = page.get_text("dict", flags=flags)
            for block in extracted.get("blocks", []):
                if block.get("type") != 0:
                    continue
                lines = ["".join(span["text"] for span in line["spans"]).strip() for line in block["lines"]]
                text = "\n".join(line for line in lines if line)
                if text.strip():
                    blocks.append({"text": text, "bbox": [round(x, 2) for x in block["bbox"]]})
            blocks.extend(_table_blocks(page, blocks))
            yield i, _order_page(blocks, page.rect.width)


def _table_blocks(page, blocks):
    if not hasattr(page, "find_tables"):
        return []
    captions = []
    for block in blocks:
        match = CAPTION.match(block["text"])
        if match and re.match(r"tab|表", match[1], re.I):
            captions.append((match[1], block["bbox"]))
    if not captions:
        return []
    try:
        tables = page.find_tables(strategy="lines").tables
        result = []
        for table in tables:
            rows = table.extract()
            if len(rows) < 2 or min(map(len, rows)) < 2:
                continue
            box = [round(x, 2) for x in table.bbox]
            label, bounds = min(captions, key=lambda row: min(abs(row[1][3] - box[1]), abs(row[1][1] - box[3])))
            if min(abs(bounds[3] - box[1]), abs(bounds[1] - box[3])) > 75:
                label = None
            text = "\n".join("| " + " | ".join(" ".join((cell or "").split()).replace("|", "｜") for cell in row)
                             + " |" for row in rows if any(cell and cell.strip() for cell in row))
            if text:
                result.append({"text": text, "bbox": box, "kind": "table", "label": label, "extraction": "pymupdf-table"})
        return result
    except (AttributeError, RuntimeError, TypeError, ValueError):
        # Retain the already extracted native text even when this backend
        # version or page does not produce a usable grid.
        return []


def _poppler_pages(path):
    tool = shutil.which("pdftotext")
    if not tool:
        raise ImportError("pdftotext is unavailable")
    result = subprocess.run([tool, "-enc", "UTF-8", "-bbox-layout", str(path), "-"],
                            capture_output=True, check=True, timeout=90)
    root = ET.fromstring(result.stdout)
    for i, page in enumerate((node for node in root.iter() if _xml_tag(node) == "page"), 1):
        blocks = []
        for node in page.iter():
            if _xml_tag(node) != "block":
                continue
            lines = [" ".join(word.text or "" for word in line if _xml_tag(word) == "word")
                     for line in node if _xml_tag(line) == "line"]
            if any(lines):
                blocks.append({"text": "\n".join(lines),
                               "bbox": [round(float(node.attrib[k]), 2) for k in ("xMin", "yMin", "xMax", "yMax")]})
        yield i, _order_page(blocks, float(page.attrib["width"]))


def _pypdf_pages(path):
    from pypdf import PdfReader
    with path.open("rb") as handle:
        doc = PdfReader(handle)
        if doc.is_encrypted:
            raise InputError("encrypted_pdf", "PDF requires a password.")
        for i, page in enumerate(doc.pages, 1):
            try:
                value = page.extract_text() or ""
                yield i, [{"text": chunk, "bbox": None} for chunk in _pdf_paragraphs(value)]
            except Exception as error:
                yield i, [{"page_failure": f"{type(error).__name__}: {str(error)[:160]}"}]


def _pdf_paragraphs(value):
    """Keep line/blank-line structure, with standalone headings and captions."""
    pending = []
    for line in value.splitlines():
        stripped = line.strip()
        heading = heading_kind(stripped)
        if not stripped or heading or CAPTION.match(stripped):
            if pending:
                yield "\n".join(pending)
                pending = []
            if stripped:
                yield stripped
        else:
            pending.append(stripped)
    if pending:
        yield "\n".join(pending)


def render_page(path, page, dpi=165):
    """Render one physical PDF page to PNG bytes for local inspection or OCR."""
    try:
        fitz = _fitz()
        with fitz.open(str(path)) as doc:
            return doc[page - 1].get_pixmap(dpi=dpi, alpha=False).tobytes("png")
    except (ImportError, RuntimeError, ValueError, IndexError, OSError):
        tool = shutil.which("pdftoppm")
        if not tool:
            raise InputError("render_unavailable", "Install or locate PyMuPDF or pdftoppm for PDF rendering.")
        result = subprocess.run([tool, "-f", str(page), "-l", str(page), "-r", str(dpi),
                                 "-singlefile", "-png", str(path)], capture_output=True, check=True, timeout=40)
        if not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            raise InputError("render_failed", f"Page {page} did not render as a PNG.")
        return result.stdout


def _ocr(path, page, language):
    tool = shutil.which("tesseract")
    if not tool:
        raise InputError("ocr_unavailable", "Tesseract is unavailable for this page.")
    data = render_page(path, page, dpi=190)
    cmd = [tool, "stdin", "stdout", "-l", language, "--psm", "3"]
    result = subprocess.run(cmd, input=data, capture_output=True, check=True, timeout=35)
    raw = result.stdout.decode("utf-8", errors="strict")
    return [{"text": p, "bbox": None, "extraction": "ocr"} for p in _pdf_paragraphs(raw)]


def pdf_blocks(path, ocr, ocr_language, ocr_limit):
    failures, warnings, unreadable = [], [], []
    readers = (("pymupdf", _pymupdf_pages), ("poppler", _poppler_pages), ("pypdf", _pypdf_pages))
    for name, reader in readers:
        try:
            pages = list(reader(path))
            if not pages:
                raise ValueError("no PDF pages returned")
            break
        except InputError:
            raise
        except Exception as error:
            failures.append(f"{name}: {type(error).__name__} ({str(error)[:95]})")
    else:
        raise InputError("pdf_unreadable", "No PDF reader could extract pages. " + " | ".join(failures))
    if name == "pypdf":
        warnings.append({"kind": "pdf_no_bboxes", "message": "Page text is available, block coordinates are unavailable with this backend."})
    blocks, ocr_count = [], 0
    for page, rows in pages:
        failure = next((b["page_failure"] for b in rows if "page_failure" in b), "")
        rows = [b for b in rows if "text" in b]
        count = sum(c.isalnum() for b in rows for c in b["text"])
        use_ocr = ocr == "force" or (ocr == "auto" and count < 24)
        recovered = False
        if use_ocr and ocr_count < ocr_limit:
            ocr_count += 1
            try:
                transcribed = _ocr(path, page, ocr_language)
                if sum(c.isalnum() for b in transcribed for c in b["text"]) >= 12:
                    rows, recovered = transcribed, True
                    warnings.append({"kind": "ocr_page", "page": page, "message": "Check OCR readings against the page image, especially numbers and symbols."})
                else:
                    failure = "OCR returned too little readable text."
            except (OSError, subprocess.SubprocessError, InputError, UnicodeError) as error:
                failure = f"OCR unavailable or failed ({str(error)[:150]})."
        if not recovered and count < 24:
            unreadable.append({"page": page, "reason": failure or ("OCR page allowance exhausted." if use_ocr
                                                                  else "Sparse or missing text layer. OCR disabled.")})
        elif ocr == "force" and not recovered:
            warnings.append({"kind": "ocr_fallback", "page": page,
                             "message": failure or "OCR allowance exhausted. Using native page text."})
        for row in rows:
            blocks.append({**row, "page": page, "extraction": row.get("extraction", name)})
    return blocks, name, len(pages), unreadable, warnings


def heading_kind(text):
    candidate = re.sub(r"^(?:\d+(?:\.\d+)*|[IVX]+)[.)]?\s+", "", text).strip()
    if BIBLIOGRAPHY.fullmatch(candidate):
        return "bibliography"
    if BACK_MATTER.fullmatch(candidate):
        return "back_matter"
    if HEADING.fullmatch(candidate):
        return "body"
    return None


def _text_blocks(text):
    blocks, pending = [], []
    body = False
    section = "Front matter"
    fenced = False
    pending_first = 0

    def flush():
        nonlocal pending
        if pending:
            value = "\n".join(pending).strip()
            if value:
                blocks.append({"text": value, "lines": [pending_first, pending_first + len(pending) - 1],
                               "section": section, "kind": "body" if body else "front"})
            pending = []

    for i, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith(("\x60\x60\x60", "~~~")):
            flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        match = re.match(r"^\s*(?:#{1,6}\s+|\\(?:sub)*section\*?\{)(.*?)(?:\}\s*)?$", line)
        candidate = match[1].strip(" #") if match else line.strip()
        heading = heading_kind(candidate)
        if match or heading:
            flush()
            if match and not blocks and not heading:
                blocks.append({"text": candidate, "lines": [i, i], "kind": "title", "section": section})
            else:
                section, body = candidate, True
            continue
        if not line.strip():
            flush()
        else:
            if not pending:
                pending_first = i
            pending.append(line.strip())
    flush()
    return blocks


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"style", "script", "noscript"}:
            self.skip += 1
        elif tag in {"p", "article", "section", "title", "br", "tr", "li", "figcaption"} and not self.skip:
            self.parts.append("\n\n")
        elif re.fullmatch(r"h[1-6]", tag) and not self.skip:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in {"style", "script", "noscript"}:
            self.skip = max(0, self.skip - 1)
        elif tag in {"p", "li", "tr", "title", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6"} and not self.skip:
            self.parts.append("\n\n")
        elif tag in {"td", "th"} and not self.skip:
            self.parts.append(" | ")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _docx_blocks(path):
    word = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    lines = []
    body = root.find(word + "body")
    if body is None:
        raise InputError("docx_structure", "The Word file has no readable document body.")
    for child in body:
        if child.tag == word + "p":
            text = "".join(part.text or "" for part in child.iter(word + "t")).strip()
            style = child.find(f"{word}pPr/{word}pStyle")
            name = "" if style is None else style.get(word + "val", "")
            if name.casefold() == "title" or name.casefold().startswith("heading"):
                text = ("#" if name.casefold() == "title" else "##") + " " + text
            lines.append(text + "\n")
        elif child.tag == word + "tbl":
            for row in child.findall(word + "tr"):
                cells = [" ".join(v.text or "" for v in cell.iter(word + "t"))
                         for cell in row.findall(word + "tc")]
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
    return [{**block, "lines": None} for block in _text_blocks("\n".join(lines))]


def _json_blocks(obj):
    blocks = []

    def walk(value, pointer, section, in_metadata=False):
        if isinstance(value, dict):
            local_section = value.get("heading") or value.get("section") or section
            local_section = local_section if isinstance(local_section, str) else section
            for key, item in value.items():
                if key in {"heading", "section"}:
                    continue
                escaped = key.replace("~", "~0").replace("/", "~1")
                field = key.casefold()
                metadata_context = not pointer or in_metadata
                meta = (not pointer and field == "metadata") or (metadata_context and field in {
                    "doi", "authors", "year", "arxiv_id", "version", "venue", "title", "url"
                })
                if meta and field == "authors" and isinstance(item, list) and all(isinstance(a, str) for a in item):
                    blocks.append({"text": "Authors: " + "; ".join(item), "section": "Front matter", "kind": "front",
                                   "pointer": pointer + "/" + escaped})
                elif meta and field in {"doi", "year", "arxiv_id", "version", "venue", "url"} and type(item) in {str, int}:
                    label = "arXiv" if field == "arxiv_id" else field
                    blocks.append({"text": f"{label}: {item}", "section": "Front matter", "kind": "front",
                                   "pointer": pointer + "/" + escaped})
                else:
                    next_section = key if field in {"abstract", "results", "methods", "conclusion", "references", "bibliography"} else local_section
                    walk(item, pointer + "/" + escaped, next_section, meta)
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                walk(item, pointer + "/" + str(idx), section, in_metadata)
        elif (isinstance(value, str) and value.strip()) or value is None or type(value) in {int, float, bool}:
            text = value.strip() if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
            blocks.append({"text": text, "section": "Front matter" if in_metadata else section,
                           "kind": "title" if pointer in {"/title", "/metadata/title"}
                           else ("front" if in_metadata else "body"), "pointer": pointer or "", "extraction": "json-field"})

    walk(obj, "", "Document")
    return blocks


def make_units(source_id, blocks):
    units, section, stage = [], "Front matter", "front"
    for block in blocks:
        text = block["text"].strip()
        if not text:
            continue
        # Native blocks can contain a heading followed by the paragraph it starts.
        first, newline, tail = text.partition("\n")
        heading = heading_kind(first)
        if heading and block.get("page") and len(first) < 100:
            section, stage = first, heading
            text = tail.strip() if newline else ""
            if not text:
                continue
        current = block.get("section", section)
        if "section" in block:
            stage = heading_kind(current) or ("front" if current == "Front matter" else "body")
        kind = block.get("kind", stage)
        if stage in {"bibliography", "back_matter"} or (
            re.match(r"^\s*\[\d+\]\s+.{18,}$", text, re.S) and len(re.findall(r"\b(?:19|20)\d{2}\b", text)) >= 1
        ):
            kind = stage if stage in {"bibliography", "back_matter"} else "bibliography"
        elif block.get("page", 0) not in {None, 0, 1} and kind == "front":
            kind = "body"
        caption = CAPTION.match(text)
        table_rows = text.splitlines()
        is_table = (len(table_rows) >= 2 and sum(line.count("|") >= 2 for line in table_rows) >= 2) or (
            len(table_rows) > 1 and all("\t" in line for line in table_rows)
        )
        if kind not in {"bibliography", "back_matter", "table"} and (caption or is_table):
            kind = "table" if is_table else "caption"
        # Keep text in extraction order. PDF layout line endings are collapsed so a
        # sentence spanning two lines still has a contiguous, reproducible span.
        if block.get("page") and kind != "table":
            text = re.sub(r"[ \t]*\n[ \t]*", " ", text)
        if kind == "table":
            spans, pos = [], 0
            for row in text.splitlines(keepends=True):
                value = row.rstrip("\r\n")
                if value.strip() and not re.fullmatch(r"[| :\-]+", value):
                    spans.append([pos, pos + len(value)])
                pos += len(row)
        elif kind in {"title", "bibliography", "back_matter"}:
            spans = [[0, len(text)]]
        else:
            spans = sentences(text)
        units.append({
            "id": f"{source_id}-U{len(units) + 1:04d}", "text": text,
            "text_sha256": hash_bytes(text.encode("utf-8")), "spans": spans,
            "page": block.get("page"), "lines": block.get("lines"), "pointer": block.get("pointer"),
            "bbox": block.get("bbox"), "section": current, "kind": kind,
            "label": block.get("label") or (caption[1] if caption else None), "extraction": block.get("extraction", "text"),
        })
    return units


def _field_candidate(field, value, unit, method):
    return {"field": field, "value": value, "anchor": unit["id"], "method": method}


def metadata_candidates(units):
    result = []
    for unit in units:
        if unit["kind"] not in {"title", "front"} or unit["page"] not in {None, 1}:
            continue
        entire = unit["text"]
        if unit["kind"] == "title":
            result.append(_field_candidate("title", entire, unit, "document heading"))
        for line in entire.splitlines():
            if re.match(r"^(?:authors?|作者)\s*[:：]", line, re.I):
                values = re.split(r"\s*(?:;|；|,|，|、|\band\b)\s*", re.split("[:：]", line, maxsplit=1)[1].strip())
                if values and all(values):
                    result.append(_field_candidate("authors", values, unit, "explicit author line"))
            # Accept an explicit identifier in the front matter only. A DOI
            # mentioned in a correction, copyright notice, or citation is a clue
            # for the human rather than the identity of the input.
            declaration = re.fullmatch(r"\s*(?:doi\s*[:：]\s*|https?://(?:dx\.)?doi\.org/)(\S+)\s*", line, re.I)
            if declaration:
                doi = declaration[1].removeprefix("https://doi.org/").rstrip(".,;")
                if DOI.fullmatch(doi):
                    result.append(_field_candidate("doi", doi, unit, "explicit front-matter identifier"))
            arxiv = ARXIV.match(line)
            if arxiv:
                result.append(_field_candidate("arxiv_id", arxiv[1], unit, "arXiv front-matter stamp"))
                if arxiv[2]:
                    result.append(_field_candidate("version", arxiv[2], unit, "arXiv version suffix"))
            year = re.match(r"^(?:year|published|publication year|年份|发表日期)\s*[:：]\s*((?:19|20)\d{2})\b", line, re.I)
            if year:
                result.append(_field_candidate("year", int(year[1]), unit, "explicit publication date"))
            for field in ("version", "venue", "url"):
                match = re.match(rf"^{field}\s*[:：]\s*(.+)", line, re.I)
                if match:
                    result.append(_field_candidate(field, match[1].strip(), unit, f"explicit {field}"))
    return result[:40]


def _source(source_id, name, digest, size, format_name, blocks, backend, pages, unreadable, warnings, path):
    units = make_units(source_id, blocks)
    return {
        "id": source_id, "name": name, "sha256": digest, "bytes": size, "format": format_name,
        "backend": backend, "pages": pages, "unreadable": unreadable, "warnings": warnings,
        "units": units, "metadata_candidates": metadata_candidates(units),
        "state": "partial" if units and unreadable else ("readable" if units else "unreadable"),
        "_path": str(path) if path else None,
    }


def parse_file(path, source_id, args):
    path = Path(path)
    ext = path.suffix.lower()
    digest = file_hash(path)
    size = path.stat().st_size
    pages, unreadable, warnings, backend = None, [], [], ext[1:]
    if ext == ".pdf":
        blocks, backend, pages, unreadable, warnings = pdf_blocks(path, args.ocr, args.ocr_language, args.ocr_limit)
    elif ext == ".docx":
        blocks = _docx_blocks(path)
    elif ext == ".json":
        blocks = _json_blocks(read_json(path))
    else:
        text = path.read_text(encoding="utf-8-sig")
        if ext in {".html", ".htm"}:
            parser = _HTMLText()
            parser.feed(text)
            blocks = [{**b, "lines": None} for b in _text_blocks("".join(parser.parts))]
        elif ext in {".csv", ".tsv"}:
            blocks = [{"text": text, "lines": [1, len(text.splitlines())], "section": "Table", "kind": "table"}]
        else:
            blocks = _text_blocks(text)
    if file_hash(path) != digest:
        raise InputError("source_changed", f"{path.name} changed while it was read. Retry prepare.")
    return _source(source_id, path.name, digest, size, ext[1:], blocks, backend, pages, unreadable, warnings, path)


def parse_passage(text, source_id, order):
    raw = text.encode("utf-8")
    return _source(source_id, f"case-passage-{order}", hash_bytes(raw), len(raw), "text",
                   _text_blocks(text), "case-text", None, [], [], None)


def prepare(case_path, task_path, args):
    case_path = Path(case_path).resolve() if case_path else None
    task_path = Path(task_path).resolve() if task_path else None
    case = read_json(case_path) if case_path else {}
    if not isinstance(case, dict):
        raise InputError("case_shape", "case.json must contain an object.")
    root = case_path.parent if case_path else (task_path.parent if task_path else Path.cwd().resolve())
    if task_path is None and (root / "task.md").is_file():
        task_path = root / "task.md"
    task = task_path.read_text(encoding="utf-8-sig") if task_path else ""
    paths, passages, problems = discover(case, root, args.input, [Path(args.work).resolve(), Path(args.out).resolve()])
    sources = []
    for path in paths:
        try:
            source = parse_file(path, f"S{len(sources) + 1}", args)
            sources.append(source)
            if not source["units"]:
                problems.append({"kind": "no_readable_text", "input": path.name, "message": "No addressable text was recovered."})
        except (InputError, OSError, UnicodeError, ValueError, zipfile.BadZipFile, ET.ParseError) as error:
            problems.append({"kind": getattr(error, "kind", "parse_failed"), "input": path.name, "message": str(error)[:350]})
    # An excerpt may scope a named paper. Preserve it as a task cue in that
    # situation, so that its position in the paper remains a claim to verify.
    if not paths and not args.input and not problems:
        sources.extend(parse_passage(p, f"S{i}", i) for i, p in enumerate(passages, 1))
    if not sources and not problems:
        problems.append({"kind": "input_missing", "message": "No supported material or case passage was provided."})
    contract = case.get("output_contract")
    binding = {
        "case_sha256": file_hash(case_path) if case_path else None, "task_sha256": file_hash(task_path) if task_path else None,
        "sources": [[s["name"], s["sha256"], s["backend"], [u["text_sha256"] for u in s["units"]]] for s in sources],
        "problems": problems,
    }
    return {
        "snapshot_id": fingerprint(binding), "case_id": case.get("case_id", case.get("caseId", case.get("id"))), "contract": contract,
        "task_file_text": task, "case": case, "passage_hints": passages if paths else [],
        "sources": sources, "problems": problems, "_case_path": str(case_path) if case_path else None,
        "_case_sha256": binding["case_sha256"], "_task_path": str(task_path) if task_path else None,
        "_task_sha256": binding["task_sha256"],
    }


def verify_source_files(state):
    pairs = [(state.get("_case_path"), state.get("_case_sha256")),
             (state.get("_task_path"), state.get("_task_sha256"))]
    pairs.extend((source["_path"], source["sha256"]) for source in state["sources"])
    for path, digest in pairs:
        if path and (not Path(path).is_file() or file_hash(path) != digest):
            raise InputError("stale_snapshot", f"{Path(path).name} changed or moved. Run prepare and re-anchor the claims.")


def blank_metadata():
    return {field: {"value": None, "state": "unknown", "evidence": []} for field in META_FIELDS}
