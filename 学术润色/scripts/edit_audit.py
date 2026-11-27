#!/usr/bin/env python3
"""Inspect observed anchors and apply exact scholarly edits to a new copy."""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Anchor:
    kind: str
    start: int
    end: int
    text: str
    key: str


class Document:
    def __init__(self, path, source_format="auto"):
        self.path = Path(path)
        self.raw = self.path.read_bytes()
        self.text = self.raw.decode("utf-8-sig")
        self.hash = hashlib.sha256(self.raw).hexdigest()
        self.format = source_format
        if source_format == "auto":
            self.format = "latex" if self.path.suffix.lower() == ".tex" else "text"
        self.line_starts = [0] + [match.end() for match in re.finditer("\n", self.text)]

    def loc(self, start):
        line = bisect_right(self.line_starts, start)
        return {"line": line, "column": start - self.line_starts[line - 1] + 1}

    def record(self, anchor):
        return {
            "kind": anchor.kind, "text": anchor.text,
            "start": anchor.start, "end": anchor.end, **self.loc(anchor.start),
        }

    def identity(self):
        return {"file": self.path.name, "sha256": self.hash, "format": self.format}


HSPACE = r"[ \t\u00a0\u202f\u3000]*"
DIGIT = r"[0-9０-９]"
UNSIGNED = (
    rf"(?:{DIGIT}{{1,3}}(?:[,，]{DIGIT}{{3}})+(?:[.．]{DIGIT}+)?"
    rf"|{DIGIT}+(?:[.．]{DIGIT}+)?|[.．]{DIGIT}+)"
)
SIGNED = rf"[+\-−－＋]?{UNSIGNED}(?:[eE][+\-−]?{DIGIT}+)?"
RELATION = r"(?:[<>＜＞]=?|[=＝≤≥≈])"
STATISTIC = r"(?:n|N|p|q|r|R²|OR|RR|HR|β)"
QUANTITY = rf"(?:(?:{STATISTIC}{HSPACE})?{RELATION}{HSPACE})?{SIGNED}(?:{HSPACE}[/／–—\-~〜～]{HSPACE}{SIGNED})?"
UNIT_EQUIVALENTS = {
    "pp": ("pp", "percentage point", "percentage points", "percentage-point", "percentage-points", "个百分点", "百分点"),
    "%": ("%", r"\%", "％"),
    "ms": ("ms", "毫秒"),
    "s": ("s", "sec", "second", "seconds", "秒"),
    "min": ("min", "minute", "minutes", "分钟"),
    "h": ("h", "hr", "hour", "hours", "小时"),
    "d": ("d", "day", "days", "天"),
    "wk": ("wk", "week", "weeks", "周"),
    "mo": ("mo", "month", "months", "个月"),
    "yr": ("yr", "year", "years", "年"),
    "µg": ("µg", "μg", "ug", "微克"),
    "mg": ("mg", "毫克"),
    "g": ("g", "克"),
    "kg": ("kg", "千克", "公斤"),
    "µm": ("µm", "μm", "微米"),
    "mm": ("mm", "毫米"),
    "cm": ("cm", "厘米"),
    "m": ("m", "米"),
    "km": ("km", "千米", "公里"),
    "mL": ("mL", "ml", "毫升"),
    "L": ("L", "升"),
    "°C": ("°C", "℃", "摄氏度"),
}
UNIT_MAP = {variant: key for key, values in UNIT_EQUIVALENTS.items() for variant in values}
UNIT_FORMS = set(UNIT_MAP) | {
    "ng", "pg", "nm", "pm", "μL", "µL", "uL", "nL", "mmHg", "Pa", "kPa", "MPa",
    "Hz", "kHz", "MHz", "GHz", "W", "mW", "kW", "V", "mV", "A", "mA", "J", "kJ",
    "K", "°F", "mol", "mmol", "μmol", "µmol", "mM", "μM", "µM", "nM", "M",
    "m²", "cm²", "mm²", "m³", "cm³", "mm³", "GB", "MB", "kbps", "Mbps", "dB",
    "kg/m²", "g/L", "mg/L", "mg/dL", "m/s²", "m/s", "km/h", "mol/L", "×",
}
UNIT_PATTERN = "|".join(re.escape(unit) for unit in sorted(UNIT_FORMS, key=lambda value: (-len(value), value)))
NUMBER = re.compile(
    rf"(?<![A-Za-z0-9_０-９])(?P<quantity>{QUANTITY})"
    rf"(?:(?:{HSPACE}|-)(?P<unit>{UNIT_PATTERN})(?![A-Za-z_]))?"
    rf"(?![0-9０-９]|[.．][0-9０-９])"
)
REF_KIND = {
    "图": "figure", "表": "table", "式": "equation", "附录": "appendix",
    "文献": "reference", "节": "section",
    "fig": "figure", "figs": "figure", "figures": "figure",
    "tables": "table", "eq": "equation", "eqs": "equation", "equations": "equation",
    "ref": "reference", "refs": "reference", "references": "reference",
    "sec": "section", "sections": "section",
}
LABEL = rf"[A-Za-z]?{DIGIT}+(?:[.．]{DIGIT}+)*(?:\([a-z]\)|[a-z])?"
OBJECT_REFERENCE = re.compile(
    rf"(?P<kind>\b(?i:fig(?:ures?|s)?|tables?|eq(?:uations?|s)?|appendix|refs?|references?|sec(?:tions?)?)\.?"
    rf"|图|表|式|附录|文献|节)[ \t\u00a0\u202f\u3000~]*"
    rf"(?P<label>\({LABEL}\)|{LABEL}|[A-Z](?![A-Za-z0-9]))"
    rf"(?![A-Za-z0-9_]|[.．]{DIGIT})"
)
CITATION_ID = r"(?:C|REF|SRC|SYN-[A-Z]*)[-_]?\d+"
CITATION = re.compile(
    r"\[(?:@[\w:./+\-]+[^\]\n]*?"
    rf"|{CITATION_ID}(?:\s*[,;]\s*{CITATION_ID})*"
    r"|\d+(?:[a-z]?\s*[-–,;]\s*\d+)*[a-z]?)\]"
    r"|\((?:[A-Z\u3400-\u9fff][^()\n]{0,85}?),\s*(?:18|19|20)\d{2}[a-z]?(?:;[^()\n]{1,100})?\)"
)
FENCE = re.compile(r"^[ \t]{0,3}(\x60{3,}|~{3,})")
MATH = re.compile(
    r"\\begin\{(?P<environment>(?:equation|align|gather|multline|flalign|alignat|displaymath|math|eqnarray"
    r"|verbatim|Verbatim|lstlisting|minted|tabular|tabularx|longtable)\*?)\}"
    r"[\s\S]*?\\end\{(?P=environment)\}"
    r"|\\\[[\s\S]*?\\\]|\\\([^\n]*?\\\)"
    r"|(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$"
    r"|(?<![\\$])\$(?!\$)(?:\\.|[^$\\\r\n])+?\$(?![$0-9])"
)
MARKDOWN_CODE = re.compile(r"(?<!\x60)(\x60+)(?!\x60)[^\r\n]*?\1(?!\x60)")
TEX_INLINE_CODE = re.compile(r"\\(?:verb\*?|lstinline)(?P<delimiter>[^A-Za-z0-9\s\\])[^\r\n]*?(?P=delimiter)")
DESTINATION = re.compile(r"\]\((?:[^()\n]|\([^()\n]*\))*\)|https?://[^\s<>\"\x60)\]]+")
MD_STRUCTURE = re.compile(r"^[ \t]{0,3}(?:#{1,6}(?=[ \t])|[-+*](?=[ \t])|\d+[.)](?=[ \t]))", re.M)
MD_TABLE_ROW = re.compile(r"^[ \t]{0,3}\|[^\r\n]*\|[ \t]*(?=\r?$)", re.M)
TEX_COMMAND = re.compile(r"\\([A-Za-z@]+\*?|[&#_$~{}])")
TEX_COMMENTS = re.compile(r"(?<!\\)(?:\\\\)*(%[^\r\n]*)")
TEX_ARGUMENT = re.compile(r"[ \t]*([\[{])")
TEXT_MACROS = {
    "section", "subsection", "subsubsection", "chapter", "paragraph", "subparagraph",
    "caption", "title", "emph", "textbf", "textit", "textsc",
    "textsuperscript", "textsubscript", "footnote",
}
REVIEW_CUES = {
    "negation": r"\b(?:not|never|no|without|neither|\w+n['’]t)\b|未(?:能|发现|观察到|验证|记录)|没有|并无|并非|不支持|不足以|无显著|不能|不排除",
    "uncertainty": r"\b(?:may|might|could|possibly|probably|suggests?|appears?|consistent with)\b|可能|或许|提示|与.{1,24}一致",
    "association": r"\b(?:associated with|correlat(?:ed|es|ion)|association|correlates? with)\b|相关|关联",
    "causality_or_proof": r"\b(?:caus(?:es?|ed|al)|leads? to|led to|proves?|demonstrates?)\b|导致|引起|证[明实]|揭示了.{0,8}机制",
    "scope_and_design": r"\b(?:pilot|exploratory|post hoc|subgroup|only|all|every|observational|independent validation)\b|仅|所有|探索性|事后|亚组|先导|观察性|外部验证|独立验证",
    "statistical_qualification": r"\b(?:statistically significant|significant(?:ly)?|equivalen(?:t|ce)|non-inferior)\b|显著|等效|非劣",
    "direction": r"\b(?:high(?:er)?|low(?:er)?|increas(?:e|ed|es)|decreas(?:e|ed|es)|reduc(?:e|ed|es)|positive|negative)\b|升高|增加|降低|减少|高于|低于|正相关|负相关",
}


def cue_changes(before, after, fmt):
    """Surface changed cues for semantic review. A hit can be an acceptable edit."""
    _, old = markup_anchors(before, fmt)
    _, new = markup_anchors(after, fmt)
    report = []
    for category, pattern in REVIEW_CUES.items():
        old_matches = Counter(match.group().casefold() for match in re.finditer(pattern, old, re.I))
        new_matches = Counter(match.group().casefold() for match in re.finditer(pattern, new, re.I))
        if old_matches != new_matches:
            report.append({
                "category": category,
                "removed": dict(old_matches - new_matches),
                "added": dict(new_matches - old_matches),
            })
    return report


def raw_anchor(kind, text, start, end):
    raw = text[start:end]
    return Anchor(kind, start, end, raw, raw)


def mask(text, spans):
    """Blank spans without disturbing character offsets or line boundaries."""
    pieces = []
    cursor = 0
    for span in sorted(spans, key=lambda item: item.start):
        if span.end <= cursor:
            continue
        start = max(span.start, cursor)
        pieces.append(text[cursor:start])
        pieces.append(re.sub(r"[^\r\n]", " ", text[start:span.end]))
        cursor = span.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def markdown_blocks(text):
    front = re.match(r"\A---[ \t]*\r?\n[\s\S]*?^---[ \t]*(?:\r?\n|$)", text, re.M)
    if front:
        yield raw_anchor("frontmatter", text, 0, front.end())
    opened = None
    offset = front.end() if front else 0
    for line in text[offset:].splitlines(keepends=True):
        match = FENCE.match(line)
        if match:
            ticks = match.group(1)
            if opened is None:
                opened = (offset, ticks)
            elif ticks.startswith(opened[1]) and not line[match.end():].strip():
                yield raw_anchor("code_block", text, opened[0], offset + len(line.rstrip("\r\n")))
                opened = None
        offset += len(line)
    if opened:
        yield raw_anchor("code_block", text, opened[0], len(text))


def matching_end(text, start):
    pairs = {"{": "}", "[": "]"}
    stack = [pairs[text[start]]]
    cursor = start + 1
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == stack[-1]:
            stack.pop()
            if not stack:
                return cursor + 1
        elif char == "{" or (char == "[" and stack[-1] == "]"):
            stack.append(pairs[char])
        cursor += 1
    return None


def latex_commands(text, visible):
    consumed = 0
    for match in TEX_COMMAND.finditer(visible):
        if match.start() < consumed:
            continue
        command = match.group(1).rstrip("*")
        cursor = match.end()
        while argument := TEX_ARGUMENT.match(visible, cursor):
            start = argument.start(1)
            end = matching_end(visible, start)
            if end is None:
                break
            if command in TEXT_MACROS and visible[start] == "{":
                yield raw_anchor("latex_structure", text, match.start(), start + 1)
                yield raw_anchor("latex_structure", text, end - 1, end)
                consumed = start + 1
                break
            cursor = end
        if consumed <= match.start():
            kind = "citation" if "cite" in command.lower() else "latex_command"
            if command.lower() in {"ref", "cref", "autoref", "eqref", "pageref", "label"}:
                kind = "latex_reference"
            yield raw_anchor(kind, text, match.start(), cursor)
            consumed = cursor


def markup_anchors(text, source_format):
    anchors = list(markdown_blocks(text)) if source_format != "latex" else []
    visible = mask(text, anchors)
    anchors.extend(raw_anchor("inline_code", text, *match.span()) for match in TEX_INLINE_CODE.finditer(visible))
    visible = mask(text, anchors)
    if source_format == "latex":
        comments = [
            raw_anchor("latex_comment", text, *match.span(1))
            for match in TEX_COMMENTS.finditer(visible)
        ]
        visible = mask(visible, comments)
        begin = re.search(r"\\begin[ \t]*\{document\}", visible)
        if begin:
            anchors.append(raw_anchor("latex_preamble", text, 0, begin.end()))
            comments = [item for item in comments if item.start >= begin.end()]
            visible = mask(visible, anchors)
        anchors.extend(comments)
    for kind, pattern in (("inline_code", MARKDOWN_CODE), ("math_or_environment", MATH)):
        spans = [raw_anchor(kind, text, *match.span()) for match in pattern.finditer(visible)]
        if spans:
            anchors.extend(spans)
            visible = mask(visible, spans)
    commands = list(latex_commands(text, visible))
    anchors.extend(commands)
    visible = mask(visible, commands)
    for kind, pattern in (("link", DESTINATION), ("citation", CITATION)):
        spans = [raw_anchor(kind, text, *match.span()) for match in pattern.finditer(visible)]
        if spans:
            anchors.extend(spans)
            visible = mask(visible, spans)
    return anchors, visible


def collect_anchors(text, source_format="text", profile="strict"):
    anchors, visible = markup_anchors(text, source_format)
    if source_format != "latex":
        structure = [
            raw_anchor("markdown_structure", text, *match.span())
            for match in MD_STRUCTURE.finditer(visible)
        ]
        for row in MD_TABLE_ROW.finditer(visible):
            for delimiter in re.finditer(r"(?<!\\)\|", row.group()):
                start = row.start() + delimiter.start()
                structure.append(raw_anchor("markdown_table", text, start, start + 1))
        anchors.extend(structure)
        visible = mask(visible, structure)
    object_refs = []
    for match in OBJECT_REFERENCE.finditer(visible):
        raw = text[match.start():match.end()]
        key = raw
        if profile == "aligned":
            name = match.group("kind").casefold().rstrip(".")
            label = unicodedata.normalize("NFKC", match.group("label"))
            if label.startswith("(") and label.endswith(")"):
                label = label[1:-1]
            key = f"{REF_KIND.get(name, name)}:{label}"
        object_refs.append(Anchor("object_reference", match.start(), match.end(), raw, key))
    anchors.extend(object_refs)
    visible = mask(visible, object_refs)
    for match in NUMBER.finditer(visible):
        start = match.start()
        while start < match.end() and visible[start].isspace():
            start += 1
        raw = text[start:match.end()]
        quantity = re.sub(r"\s+", "", match.group("quantity"))
        unit = match.group("unit") or ""
        if profile == "aligned":
            quantity = unicodedata.normalize("NFKC", quantity).replace("−", "-")
            unit = UNIT_MAP.get(unit, unit)
        key = f"{quantity}|{unit}"
        anchors.append(Anchor("numeric", start, match.end(), raw, key))
    return sorted(anchors, key=lambda item: (item.start, item.end, item.kind))


def signature(anchors):
    return [(item.kind, item.key) for item in anchors]


def differences(source, candidate):
    old_signature = signature(source)
    new_signature = signature(candidate)
    if old_signature == new_signature:
        return []
    old_count = Counter(old_signature)
    new_count = Counter(new_signature)
    result = []
    for label, anchors, counts in (
        ("missing", source, old_count - new_count), ("added", candidate, new_count - old_count)
    ):
        for item in anchors:
            key = (item.kind, item.key)
            if counts[key]:
                result.append({"issue": label, "anchor": item})
                counts[key] -= 1
    if not result:
        for index, (old, new) in enumerate(zip(old_signature, new_signature)):
            if old != new:
                result.append({
                    "issue": "anchor_order_changed",
                    "anchor": source[index], "candidate_anchor": candidate[index],
                })
                break
    return result


def public_differences(items, source, candidate):
    result = []
    for item in items:
        document = candidate if item["issue"] == "added" else source
        row = {"issue": item["issue"], "anchor": document.record(item["anchor"])}
        if "candidate_anchor" in item:
            row["candidate_anchor"] = candidate.record(item["candidate_anchor"])
        result.append(row)
    return result


def term_pattern(term):
    escaped = re.escape(term)
    if re.match(r"[A-Za-z0-9]", term[0]):
        escaped = rf"(?<![A-Za-z0-9_\-]){escaped}"
    if re.match(r"[A-Za-z0-9]", term[-1]):
        escaped = rf"{escaped}(?![A-Za-z0-9_\-])"
    return re.compile(escaped)


def use_glossary(document, path, language):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"concept_id", f"term_{language}"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"glossary columns required: {', '.join(sorted(required))}")
        rows = list(reader)
    terms = defaultdict(list)
    concepts = Counter()
    for row in rows:
        concept = (row.get("concept_id") or "").strip()
        preferred = (row.get(f"term_{language}") or "").strip()
        if not concept or not preferred:
            continue
        concepts[concept] += 1
        variants = (row.get(f"variants_{language}") or "").split("|")
        for value in dict.fromkeys([preferred, *(term.strip() for term in variants)]):
            if value:
                terms[value].append((concept, preferred))
    conflicts = [{"concept_id": key, "issue": "duplicate_concept"} for key, count in concepts.items() if count > 1]
    conflicted_terms = set()
    for spelling, mappings in terms.items():
        if len(set(mappings)) > 1 or any(concepts[concept] > 1 for concept, _ in mappings):
            conflicts.append({"term": spelling, "mappings": sorted(set(mappings))})
            conflicted_terms.add(spelling)
    _, visible = markup_anchors(document.text, document.format)
    findings = []
    occupied = set()
    for term in sorted(terms, key=lambda value: (-len(value), value)):
        concept, preferred = terms[term][0]
        for match in term_pattern(term).finditer(visible):
            positions = range(*match.span())
            if any(position in occupied for position in positions):
                continue
            occupied.update(positions)
            if term in conflicted_terms or term != preferred:
                findings.append({
                    "issue": "glossary_conflict" if term in conflicted_terms else "term_variant",
                    "concept_id": concept, "text": term,
                    "preferred": None if term in conflicted_terms else preferred,
                    **document.loc(match.start()),
                })
    if language == "en":
        for row in rows:
            preferred = (row.get("term_en") or "").strip()
            abbreviation = (row.get("abbreviation") or "").strip()
            if preferred and abbreviation and preferred not in conflicted_terms:
                first = term_pattern(abbreviation).search(visible)
                if first and not re.search(rf"{re.escape(preferred)}\s*\(\s*$", visible[:first.start()], re.I):
                    findings.append({
                        "issue": "check_first_abbreviation_use", "text": abbreviation,
                        "supplied_full_form": preferred, **document.loc(first.start()),
                    })
    return {"conflicts": conflicts, "findings": sorted(findings, key=lambda row: (row["line"], row["column"]))}


def atomic_write(path, content):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, scratch = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(scratch, target)
    finally:
        Path(scratch).unlink(missing_ok=True)


def ensure_separate(reads, writes):
    protected = [Path(path).resolve() for path in reads if path]
    for path in writes:
        if not path:
            continue
        target = Path(path).resolve()
        if target in protected or (
            target.exists() and any(path.exists() and target.samefile(path) for path in protected)
        ):
            raise ValueError(f"output overlaps another input or output: {target.name}")
        protected.append(target)


def show(data, path=None):
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if path:
        atomic_write(path, payload.encode("utf-8"))
        short = {"status": data.get("status"), "report": str(path)}
        if "coverage" in data:
            short["coverage"] = data["coverage"]
        print(json.dumps(short, ensure_ascii=False))
    else:
        print(payload, end="")


def locate(document, edit):
    before = edit["before"]
    if "start" in edit:
        start = edit["start"]
        if type(start) is not int or start < 0 or not document.text.startswith(before, start):
            raise ValueError("start does not match the supplied original text")
        if "line" in edit and edit["line"] != document.loc(start)["line"]:
            raise ValueError("line and start disagree")
        return start
    offsets = [match.start() for match in re.finditer(f"(?={re.escape(before)})", document.text)]
    if "line" in edit:
        line = edit["line"]
        if type(line) is not int or line < 1:
            raise ValueError("line must be a positive integer")
        offsets = [offset for offset in offsets if document.loc(offset)["line"] == line]
    if len(offsets) != 1:
        raise ValueError(f"expected one exact match, found {len(offsets)}; specify the original line or start")
    return offsets[0]


def plan_edit(document, anchors, edit, available_id):
    if not isinstance(edit, dict):
        raise ValueError("edit must be an object")
    for key in ("id", "before", "after", "reason"):
        if not isinstance(edit.get(key), str) or not (edit[key].strip() or key == "after"):
            raise ValueError(f"{key} must be a string with content")
    if not available_id:
        raise ValueError("duplicate edit id")
    if not isinstance(edit.get("types"), list) or not edit["types"] or not all(
        isinstance(kind, str) and kind.strip() for kind in edit["types"]
    ):
        raise ValueError("types must be a nonempty list of edit types")
    start = locate(document, edit)
    end = start + len(edit["before"])
    affected = [item for item in anchors if item.start < end and item.end > start]
    if any(item.start < start or item.end > end for item in affected):
        raise ValueError("span divides a protected anchor; use the full sentence or a narrower prose edit")
    local_after = collect_anchors(edit["after"], document.format)
    warnings = differences(affected, local_after)
    if warnings:
        labels = ", ".join(
            f"{item['issue']} {item['anchor'].kind} {item['anchor'].text[:72]!r}" for item in warnings[:3]
        )
        raise ValueError(f"protected anchors changed: {labels}")
    return {**edit, "start": start, "end": end, **document.loc(start)}


def apply_plan(args, document, anchors):
    plan = json.loads(Path(args.plan).read_bytes().decode("utf-8-sig"))
    if not isinstance(plan, dict) or plan.get("source_sha256") != document.hash:
        raise ValueError("edit plan does not match source_sha256; inspect the current source")
    edits = plan.get("edits")
    if not isinstance(edits, list):
        raise ValueError("edits must be an array")
    occurrences = Counter(
        item["id"] for item in edits if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    accepted = []
    rejected = []
    unchanged = []
    for index, item in enumerate(edits, 1):
        identifier = item.get("id") if isinstance(item, dict) else None
        try:
            edit = plan_edit(document, anchors, item, isinstance(identifier, str) and occurrences[identifier] == 1)
        except ValueError as error:
            rejected.append({"id": identifier, "plan_index": index, "reason": str(error)})
            continue
        if edit["before"] == edit["after"]:
            unchanged.append(edit["id"])
        else:
            accepted.append(edit)
    accepted.sort(key=lambda item: item["start"])
    overlaps = set()
    if accepted:
        furthest = accepted[0]
        for edit in accepted[1:]:
            if edit["start"] < furthest["end"]:
                overlaps.update((edit["id"], furthest["id"]))
            if edit["end"] > furthest["end"]:
                furthest = edit
    for edit in accepted:
        if edit["id"] in overlaps:
            rejected.append({"id": edit["id"], "line": edit["line"], "reason": "overlapping edits"})
    accepted = [edit for edit in accepted if edit["id"] not in overlaps]
    pieces = []
    cursor = 0
    candidate_cursor = 0
    for edit in accepted:
        preceding = document.text[cursor:edit["start"]]
        pieces.extend((preceding, edit["after"]))
        candidate_cursor += len(preceding)
        edit["candidate_start"] = candidate_cursor
        candidate_cursor += len(edit["after"])
        edit["candidate_end"] = candidate_cursor
        cursor = edit["end"]
    pieces.append(document.text[cursor:])
    merged = "".join(pieces)
    boundary_issues = differences(anchors, collect_anchors(merged, document.format))
    if boundary_issues:
        show({
            "status": "issues", "source": document.identity(),
            "reason": "combined edits changed anchors at a boundary; no candidate written",
            "issues": [
                {"issue": item["issue"], "kind": item["anchor"].kind,
                 "text": item["anchor"].text, "start": item["anchor"].start}
                for item in boundary_issues
            ],
            "rejected": rejected,
        }, args.report)
        return 1
    output = merged.encode("utf-8")
    if document.raw.startswith(b"\xef\xbb\xbf"):
        output = b"\xef\xbb\xbf" + output
    atomic_write(args.output, output)
    candidate = Document(args.output, document.format)
    for edit in accepted:
        edit["candidate_location"] = candidate.loc(edit["candidate_start"])
        edit["semantic_review_cues"] = cue_changes(edit["before"], edit["after"], document.format)
    show({
        "status": "partial" if rejected else "passed",
        "check": "exact span and observed anchors", "semantic_review": "required",
        "source": document.identity(), "candidate": candidate.identity(),
        "coverage": {"planned": len(edits), "applied": len(accepted), "unchanged": len(unchanged), "rejected": len(rejected)},
        "changes": accepted, "unchanged": unchanged, "rejected": rejected,
    }, args.report)
    return 1 if rejected else 0


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("inspect", "check", "apply"):
        sub = commands.add_parser(command)
        sub.add_argument("source")
        sub.add_argument("--format", choices=("auto", "text", "latex"), default="auto")
        sub.add_argument("--report", help="Write the full JSON report to this path")
        if command != "apply":
            sub.add_argument("--glossary")
            sub.add_argument("--language", choices=("en", "zh"), default="en")
        if command == "check":
            sub.add_argument("candidate")
            sub.add_argument("--candidate-format", choices=("auto", "text", "latex"), default="auto")
            sub.add_argument("--profile", choices=("strict", "aligned"), default="strict")
        if command == "apply":
            sub.add_argument("plan")
            sub.add_argument("--output", required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    document = Document(args.source, args.format)
    profile = getattr(args, "profile", "strict")
    anchors = collect_anchors(document.text, document.format, profile)
    if args.command == "apply":
        ensure_separate([args.source, args.plan], [args.output, args.report])
        return apply_plan(args, document, anchors)
    candidate_path = getattr(args, "candidate", None)
    ensure_separate([args.source, candidate_path, args.glossary], [args.report])
    report = {"source": document.identity(), "anchor_counts": dict(Counter(anchor.kind for anchor in anchors))}
    subject = document
    if args.command == "inspect":
        report.update(status="inspected", anchors=[document.record(anchor) for anchor in anchors])
    else:
        subject = Document(candidate_path, args.candidate_format)
        other = collect_anchors(subject.text, subject.format, profile)
        items = public_differences(differences(anchors, other), document, subject)
        issues = [item for item in items if profile == "strict" or item["issue"] != "anchor_order_changed"]
        report.update(
            status="issues" if issues else "passed", candidate=subject.identity(),
            profile=profile, issues=issues, review_flags=[item for item in items if item not in issues],
            semantic_review="required",
        )
        if profile == "strict" and document.format == subject.format:
            report["semantic_review_cues"] = cue_changes(document.text, subject.text, document.format)
    if args.glossary:
        report["terminology"] = use_glossary(subject, args.glossary, args.language)
    show(report, args.report)
    return 1 if report["status"] == "issues" else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (OSError, UnicodeError, ValueError) as error:
        print(json.dumps({"status": "execution_failed", "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
