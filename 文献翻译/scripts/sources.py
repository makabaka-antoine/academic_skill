"""Freeze source units and their locations before translation."""

from __future__ import annotations

from collections import defaultdict
from io import BytesIO
from pathlib import Path
import re


PAGE_MARKER = re.compile(r"\s*<!--\s*page:\s*([1-9]\d*)\s*-->\s*", re.I)
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
PDF_HEADING = re.compile(
    r"^(?:(?P<number>\d+(?:\.\d+)*)[.)]?\s+)?"
    r"(?P<title>Abstract|Introduction|Methods?|Materials and Methods|Results(?: and Discussion)?|"
    r"Discussion|Conclusions?|References|Bibliography|Appendix(?: [A-Z])?)$", re.I
)
CAPTION = re.compile(r"^(?:Fig(?:ure)?\.?|Table)\s+[A-Z]?\d+[a-z]?[.:]\s", re.I)
TABLE_RULE = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|)+$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
DISPLAY_ENV = re.compile(r"^\\begin\{((?:equation|align|gather|multline)\*?)\}")
REFERENCE_SECTIONS = {"references", "bibliography", "参考文献"}
COPY_KINDS = {"equation", "reference", "code", "table_rule"}


class SourceProblem(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def select_pages(spec: str | None, available: range | None) -> list[int]:
    if not spec:
        return list(available or ())
    if available is not None and not available:
        raise SourceProblem("page_anchors_unavailable", "Use a PDF or page-marked text for --pages.")
    selected = set()
    for item in spec.split(","):
        match = re.fullmatch(r"\s*([1-9]\d*)(?:\s*-\s*([1-9]\d*))?\s*", item)
        if not match:
            raise SourceProblem("invalid_scope", f"Invalid page range: {item}")
        first = int(match[1])
        last = int(match[2] or first)
        if last < first or (available is not None and (first not in available or last not in available)):
            raise SourceProblem("invalid_scope", f"Page range is outside the supplied source: {item}")
        selected.update(range(first, last + 1))
    return sorted(selected)


def pdf_pages(data: bytes, pages: str | None) -> tuple[list[dict], list[tuple], str]:
    try:
        from pypdf import PdfReader, __version__
        from pypdf.errors import DependencyError, PdfReadError
    except ImportError as error:
        raise SourceProblem("pdf_dependency_missing", "Install requirements-pdf.txt or supply verified page-marked text.") from error
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise SourceProblem("encrypted_source", "An accessible copy of the encrypted PDF is needed.")
        page_numbers = select_pages(pages, range(1, len(reader.pages) + 1))
    except DependencyError as error:
        raise SourceProblem("pdf_dependency_missing", "The supplied PDF requires an unavailable decoding dependency.") from error
    except (PdfReadError, OSError) as error:
        raise SourceProblem("invalid_pdf", str(error)) from error

    records, groups = [], []
    for number in page_numbers:
        text_starts = defaultdict(set)

        def remember_position(fragment, cm, tm, _font, _size):
            if len(fragment.strip()) >= 5:
                x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
                y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
                text_starts[round(y / 4)].add(x)

        try:
            page = reader.pages[number - 1]
            content = page.extract_text(visitor_text=remember_position) or ""
            width = float(page.mediabox.width)
            possible_columns = sum(max(xs) - min(xs) > width * 0.3 for xs in text_starts.values()) >= 3
            status = "readable" if content.strip() else "no_text_layer"
            if status == "readable" and (page.rotation or possible_columns):
                status = "layout_uncertain"
        except (DependencyError, PdfReadError, KeyError, ValueError, TypeError, OSError) as error:
            records.append({"page": number, "status": "extraction_failed", "reason": type(error).__name__})
            continue
        records.append({"page": number, "status": status})
        if content.strip():
            groups.append((number, "pdf_physical", list(enumerate(content.splitlines(), 1))))
    return records, groups, f"pypdf {__version__} extracted text order"


def text_pages(data: bytes, pages: str | None, markers: bool) -> tuple[list[dict], list[tuple], str]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as error:
        raise SourceProblem("unreadable_encoding", "Expected UTF-8 Markdown or TXT.") from error
    lines = list(enumerate(text.splitlines(), 1))
    if not markers:
        select_pages(pages, range(0))
        return [], [(None, None, lines)], "UTF-8 source lines"

    groups = {}
    current = None
    fence = None
    for number, line in lines:
        delimiter = FENCE.match(line)
        if delimiter and fence is None:
            fence = delimiter[1]
        elif delimiter and line.strip() == delimiter[1] and delimiter[1][0] == fence[0] and len(delimiter[1]) >= len(fence):
            fence = None
        match = PAGE_MARKER.fullmatch(line) if fence is None else None
        if match:
            page = int(match[1])
            if current is not None and page <= current:
                raise SourceProblem("invalid_page_markers", "Page markers must be strictly increasing.")
            current = page
            groups[current] = []
        elif current is not None:
            groups[current].append((number, line))
        elif line.strip():
            raise SourceProblem("invalid_page_markers", "Text before the first marker has no supplied page anchor.")
    if not groups:
        raise SourceProblem("invalid_page_markers", "Expected at least one <!-- page: N --> marker.")
    selected = select_pages(pages, None) if pages else list(range(min(groups), max(groups) + 1))
    records, chosen = [], []
    for number in selected:
        page_lines = groups.get(number, [])
        status = "readable" if any(line.strip() for _, line in page_lines) else "no_verified_text"
        records.append({"page": number, "status": status})
        if status == "readable":
            chosen.append((number, "declared_page", page_lines))
    notes = "UTF-8 lines with supplied page markers"
    return records, chosen, notes


def blocks(lines: list[tuple[int, str]], pdf: bool):
    buffer = []
    kind, closing = "paragraph", None
    for number, line in lines:
        stripped = line.strip()
        if closing:
            buffer.append((number, line))
            fence_end = kind == "code" and stripped and stripped[0] == closing[0] and set(stripped) == {closing[0]} and len(stripped) >= len(closing)
            if fence_end or (kind == "equation" and stripped.endswith(closing)):
                yield kind, buffer
                buffer, kind, closing = [], "paragraph", None
            continue
        fence, environment = FENCE.match(line), DISPLAY_ENV.match(stripped)
        if HEADING.match(stripped) or (pdf and PDF_HEADING.fullmatch(stripped)):
            boundary = "heading"
        elif stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 2:
            boundary = "table_rule" if TABLE_RULE.fullmatch(stripped) else "table_row"
        elif CAPTION.match(stripped):
            boundary = "caption"
        else:
            boundary = None
        display = environment or stripped.startswith(("$$", r"\["))
        if buffer and (not stripped or boundary or fence or display):
            yield kind, buffer
            buffer, kind = [], "paragraph"
        if not stripped:
            continue
        if fence:
            kind, closing = "code", fence[1]
        elif display:
            kind = "equation"
            end = f"\\end{{{environment[1]}}}" if environment else ("$$" if stripped.startswith("$$") else r"\]")
            if stripped.endswith(end) and stripped != end:
                yield kind, [(number, line)]
                kind = "paragraph"
                continue
            closing = end
        elif boundary in {"heading", "table_row", "table_rule"}:
            yield boundary, [(number, line)]
            continue
        elif boundary == "caption":
            kind = boundary
        buffer.append((number, line))
    if buffer:
        yield kind, buffer


def table_cells(text: str) -> list[str]:
    parts = re.split(r"(\\.|`+[^`]*`+|\$[^$\n]+\$)", text.strip()[1:-1])
    cells = [""]
    for index, part in enumerate(parts):
        if index % 2:
            cells[-1] += part
        else:
            first, *rest = part.split("|")
            cells[-1] += first
            cells.extend(rest)
    return cells


def read_source(path: Path, data: bytes, pages: str | None, section: str | None, markers: bool) -> dict:
    suffix = path.suffix.lower()
    pdf = suffix == ".pdf"
    if pdf:
        if markers:
            raise SourceProblem("invalid_input", "--page-markers is for text with supplied page anchors.")
        page_records, groups, extractor = pdf_pages(data, pages)
    elif suffix in {".md", ".markdown", ".txt"}:
        page_records, groups, extractor = text_pages(data, pages, markers)
    else:
        raise SourceProblem("unsupported_format", "Provide PDF, UTF-8 Markdown or UTF-8 TXT.")
    units, headings = [], []
    for page, origin, lines in groups:
        for block, (kind, block_lines) in enumerate(blocks(lines, pdf), 1):
            first, last = block_lines[0][0], block_lines[-1][0]
            raw = "\n".join(line for _, line in block_lines)
            locator = {"page": page, "page_origin": origin, "block": block, "line_start": first, "line_end": last}
            block_id = f"d01:p{page:04d}-b{block:04d}" if page is not None else f"d01:l{first:05d}"
            fields = {}
            if kind == "heading":
                match = HEADING.fullmatch(raw.strip())
                native = PDF_HEADING.fullmatch(raw.strip()) if not match else None
                if match:
                    level, title = len(match[1]), match[2]
                else:
                    level = (native["number"] or "").count(".") + 1
                    title = native["title"]
                headings = [(depth, old) for depth, old in headings if depth < level] + [(level, title)]
                fields = {"heading": title, "heading_level": level}
            elif headings and headings[-1][1].casefold() in REFERENCE_SECTIONS:
                kind = "reference"
            cell = kind == "table_row"
            parts = table_cells(raw) if cell else [raw]
            for column, text in enumerate(parts, 1):
                units.append({
                    "id": f"{block_id}-c{column:03d}" if cell else block_id,
                    "block_id": block_id, "kind": "table_cell" if cell else kind, "text": text,
                    "location": {**locator, "column": column} if cell else locator,
                    "section": [title for _, title in headings],
                    "translate": bool(text.strip()) and kind not in COPY_KINDS, **fields,
                })
    if section is not None:
        starts = [i for i, unit in enumerate(units) if unit.get("heading", "").casefold() == section.casefold()]
        if len(starts) != 1:
            raise SourceProblem("section_not_unique", "Use a unique extracted heading or the exact page range/excerpt.")
        start = starts[0]
        level = units[start]["heading_level"]
        end = next((i for i in range(start + 1, len(units)) if units[i].get("heading_level", 7) <= level), len(units))
        first_page = units[start]["location"]["page"]
        last_page = units[end]["location"]["page"] if end < len(units) else None
        if first_page is not None:
            page_records = [entry for entry in page_records if entry["page"] >= first_page
                            and (last_page is None or entry["page"] <= last_page)]
        units = units[start:end]
    return {"units": units, "pages": page_records, "extractor": extractor}
