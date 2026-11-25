"""Local evidence-preserving PDF parser. Python 3.10+. No network access needed."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

VERSION = "1.1.0"


def rect(obj):
    return (obj["x0"], obj["top"], obj["x1"], obj["bottom"])


def box(values):
    return tuple(round(float(value), 3) for value in values)


def relative(values, origin):
    x0, top, x1, bottom = values
    return box((x0 - origin[0], top - origin[1], x1 - origin[0], bottom - origin[1]))


def absolute(values, origin):
    x0, top, x1, bottom = values
    return (x0 + origin[0], top + origin[1], x1 + origin[0], bottom + origin[1])


def bounds(items):
    return box((min(v[0] for v in items), min(v[1] for v in items),
                max(v[2] for v in items), max(v[3] for v in items)))


def center(b):
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def inside(point, b):
    return b[0] <= point[0] < b[2] and b[1] <= point[1] < b[3]


def area(b):
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def intersection(a, b):
    return area((max(a[0], b[0]), max(a[1], b[1]),
                 min(a[2], b[2]), min(a[3], b[3])))


def contains(outer, inner, tolerance=0.1):
    return (inner[0] >= outer[0] - tolerance and inner[1] >= outer[1] - tolerance
            and inner[2] <= outer[2] + tolerance and inner[3] <= outer[3] + tolerance)


def write_json(path, data):
    # Preserve Unicode and escape even malformed surrogate code points.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", errors="backslashreplace",
                                     dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        staged = Path(stream.name)
        try:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
        except BaseException:
            stream.close()
            staged.unlink(missing_ok=True)
            raise
    try:
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def time_left(args):
    if args.deadline is None:
        return math.inf
    return args.deadline - time.monotonic()


def dependency_versions():
    result = {}
    for package in ("pdfplumber", "pdfminer.six", "pypdf", "pypdfium2", "Pillow"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = None
    return result


class ProtectedPDFError(Exception):
    """The input needs an authorized password or supported encryption handler."""


def pdf_catalog(path, password):
    """PDF catalog labels are distinct from printed page numbers."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt(password or "")
            except Exception as exc:
                raise ProtectedPDFError(f"Encrypted PDF could not be opened: {type(exc).__name__}") from exc
            if not unlocked:
                raise ProtectedPDFError("Encrypted PDF: supply a valid authorized password with --password-env")
        labels = (list(reader.page_labels)
                  if reader.trailer["/Root"].get("/PageLabels") is not None else [])
        outline = []

        def add_outline(items, depth):
            for item in items:
                if isinstance(item, list):
                    add_outline(item, depth + 1)
                else:
                    try:
                        number = reader.get_destination_page_number(item)
                        number = number + 1 if number is not None and number >= 0 else None
                    except (ValueError, TypeError, KeyError):
                        number = None
                    outline.append({"title": str(item.title), "depth": depth, "physical_page": number})

        try:
            add_outline(reader.outline, 1)
        except Exception as exc:
            return labels, outline, f"PDF outline incomplete ({type(exc).__name__})"
        return labels, outline, None
    except ProtectedPDFError:
        raise
    except Exception as exc:
        return [], [], f"PDF catalog unavailable ({type(exc).__name__})"


def read_glyphs(page, origin, viewport):
    glyphs, active, seen = [], [], {}
    for index, char in enumerate(page.chars, 1):
        b = relative(rect(char), origin)
        glyph = {
            "i": index, "text": char.get("text", ""), "bbox": b,
            "font": char.get("fontname"), "size": round(float(char.get("size", 0)), 3),
            "upright": bool(char.get("upright", True)), "status": "active",
        }
        if char.get("matrix"):
            glyph["matrix"] = [round(float(v), 5) for v in char["matrix"]]
        key = (glyph["text"], b, glyph["font"],
               glyph["size"], glyph["upright"])
        if not inside(center(b), viewport):
            glyph["status"] = "outside_cropbox"
        elif key in seen:
            glyph["status"] = "duplicate"
            glyph["duplicate_of"] = seen[key]
        else:
            seen[key] = index
            active.append(glyph)
        glyphs.append(glyph)
    return glyphs, active


def body_scale(glyphs):
    sizes = [g["size"] for g in glyphs if g["text"].strip() and g["size"] > 0]
    return max(4.0, statistics.median(sizes) if sizes else 10.0)


def is_cjk(s):
    if not s:
        return False
    cp = ord(s[0])
    return (0x2E80 <= cp <= 0x9FFF or 0xAC00 <= cp <= 0xD7AF
            or 0xF900 <= cp <= 0xFAFF or 0x20000 <= cp <= 0x3134F)


def fragment(glyphs, rotated=False):
    ordered = sorted(glyphs, key=lambda g: g["i"] if rotated else (g["bbox"][0], g["i"]))
    parts, inferred = [], []
    previous = None
    for g in ordered:
        raw = g["text"]
        if previous is not None and not rotated and previous["text"] and raw:
            gap = g["bbox"][0] - previous["bbox"][2]
            a, b = previous["text"][-1], raw[0]
            threshold = max(1.0, 0.16 * min(g["size"], previous["size"]))
            if (gap > threshold and not a.isspace() and not b.isspace()
                    and not (is_cjk(a) and is_cjk(b))):
                parts.append(" ")
                inferred.append(previous["i"])
        parts.append(raw)
        previous = g
    return {
        "kind": "text", "bbox": bounds([g["bbox"] for g in ordered]),
        "text": "".join(parts), "glyph_ids": [g["i"] for g in ordered],
        "inferred_space_after": inferred,
        "font_size": round(statistics.median(g["size"] for g in ordered), 3),
        "fonts": sorted({g["font"] for g in ordered if g["font"]}),
        "source": "text_layer", "role": "rotated_text_candidate" if rotated else "body",
    }


def line_fragments(glyphs, scale, width, split_gaps=True):
    """Build baseline-local fragments before deciding the column order."""
    rows = []
    horizontal = [g for g in glyphs if g["upright"]]
    for g in sorted(horizontal, key=lambda g: (center(g["bbox"])[1], g["bbox"][0])):
        y = center(g["bbox"])[1]
        height = max(1, g["bbox"][3] - g["bbox"][1])
        candidates = []
        for row in rows[-8:]:
            distance = abs(y - row["y"])
            larger = max(height, row["height"])
            small_script = min(height, row["height"]) < 0.78 * larger
            limit = max(1.5, (0.51 if small_script else 0.30) * larger)
            if distance <= limit:
                candidates.append((distance / larger, row))
        if candidates:
            row = min(candidates, key=lambda item: item[0])[1]
            row["glyphs"].append(g)
            if height > row["height"]:
                row["y"], row["height"] = y, height
        else:
            rows.append({"y": y, "height": height, "glyphs": [g]})
    output = []
    for row in sorted(rows, key=lambda r: r["y"]):
        run, right = [], None
        # Keep narrower gutters as independent fragments. The cut detector below
        # still requires repeated, aligned evidence before changing reading order.
        gap_limit = (max(4.8, 0.55 * min(scale, row["height"]), 0.008 * width)
                     if split_gaps else float("inf"))
        for g in sorted(row["glyphs"], key=lambda g: (g["bbox"][0], g["i"])):
            # An explicit normal-width space is part of its line, including
            # monospaced fonts; only a genuinely wide blank is its own fragment.
            wide_blank = (split_gaps and g["text"].isspace()
                          and g["bbox"][2] - g["bbox"][0] > max(gap_limit, 1.3 * g["size"]))
            if run and (g["bbox"][0] - right > gap_limit or wide_blank):
                output.append(fragment(run))
                run, right = [], None
            if wide_blank:
                output.append(fragment([g]))
                continue
            run.append(g)
            right = max(g["bbox"][2], right) if right is not None else g["bbox"][2]
        if run:
            output.append(fragment(run))
    # Preserve vertical or rotated text without assuming its reading direction.
    rotated = sorted((g for g in glyphs if not g["upright"]),
                     key=lambda g: (center(g["bbox"])[0], g["bbox"][1]))
    groups = []
    for g in rotated:
        x, _ = center(g["bbox"])
        if groups and abs(x - center(groups[-1][0]["bbox"])[0]) <= scale * 0.5:
            groups[-1].append(g)
        else:
            groups.append([g])
    for group in groups:
        run = []
        for g in sorted(group, key=lambda g: g["bbox"][1]):
            if run and g["bbox"][1] - run[-1]["bbox"][3] > scale * 1.8:
                output.append(fragment(run, rotated=True))
                run = []
            run.append(g)
        if run:
            output.append(fragment(run, rotated=True))
    return output


CAPTION = re.compile(
    r"^(?P<label>(?:fig(?:ure)?\.?|table(?:au)?|scheme|chart|图|表)\s*"
    r"(?:[Ss]?\d+(?:[.\-]\d+)*[A-Za-z]?|[IVX]+))(?=\s|[.:：、()\-]|$)", re.I)
NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+){0,5})[.)、]?(?:\s+|(?=[\u3400-\u9fff]))\S")
HEADING = re.compile(
    r"^(?:abstract|summary|introduction|background|(?:materials and )?methods|"
    r"results(?: and discussion)?|discussion|conclusions?|references|bibliography|"
    r"acknowledg(?:e)?ments?|appendix(?:\s+[A-Z0-9])?|supplementary (?:materials?|information)|"
    r"résumé|introduction|méthodes|résultats|conclusion|bibliographie|"
    r"摘要|关键词|引言|绪论|研究方法|材料与方法|实验方法|结果与讨论|讨论|结论|参考文献|致谢|附录|补充材料)"
    r"[\s:：.]*$", re.I)


def classify(node, page_no, height, scale):
    if node["role"] == "rotated_text_candidate":
        return
    text = node["text"].strip()
    b = node["bbox"]
    if not text:
        node["role"] = "spacing"
        return
    cap = CAPTION.match(text)
    if cap:
        node["role"] = ("table_caption_candidate" if re.match(r"table|表", cap.group("label"), re.I)
                        else "figure_caption_candidate")
        node["label_candidate"] = cap.group("label")
    elif (b[3] > height * 0.91 or b[1] < height * 0.055) and re.fullmatch(
            r"(?:\d+|[ivxlcdm]+|[Ss]\d+)", text, re.I):
        node["role"] = "page_number_candidate"
    elif re.search(r"[=≈≠≤≥∑∫∂√∞±×÷∇]", text) and (
            re.search(r"\([^()]*\d+\)\s*$", text)
            or (len(text) < 90 and len(re.findall(r"[A-Za-z]{3,}", text)) <= 3)):
        node["role"] = "formula_candidate"
    elif re.fullmatch(r"\([A-Za-z]?\d+(?:\.\d+)*[a-z]?\)", text):
        node["role"] = "equation_number_candidate"
    elif HEADING.fullmatch(text):
        node["role"], node["level_candidate"] = "heading_candidate", 1
    elif lead := re.match(r"^(abstract|summary|résumé|摘要|关键词)\s*[.:：]\s*\S", text, re.I):
        node["role"], node["heading_lead_candidate"] = "section_lead_candidate", lead.group(1)
    elif (match := NUMBERED_HEADING.match(text)) and len(text) < 100 and (
            node["font_size"] >= scale * 1.05 or any("bold" in f.lower() for f in node["fonts"])
            or "." in match.group(1)):
        node["role"] = "heading_candidate"
        node["level_candidate"] = match.group(1).count(".") + 1
    elif len(text) < 80 and re.match(r"^(?:第[一二三四五六七八九十百\d]+[章节篇]|[一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[）)])", text):
        node["role"] = "heading_candidate"
    elif node["font_size"] >= scale * 1.35 and b[1] < height * 0.3 and page_no == 1:
        node["role"] = "title_candidate"
    elif len(text) < 100 and any("bold" in f.lower() for f in node["fonts"]) and node["font_size"] >= scale * 1.06:
        node["role"] = "heading_candidate"
    elif b[1] > height * 0.73 and node["font_size"] <= scale * 0.83:
        node["role"] = "footnote_candidate"


def column_cut(nodes, scale, width):
    """Find a locally sustained gutter from nearby fragments."""
    lines = [n for n in nodes if n["kind"] == "text"
             and n["role"] not in ("spacing", "rotated_text_candidate", "page_number_candidate")
             and n["bbox"][2] - n["bbox"][0] >= max(27, scale * 3.0)
             and n["text"].strip()]
    lines.sort(key=lambda n: center(n["bbox"])[1])
    gutter = max(5.0, scale * 0.55, width * 0.008)
    pairs = []
    for index, first in enumerate(lines):
        a = first["bbox"]
        ay = center(a)[1]
        for second in lines[index + 1:]:
            b = second["bbox"]
            by = center(b)[1]
            if by - ay > scale * 1.60:
                break
            left, right = sorted((a, b), key=lambda v: v[0])
            if right[0] - left[2] >= gutter:
                pairs.append((left[2] + gutter * 0.35, right[0] - gutter * 0.35,
                              (ay + by) / 2, right[0]))
    if not pairs:
        return None
    step = max(2.0, scale * 0.30)
    start, end = min(p[0] for p in pairs), max(p[1] for p in pairs)
    points = {p[0] + (p[1] - p[0]) / 2 for p in pairs}
    points.update(start + step * i for i in range(int((end - start) / step) + 1))
    best = None
    for x in sorted(points):
        votes = [(lo, hi, y, right_start) for lo, hi, y, right_start in pairs if lo <= x <= hi]
        bins = sorted({round(y / (scale * 0.85)) for _, _, y, _ in votes})
        clusters = []
        for value in bins:
            if not clusters or value - clusters[-1][-1] > 4:
                clusters.append([value])
            else:
                clusters[-1].append(value)
        for cluster in clusters:
            if len(cluster) < 3:
                continue
            low = (cluster[0] - 0.75) * scale * 0.85
            high = (cluster[-1] + 0.75) * scale * 0.85
            crossing = sum(n["bbox"][0] < x < n["bbox"][2]
                           and low <= center(n["bbox"])[1] <= high for n in lines)
            if crossing > max(1, len(cluster) * 0.4):
                continue
            local_votes = [(lo, hi, y, start) for lo, hi, y, start in votes if low <= y <= high]
            # Repeated right-column starts distinguish a gutter from most
            # incidental gaps between words in a justified paragraph.
            starts = {v[3] for v in local_votes}
            alignment = max((len({round(y / (scale * 0.85)) for _, _, y, start in local_votes
                                  if abs(start - target) <= max(3.5, scale * 0.65)})
                             for target in starts), default=0)
            if alignment < max(3, math.ceil(len(cluster) * 0.55)):
                continue
            clearances = [min(x - lo, hi - x) for lo, hi, _, _ in local_votes]
            score = (alignment + (len(cluster) - alignment) * 0.5 - crossing * 1.5,
                     statistics.median(clearances))
            if best is None or score > best[0]:
                best = (score, x, len(cluster))
    return (best[1], best[2]) if best else None


def spatial_key(node):
    _, y = center(node["bbox"])
    return (y, node["bbox"][0], node["id"])


def substantive(node, scale):
    return (node["kind"] == "text" and node["role"] not in (
        "spacing", "page_number_candidate", "equation_number_candidate", "rotated_text_candidate",
        "footnote_candidate", "formula_candidate")
        and node["bbox"][2] - node["bbox"][0] >= scale * 3)


def logical_spanners(nodes, physical_spanners, x, scale):
    """Attach short headings/captions to adjacent full-width content.

    Only promote with local geometric support; these links remain candidates.
    """
    linked = []
    known = {node["id"] for node in physical_spanners}
    candidate_roles = {"heading_candidate", "title_candidate", "section_lead_candidate",
                       "table_caption_candidate", "figure_caption_candidate"}
    for node in nodes:
        if node["id"] in known or node["role"] not in candidate_roles:
            continue
        b = node["bbox"]
        for span in physical_spanners:
            sb = span["bbox"]
            gap = max(0, sb[1] - b[3], b[1] - sb[3])
            if gap > scale * 3.6:
                continue
            if node["role"] == "table_caption_candidate":
                if span["kind"] != "table" or node["id"] not in span.get("caption_candidates", []):
                    continue
            elif node["role"] in ("heading_candidate", "title_candidate", "section_lead_candidate"):
                if sb[1] < b[1] or span["role"] in ("table_candidate", "formula_candidate"):
                    continue
            opposite = [n for n in nodes if n["id"] not in known and n["id"] != node["id"]
                        and substantive(n, scale)
                        and (center(n["bbox"])[0] < x) != (center(b)[0] < x)
                        and min(b[1], sb[1]) - scale * 0.3 <= center(n["bbox"])[1]
                        <= max(b[3], sb[3]) + scale * 0.3]
            if not opposite:
                linked.append((node, span["id"]))
                break
    return linked


def reading_order(nodes, scale, width, decisions, depth=0):
    if len(nodes) < 4 or depth >= 6:
        return sorted(nodes, key=spatial_key)
    cut = column_cut(nodes, scale, width)
    if cut is None:
        return sorted(nodes, key=spatial_key)
    x, support = cut
    spanning = [n for n in nodes if n["role"] != "spacing" and n["bbox"][0] < x < n["bbox"][2]]
    linked = logical_spanners(nodes, spanning, x, scale)
    spanning.extend(node for node, _ in linked)
    span_ids = {n["id"] for n in spanning}
    rest = [n for n in nodes if n["id"] not in span_ids]
    decision = {"x": round(x, 3), "support_rows": support,
                "scope_bbox": bounds([n["bbox"] for n in nodes]),
                "spanning_ids": sorted(span_ids)}
    if linked:
        decision["logical_spanning_links"] = [{"node": n["id"], "adjacent_spanner": target}
                                               for n, target in linked]
    decisions.append(decision)

    def band_order(band):
        left = [n for n in band if center(n["bbox"])[0] < x]
        right = [n for n in band if center(n["bbox"])[0] >= x]
        left_text = [n for n in left if substantive(n, scale)]
        right_text = [n for n in right if substantive(n, scale)]
        # A prior column layout does not extend automatically into a full-width
        # equation, note or short single-column region below a spanning block.
        paired_rows = sum(any(abs(center(n["bbox"])[1] - center(r["bbox"])[1]) <= 1.6 * scale
                              for r in right_text) for n in left_text)
        if not left_text or not right_text or paired_rows < 2:
            if left_text and right_text and paired_rows:
                decision.setdefault("short_band_review_ids", []).extend(n["id"] for n in band)
            return sorted(band, key=spatial_key)
        return (reading_order(left, scale, width, decisions, depth + 1)
                + reading_order(right, scale, width, decisions, depth + 1))

    assembled = []
    span_groups = []
    for node in sorted(spanning, key=lambda n: n["bbox"][1]):
        if span_groups and node["bbox"][1] < max(n["bbox"][3] for n in span_groups[-1]):
            span_groups[-1].append(node)
        else:
            span_groups.append([node])
    for group in span_groups:
        section = bounds([n["bbox"] for n in group])
        middle = center(section)[1]
        overlap = [n["id"] for n in rest if section[1] < center(n["bbox"])[1] < section[3]]
        if overlap:
            decision.setdefault("overlap_review", []).extend(overlap)
        before = [n for n in rest if center(n["bbox"])[1] < middle]
        stop_ids = {n["id"] for n in before}
        rest = [n for n in rest if n["id"] not in stop_ids]
        assembled.extend(band_order(before))
        assembled.extend(sorted(group, key=spatial_key))
    assembled.extend(band_order(rest))
    return assembled


def validate_regions(regions, viewport):
    for i, b in enumerate(regions):
        if len(b) != 4 or not all(math.isfinite(v) for v in b) or area(b) <= 0 or not contains(viewport, b):
            raise ValueError(f"region {i + 1} is outside the visible page or has invalid coordinates")
        for other in regions[:i]:
            if intersection(b, other) > 0.1:
                raise ValueError("manual layout regions must not overlap")


def table_candidates(page, origin, viewport, regions):
    visible = page.crop(absolute(viewport, origin), strict=False) if page.bbox != page.cropbox else page
    candidates = []
    rejected = []
    for region in regions:
        if not contains(viewport, region):
            raise ValueError("table region is outside the visible page")
        cropped = page.crop(absolute(region, origin), strict=False)
        settings = {"vertical_strategy": "text", "horizontal_strategy": "text",
                    "min_words_vertical": 2, "min_words_horizontal": 1}
        try:
            candidates.extend((t, "requested_text_region") for t in cropped.find_tables(settings))
        except Exception as exc:
            rejected.append({"bbox": region, "reason": f"text_region_error:{type(exc).__name__}"})
    try:
        candidates.extend((t, "ruled_grid") for t in visible.find_tables())
    except Exception as exc:
        rejected.append({"bbox": viewport, "reason": f"ruled_table_error:{type(exc).__name__}"})
    selected = []
    for table, method in candidates:
        cells = sorted({relative(b, origin) for b in table.cells}, key=lambda b: (b[1], b[0]))
        xs = sorted({v for cell in cells for v in (cell[0], cell[2])})
        ys = sorted({v for cell in cells for v in (cell[1], cell[3])})
        if len(xs) < 3 or len(ys) < 3:
            continue
        b = relative(table.bbox, origin)
        if (len(xs) - 1) * (len(ys) - 1) > 20000:
            rejected.append({"bbox": b, "reason": "grid_too_complex_for_automatic_reconstruction"})
            continue
        if any(intersection(b, t["bbox"]) > 0.1 * min(area(b), area(t["bbox"])) for t in selected):
            rejected.append({"bbox": b, "reason": "overlapping_table_candidate"})
            continue
        selected.append({"kind": "table", "role": "table_candidate", "bbox": b,
                         "method": method, "_cell_boxes": cells, "_xs": xs, "_ys": ys})
    return sorted(selected, key=lambda t: (t["bbox"][1], t["bbox"][0])), rejected


def populate_table(table, glyphs, scale, width):
    xs, ys = table.pop("_xs"), table.pop("_ys")
    boxes = table.pop("_cell_boxes")
    grid = [[None for _ in range(len(xs) - 1)] for _ in range(len(ys) - 1)]
    cells, assigned, conflicts = [], set(), []
    for b in boxes:
        col, row = xs.index(b[0]), ys.index(b[1])
        colspan, rowspan = xs.index(b[2]) - col, ys.index(b[3]) - row
        cell_id = f"{table['id']}-r{row + 1}c{col + 1}"
        cell_glyphs = [g for g in glyphs if inside(center(g["bbox"]), b) and g["i"] not in assigned]
        ids = [g["i"] for g in cell_glyphs]
        assigned.update(ids)
        fragments = sorted(line_fragments(cell_glyphs, scale, width, split_gaps=False),
                           key=lambda f: (center(f["bbox"])[1], f["bbox"][0]))
        text = "\n".join(f["text"] for f in fragments)
        cells.append({"id": cell_id, "row": row + 1, "col": col + 1,
                      "rowspan": rowspan, "colspan": colspan, "bbox": b,
                      "text": text, "glyph_ids": ids, "lines": fragments,
                      "status": "text_layer" if text.strip() else "empty_text_layer"})
        for r in range(row, row + rowspan):
            for c in range(col, col + colspan):
                if grid[r][c] is not None:
                    conflicts.append([r + 1, c + 1])
                else:
                    grid[r][c] = cell_id
    table.update({"row_count": len(ys) - 1, "col_count": len(xs) - 1,
                  "x_edges": xs, "y_edges": ys, "grid": grid, "cells": cells,
                  "unassigned_glyph_ids": [g["i"] for g in glyphs if g["i"] not in assigned],
                  "grid_conflicts": conflicts,
                  "status": "candidate_requires_visual_check"})
    if table["method"] == "requested_text_region":
        # Text alignment often introduces blank strips between text baselines.
        # Preserve the raw grid and offer a separate, explicitly provisional view.
        indexed = {cell["id"]: cell for cell in cells}
        gap_rows = []
        for r, grid_row in enumerate(grid):
            if r == 0 or r == len(grid) - 1:
                continue
            owned = [indexed[cid] for cid in grid_row if cid is not None]
            if owned and all(not cell["text"].strip() and not cell["glyph_ids"]
                             and cell["rowspan"] == 1 for cell in owned):
                prev_has_text = any(indexed[cid]["text"].strip() for cid in grid[r - 1] if cid)
                next_has_text = any(indexed[cid]["text"].strip() for cid in grid[r + 1] if cid)
                if prev_has_text and next_has_text:
                    gap_rows.append(r + 1)
        kept = [r for r in range(1, len(grid) + 1) if r not in gap_rows]
        table["logical_view_candidate"] = {"row_count": len(kept), "raw_row_mapping": kept,
                                           "layout_gap_raw_rows": gap_rows}


def associate_captions(tables, fragments, scale):
    captions = [f for f in fragments if f["role"] == "table_caption_candidate"]
    for table in tables:
        b = table["bbox"]
        candidates = []
        for cap in captions:
            c = cap["bbox"]
            if min(b[2], c[2]) <= max(b[0], c[0]):
                continue
            distance = min(abs(b[1] - c[3]), abs(c[1] - b[3]))
            if distance < max(42, 4 * scale):
                candidates.append((distance, cap["id"]))
        table["caption_candidates"] = [cid for _, cid in sorted(candidates)]


def glyph_problem(text):
    return bool(re.search(r"\(cid:\d+\)|\ufffd|[\ud800-\udfff\ue000-\uf8ff]", text))


def graphics(page, origin, viewport, prefix):
    images, vectors = [], []
    for obj in page.images:
        b = relative(rect(obj), origin)
        if intersection(b, viewport) > 0:
            images.append({"id": f"{prefix}-im{len(images) + 1:03d}", "bbox": b,
                           "kind": "raster_region", "status": "inspect_image_or_figure"})
    for name, objects in (("line", page.lines), ("rectangle", page.rects), ("curve", page.curves)):
        for obj in objects:
            b = relative(rect(obj), origin)
            if b[2] >= 0 and b[0] <= viewport[2] and b[3] >= 0 and b[1] <= viewport[3]:
                vectors.append({"id": f"{prefix}-v{len(vectors) + 1:04d}", "kind": name, "bbox": b})
    return images, vectors


def render_page(page, out, prefix, dpi, region=None):
    if region is None:
        region = (0, 0, page.cropbox[2] - page.cropbox[0], page.cropbox[3] - page.cropbox[1])
        target_page = page
    else:
        target_page = page.crop(absolute(region, page.cropbox[:2]), strict=False)
    rendered = target_page.to_image(resolution=dpi, antialias=True)
    target = out / "images" / f"{prefix}.png"
    target.parent.mkdir(exist_ok=True)
    rendered.original.save(target, format="PNG")
    return {"path": target.relative_to(out).as_posix(),
            "pixel_size": list(rendered.original.size), "dpi": dpi,
            "pdf_bbox": box(rendered.bbox), "page_relative_bbox": box(region)}


def run_ocr(image, out, args, executable, psm):
    remaining = time_left(args)
    if remaining <= 0:
        return {"status": "pending", "message": "OCR deferred at case deadline", "words": []}
    command = [executable, str(out / image["path"]), "stdout", "-l", args.lang,
               "--psm", str(psm), "tsv"]
    result = subprocess.run(command, capture_output=True, encoding="utf-8",
                            errors="replace", timeout=min(args.ocr_timeout, remaining), check=False)
    if result.returncode:
        return {"status": "failed", "message": result.stderr.strip()[:300], "words": []}
    width, height = image["pixel_size"]
    x0, top, x1, bottom = image["page_relative_bbox"]
    x_scale, y_scale = (x1 - x0) / width, (bottom - top) / height
    words, groups = [], defaultdict(list)
    for item in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
        if item.get("level") != "5":
            continue
        text = item.get("text", "")
        if not text.strip():
            continue
        x, y, w, h = (float(item[k]) for k in ("left", "top", "width", "height"))
        b = box((x0 + x * x_scale, top + y * y_scale,
                 x0 + (x + w) * x_scale, top + (y + h) * y_scale))
        word = {"i": len(words) + 1, "text": text, "bbox": b,
                "confidence": float(item["conf"])}
        words.append(word)
        key = tuple(item[k] for k in ("block_num", "par_num", "line_num"))
        groups[key].append(word)
    lines = [{"bbox": bounds([w["bbox"] for w in ws]),
              "text": " ".join(w["text"] for w in ws),
              "word_ids": [w["i"] for w in ws],
              "minimum_word_confidence": min(w["confidence"] for w in ws)}
             for ws in groups.values()]
    return {"status": "candidate_requires_visual_check" if words else "no_text_recognized",
            "engine": "tesseract", "language": args.lang, "psm": psm, "words": words, "lines": lines}


def region_ocr(page, regions, viewport, prefix, out, args, executable, reason):
    words, lines, records = [], [], []
    for index, region in enumerate(regions, 1):
        if not contains(viewport, region):
            raise ValueError("OCR region is outside the visible page")
        region_id = f"{prefix}-ocr{index:03d}"
        record = {"id": region_id, "bbox": region, "image": None}
        try:
            record["image"] = render_page(page, out, region_id, max(args.dpi, 300), region)
            if reason:
                result = {"status": "pending", "message": reason}
            else:
                result = run_ocr(record["image"], out, args, executable, 6)
            offset = len(words)
            for word in result.get("words", []):
                word["i"] += offset
                word["region_id"] = region_id
                words.append(word)
            for line in result.get("lines", []):
                line["word_ids"] = [i + offset for i in line["word_ids"]]
                line["region_id"] = region_id
                lines.append(line)
            record.update({k: v for k, v in result.items() if k not in ("words", "lines")})
        except Exception as exc:
            record.update({"status": "failed", "message": f"{type(exc).__name__}: {str(exc)[:200]}"})
        records.append(record)
    status = ("candidate_requires_visual_check" if words and all(
        r["status"] == "candidate_requires_visual_check" for r in records)
        else ("partial" if words else "pending_or_unreadable"))
    return {"status": status, "mode": "regions", "regions": records,
            "words": words, "lines": lines}


def parse_page(page, label, args, out, ocr_executable, ocr_reason):
    no = page.page_number
    prefix = f"p{no:04d}"
    origin = page.cropbox[:2]
    width, height = page.cropbox[2] - origin[0], page.cropbox[3] - origin[1]
    viewport = box((0, 0, width, height))
    glyphs, active = read_glyphs(page, origin, viewport)
    scale = body_scale(active)
    warnings = []
    try:
        tables, rejected = table_candidates(page, origin, viewport, args.table_regions.get(no, []))
    except Exception as exc:
        tables, rejected = [], []
        warnings.append(f"table_detection_failed:{type(exc).__name__}:{str(exc)[:160]}")
    if rejected:
        warnings.append("table_candidates_overlap_or_grid_requires_manual_review")
    by_table = defaultdict(list)
    body = []
    for g in active:
        owner = next((t for t in tables if inside(center(g["bbox"]), t["bbox"])), None)
        if owner is None:
            body.append(g)
        else:
            by_table[id(owner)].append(g)
    for index, table in enumerate(tables, 1):
        table["id"] = f"{prefix}-t{index:03d}"
        populate_table(table, by_table[id(table)], scale, width)
    regions = args.layout.get(str(no))
    groups = []
    uncovered = []
    if regions:
        validate_regions(regions, viewport)
        buckets = [[] for _ in regions]
        for g in body:
            index = next((i for i, b in enumerate(regions) if inside(center(g["bbox"]), b)), None)
            if index is None:
                uncovered.append(g)
            else:
                buckets[index].append(g)
        groups = [line_fragments(bucket, scale, width) for bucket in buckets]
        groups.append(line_fragments(uncovered, scale, width))
        if uncovered:
            warnings.append("manual_regions_do_not_cover_all_visible_glyphs")
    else:
        groups = [line_fragments(body, scale, width)]
    fragments = [f for group in groups for f in group]
    for index, f in enumerate(sorted(fragments, key=lambda f: (f["bbox"][1], f["bbox"][0])), 1):
        f["id"] = f"{prefix}-b{index:04d}"
        classify(f, no, height, scale)
    associate_captions(tables, fragments, scale)
    if regions:
        for table in tables:
            index = next((i for i, b in enumerate(regions) if inside(center(table["bbox"]), b)), None)
            if index is None:
                groups[-1].append(table)
                warnings.append("table_outside_manual_regions")
            else:
                groups[index].append(table)
                if not contains(regions[index], table["bbox"]):
                    warnings.append("table_crosses_manual_region_boundary")
        order = [node for group in groups for node in sorted(group, key=spatial_key)]
        decisions = []
        layout_method = "manual_region_order"
    else:
        decisions = []
        order = reading_order(fragments + tables, scale, width, decisions)
        layout_method = "geometric_gutter_bands" if decisions else "vertical_order"
    if decisions:
        warnings.append("verify_column_flow_against_page_image")
    if any(d.get("overlap_review") for d in decisions):
        warnings.append("spanning_element_overlaps_column_content")
    if any(d.get("short_band_review_ids") for d in decisions):
        warnings.append("short_column_band_requires_visual_order_check")
    if tables:
        warnings.append("table_grid_and_cells_require_visual_check")
    images, vectors = graphics(page, origin, viewport, prefix)
    for obj in images:
        obj["text_layer_glyphs"] = sum(inside(center(g["bbox"]), obj["bbox"]) and bool(g["text"].strip())
                                      for g in active)
    raster_area = max((intersection(i["bbox"], viewport) for i in images), default=0)
    raster_fraction = raster_area / max(1, area(viewport))
    meaningful = [g for g in active if g["text"].strip()]
    broken = [g["i"] for g in meaningful if glyph_problem(g["text"])]
    if broken:
        warnings.append("unmapped_or_suspicious_glyphs")
    if any(f["role"] == "rotated_text_candidate" for f in fragments):
        warnings.append("rotated_or_vertical_text_requires_visual_check")
    if any(re.search(r"[\u0590-\u08ff]", g["text"]) for g in meaningful):
        warnings.append("right_to_left_or_complex_script_requires_visual_order_check")
    if images:
        warnings.append("raster_regions_require_figure_or_scan_check")
    ocr_candidate = (not meaningful or bool(broken) or (len(meaningful) < 80 and raster_fraction > 0.20)
                     or raster_fraction > 0.65)
    if not meaningful:
        warnings.append("no_visible_text_layer_check_scan_blank_or_vector_text")
    elif ocr_candidate:
        warnings.append("possible_incomplete_or_unreliable_text_layer")
    refs = [i for f in fragments for i in f["glyph_ids"]]
    for table in tables:
        refs.extend(i for cell in table["cells"] for i in cell["glyph_ids"])
        refs.extend(table["unassigned_glyph_ids"])
        if table["unassigned_glyph_ids"] or table["grid_conflicts"]:
            warnings.append("table_unassigned_glyphs_or_grid_conflicts")
    counts = Counter(refs)
    expected = {g["i"] for g in active}
    missing = sorted(expected - counts.keys())
    extra = sorted(counts.keys() - expected)
    duplicated = sorted(i for i, count in counts.items() if count > 1)
    flow_ids = [n["id"] for n in order]
    all_ids = {n["id"] for n in fragments + tables}
    balanced_flow = len(flow_ids) == len(all_ids) and set(flow_ids) == all_ids
    if missing or extra or duplicated or not balanced_flow:
        warnings.append("internal_accounting_mismatch")
    formulas = any(f["role"] in ("formula_candidate", "equation_number_candidate") for f in fragments)
    if formulas:
        warnings.append("formula_structure_and_symbols_require_visual_check")
    need_image = args.render == "all" or (args.render == "auto" and (
        bool(warnings) or formulas or bool(vectors and not meaningful)))
    image = None
    if need_image or (ocr_candidate and args.ocr != "off" and args.lang and ocr_executable):
        try:
            image = render_page(page, out, prefix, max(args.dpi, 250) if ocr_candidate else args.dpi)
        except Exception as exc:
            warnings.append(f"page_render_failed:{type(exc).__name__}:{str(exc)[:160]}")
    ocr = {"status": "not_needed", "words": [], "lines": []}
    requested_ocr = args.ocr_regions.get(no, [])
    if requested_ocr:
        ocr = region_ocr(page, requested_ocr, viewport, prefix, out, args,
                         ocr_executable, ocr_reason)
        warnings.append("specified_ocr_regions_require_visual_review")
    elif ocr_candidate:
        if args.ocr == "off":
            ocr["status"] = "disabled"
        elif ocr_reason:
            ocr.update({"status": "pending", "message": ocr_reason})
        elif not image:
            ocr["status"] = "render_unavailable"
        else:
            try:
                ocr = run_ocr(image, out, args, ocr_executable, 3)
            except Exception as exc:
                ocr.update({"status": "failed", "message": f"{type(exc).__name__}: {str(exc)[:200]}"})
        warnings.append("ocr_or_visual_review_required")
    for index, line in enumerate(ocr.get("lines", []), 1):
        line["id"] = f"{prefix}-o{index:04d}"
    return {
        "physical_page": no, "pdf_page_label": label, "printed_page": None,
        "width_pt": width, "height_pt": height, "rotation": page.rotation,
        "cropbox_pdfplumber": box(page.cropbox), "coordinate_system": "displayed CropBox, top-left, points",
        "layout_method": layout_method, "layout_regions": regions, "column_decisions": decisions,
        "glyphs": glyphs, "blocks": sorted(fragments, key=lambda f: f["id"]),
        "tables": tables, "rejected_table_candidates": rejected, "flow": flow_ids,
        "images": images, "vector_objects": vectors, "page_image": image, "ocr": ocr,
        "warnings": list(dict.fromkeys(warnings)),
        "audit": {"raw_glyphs": len(glyphs), "active_glyphs": len(active),
                  "duplicate_glyphs": sum(g["status"] == "duplicate" for g in glyphs),
                  "outside_cropbox": sum(g["status"] == "outside_cropbox" for g in glyphs),
                  "unmapped_glyph_ids": broken, "unassigned_glyph_ids": missing,
                  "unexpected_glyph_ids": extra, "multiply_assigned_glyph_ids": duplicated,
                  "balanced_flow": balanced_flow},
    }


def margin_key(block, height):
    b = block["bbox"]
    text = block["text"].strip()
    if not text or len(text) > 160 or block["role"] != "body":
        return None
    if b[1] < height * 0.075:
        band = "header_candidate"
    elif b[3] > height * 0.93:
        band = "footer_candidate"
    else:
        return None
    return (band, text, round(b[1] / height, 2), round(block["font_size"], 1))


def block_map(page):
    return {n["id"]: n for n in page["blocks"] + page["tables"]}


def literal(text):
    text = text.replace("\\", "\\\\")
    return re.sub(r"([\x60*_{}\[\]<>|~])", r"\\\1", text)


def write_table(stream, node):
    # HTML preserves row/column spans without assuming the first row is a header.
    placements = {(c["row"], c["col"]): c for c in node["cells"]}
    stream.write(f"\n**{node['id']}** (table candidate; cell coordinates and IDs in page JSON)\n\n")
    logical = node.get("logical_view_candidate")
    display_rows = logical["raw_row_mapping"] if logical else list(range(1, node["row_count"] + 1))
    if logical and logical["layout_gap_raw_rows"]:
        stream.write(f"Text-alignment whitespace rows (candidate, retained in JSON): {logical['layout_gap_raw_rows']}\n\n")
    stream.write("<table>\n")
    for r in display_rows:
        row = node["grid"][r - 1]
        stream.write("  <tr>\n")
        for c, cell_id in enumerate(row, 1):
            cell = placements.get((r, c))
            if cell is None:
                if cell_id is None:
                    stream.write("    <td>[UNRESOLVED]</td>\n")
                continue
            spans = ""
            if cell["rowspan"] > 1:
                spans += f' rowspan="{cell["rowspan"]}"'
            if cell["colspan"] > 1:
                spans += f' colspan="{cell["colspan"]}"'
            value = html.escape(cell["text"]).replace("\n", "<br>")
            stream.write(f'    <td{spans} title="{cell["id"]}">{value or "[EMPTY TEXT LAYER]"}</td>\n')
        stream.write("  </tr>\n")
    stream.write("</table>\n\n")
    if node["unassigned_glyph_ids"]:
        stream.write("[UNASSIGNED TABLE GLYPHS: inspect page JSON]\n\n")
    if node["grid_conflicts"]:
        stream.write("[CONFLICTING CELL GEOMETRY: inspect page JSON]\n\n")


def write_markdown(stream, page, repeated):
    no = page["physical_page"]
    label = page.get("pdf_page_label")
    stream.write(f"\n## PDF page {no}" + (f" (PDF label {literal(str(label))})" if label is not None else "") + "\n\n")
    if page.get("error"):
        stream.write(f"[UNREADABLE PAGE: {literal(page['error'])}]\n")
        if page.get("page_image"):
            stream.write(f"Page image: {page['page_image']['path']}\n")
        return
    if page["warnings"]:
        stream.write("Review: " + ", ".join(page["warnings"]) + "\n\n")
    if page["page_image"]:
        stream.write(f"Page image: {page['page_image']['path']}\n\n")
    mapped = block_map(page)
    auxiliary = []
    for node_id in page["flow"]:
        node = mapped[node_id]
        if node["kind"] == "table":
            write_table(stream, node)
            continue
        if margin_key(node, page["height_pt"]) in repeated or node["role"] == "page_number_candidate":
            auxiliary.append(node)
        else:
            stream.write(f"[{node_id} | {node['role']}] {literal(node['text'])}\n\n")
    if auxiliary:
        stream.write("### Marginal text candidates (retained)\n\n")
        for node in auxiliary:
            stream.write(f"[{node['id']}] {literal(node['text'])}\n\n")
    if page["images"]:
        stream.write("### Raster regions (inspect page image)\n\n")
        for obj in page["images"]:
            stream.write(f"- [{obj['id']}] bbox {obj['bbox']}\n")
    if page["ocr"]["status"] != "not_needed":
        stream.write(f"\n### OCR evidence (separate source; {page['ocr']['status']})\n\n")
        if not page["ocr"].get("lines"):
            stream.write("[READ PAGE IMAGE OR REPORT UNREADABLE CONTENT]\n")
        for region in page["ocr"].get("regions", []):
            stream.write(f"[{region['id']}] bbox {region['bbox']}; {region['status']}\n\n")
            if region["image"]:
                stream.write(f"Region image: {region['image']['path']}\n\n")
        for line in page["ocr"].get("lines", []):
            stream.write(f"[{line['id']}] {literal(line['text'])}\n\n")


def parse_page_selection(spec, page_count):
    if not spec:
        return list(range(1, page_count + 1))
    selected = set()
    for item in spec.split(","):
        start, sep, end = item.strip().partition("-")
        first, last = int(start), int(end) if sep and end else (page_count if sep else int(start))
        if first < 1 or last < first or last > page_count:
            raise ValueError(f"invalid page range: {item}; PDF has {page_count} pages")
        selected.update(range(first, last + 1))
    return sorted(selected)


def ocr_setup(args):
    if args.ocr == "off":
        return None, "OCR disabled"
    if not args.lang:
        return None, "OCR language not selected; inspect page image and supply --lang"
    executable = shutil.which("tesseract")
    if not executable:
        return None, "Local Tesseract executable unavailable; inspect page image"
    remaining = time_left(args)
    if remaining <= 0:
        return None, "OCR deferred at case deadline"
    try:
        process = subprocess.run([executable, "--list-langs"], capture_output=True,
                                 encoding="utf-8", errors="replace", timeout=min(20, remaining), check=True)
        available = set(process.stdout.splitlines())
        missing = set(args.lang.split("+")) - available
        if missing:
            return None, f"Missing OCR language data: {', '.join(sorted(missing))}"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"OCR language check failed: {type(exc).__name__}"
    return executable, None


def parse_region_arguments(values, option):
    result = defaultdict(list)
    for value in values:
        page, coordinates = value.split(":", 1)
        b = tuple(float(v) for v in coordinates.split(","))
        if int(page) < 1 or len(b) != 4 or not all(math.isfinite(v) for v in b) or area(b) <= 0:
            raise ValueError(f"invalid {option}")
        result[int(page)].append(b)
    return result


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pdf", type=Path, help="local source PDF (read-only)")
    p.add_argument("--out", type=Path, required=True, help="new or empty result directory")
    p.add_argument("--pages", help="one-based physical pages, e.g. 1-3,7,12-")
    p.add_argument("--render", choices=["auto", "all", "none"], default="auto")
    p.add_argument("--dpi", type=int, default=160, help="render resolution; OCR pages at least 250 DPI")
    p.add_argument("--ocr", choices=["auto", "off"], default="auto")
    p.add_argument("--lang", help="installed Tesseract language(s), e.g. eng+chi_sim")
    p.add_argument("--ocr-timeout", type=int, default=120, help="seconds per OCR page")
    p.add_argument("--time-budget", type=float, help="stop starting new pages and OCR after this many seconds")
    p.add_argument("--password-env", help="environment variable containing an authorized PDF password")
    p.add_argument("--layout", type=Path, help="JSON {physical_page: [ordered [x0,top,x1,bottom] regions]}")
    p.add_argument("--table-region", action="append", default=[], metavar="PAGE:X0,TOP,X1,BOTTOM",
                   help="detect borderless table in a visually verified crop; repeat as needed")
    p.add_argument("--ocr-region", action="append", default=[], metavar="PAGE:X0,TOP,X1,BOTTOM",
                   help="render and OCR a visually verified region; repeat as needed")
    p.add_argument("--version", action="version", version=VERSION)
    args = p.parse_args()
    if not 72 <= args.dpi <= 450 or args.ocr_timeout <= 0:
        p.error("DPI must be 72-450 and OCR timeout must be positive")
    if args.time_budget is not None and (not math.isfinite(args.time_budget) or args.time_budget <= 0):
        p.error("Time budget must be a positive finite number")
    args.deadline = None if args.time_budget is None else time.monotonic() + args.time_budget
    try:
        args.layout = json.loads(args.layout.read_text(encoding="utf-8-sig")) if args.layout else {}
        if not isinstance(args.layout, dict):
            raise ValueError("--layout must contain a JSON object")
        for key, regions in args.layout.items():
            if int(key) < 1 or str(int(key)) != key or not isinstance(regions, list) or not regions:
                raise ValueError("layout keys must be positive physical page numbers with non-empty region lists")
            args.layout[key] = [tuple(float(v) for v in b) for b in regions]
        args.table_regions = parse_region_arguments(args.table_region, "--table-region")
        args.ocr_regions = parse_region_arguments(args.ocr_region, "--ocr-region")
    except (TypeError, ValueError, OSError) as exc:
        p.error(str(exc))
    return args


def main():
    args = arguments()
    try:
        import pdfplumber
    except ImportError:
        print("Missing pdfplumber; see SKILL.md for dependencies.", file=sys.stderr)
        return 2
    source, out = args.pdf.resolve(), args.out.resolve()
    if not source.is_file():
        print(f"Source PDF not found: {source}", file=sys.stderr)
        return 2
    try:
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise ValueError("Output must be a new or empty directory; preserve previous runs.")
        out.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    password = os.environ.get(args.password_env) if args.password_env else None
    if args.password_env and password is None:
        print("Authorized password environment variable is missing.", file=sys.stderr)
        return 2
    start = time.perf_counter()
    manifest = {"schema": "research-pdf-evidence/1", "parser_version": VERSION,
                "source": {"file": source.name, "sha256": file_hash(source), "bytes": source.stat().st_size},
                "dependencies": dependency_versions(), "status": "started", "pages": [],
                "options": {"pages": args.pages, "render": args.render, "dpi": args.dpi,
                            "ocr": args.ocr, "lang": args.lang, "ocr_timeout": args.ocr_timeout,
                            "time_budget": args.time_budget, "layout": args.layout,
                            "table_regions": dict(args.table_regions), "ocr_regions": dict(args.ocr_regions)},
                "warnings": []}
    manifest_path = out / "manifest.json"
    write_json(manifest_path, manifest)
    try:
        labels, outline, label_issue = pdf_catalog(source, password)
        manifest["outline"] = outline
        if label_issue:
            manifest["warnings"].append(label_issue)
        executable, ocr_reason = ocr_setup(args)
        with pdfplumber.open(source, password=password, unicode_norm=None) as pdf:
            page_count = len(pdf.pages)
            manifest["page_count"] = page_count
            selected = parse_page_selection(args.pages, page_count)
            manifest["selected_pages"] = selected
            invalid_overrides = (set(map(int, args.layout)) | set(args.table_regions)
                                 | set(args.ocr_regions)) - set(selected)
            if invalid_overrides:
                raise ValueError(f"Override page(s) not selected: {sorted(invalid_overrides)}")
            manifest["metadata"] = {str(k): str(v) for k, v in pdf.metadata.items()}
            pages_dir = out / "pages"
            pages_dir.mkdir()
            margins = Counter()
            manifest["heading_candidates"] = []
            manifest["caption_candidates"] = []
            for no in selected:
                if time_left(args) <= 0:
                    manifest["warnings"].append("time_budget_exhausted_before_all_pages")
                    break
                page = pdf.pages[no - 1]
                prefix = f"p{no:04d}"
                label = labels[no - 1] if no <= len(labels) else None
                try:
                    record = parse_page(page, label, args, out, executable, ocr_reason)
                    for f in record["blocks"]:
                        if f["role"] in ("heading_candidate", "title_candidate", "section_lead_candidate"):
                            manifest["heading_candidates"].append({key: f[key] for key in (
                                "id", "bbox", "text", "role", "level_candidate", "heading_lead_candidate") if key in f})
                        elif f["role"] in ("figure_caption_candidate", "table_caption_candidate"):
                            manifest["caption_candidates"].append({key: f[key] for key in (
                                "id", "bbox", "text", "role", "label_candidate") if key in f})
                    margins.update({key for f in record["blocks"]
                                    if (key := margin_key(f, record["height_pt"])) is not None})
                    mismatch = (record["audit"]["unassigned_glyph_ids"] or record["audit"]["unexpected_glyph_ids"]
                                or record["audit"]["multiply_assigned_glyph_ids"] or not record["audit"]["balanced_flow"])
                    status = "accounting_error" if mismatch else ("review_required" if record["warnings"] else "extracted")
                except Exception as exc:
                    record = {"physical_page": no, "pdf_page_label": label,
                              "error": f"{type(exc).__name__}: {str(exc)[:300]}", "page_image": None}
                    try:
                        if args.render != "none":
                            record["page_image"] = render_page(page, out, prefix, args.dpi)
                    except Exception:
                        pass
                    status = "unreadable"
                finally:
                    page.close()
                target = pages_dir / f"{prefix}.json"
                write_json(target, record)
                manifest["pages"].append({"physical_page": no, "label": label,
                    "data": target.relative_to(out).as_posix(), "status": status,
                    "warnings": record.get("warnings", []), "page_image": record.get("page_image"),
                    "ocr_status": record.get("ocr", {}).get("status"),
                    "ocr_message": record.get("ocr", {}).get("message"),
                    "audit": record.get("audit")})
                write_json(manifest_path, manifest)
            manifest["unprocessed_pages"] = selected[len(manifest["pages"]):]
            status_counts = Counter(e["status"] for e in manifest["pages"])
            manifest["status_counts"] = dict(status_counts)
            if manifest["unprocessed_pages"] or status_counts["unreadable"] or status_counts["accounting_error"]:
                manifest["status"] = "partial"
            elif status_counts["review_required"]:
                manifest["status"] = "review_required"
            else:
                manifest["status"] = "extracted"
            write_json(manifest_path, manifest)
            threshold = max(2, math.ceil(len(manifest["pages"]) * 0.5))
            repeated = {key for key, count in margins.items() if count >= threshold}
            manifest["repeated_margin_candidates"] = [
                {"role": key[0], "text": key[1], "relative_top": key[2], "font_size": key[3],
                 "page_count": margins[key]} for key in sorted(repeated)]
            with (out / "document.md").open("w", encoding="utf-8", errors="backslashreplace") as stream:
                stream.write("# Extracted PDF evidence\n\n")
                stream.write(f"Source: {literal(source.name)}\n\nSHA-256: {manifest['source']['sha256']}\n")
                for entry in manifest["pages"]:
                    if time_left(args) <= 0:
                        manifest["warnings"].append("time_budget_exhausted_before_markdown_export")
                        manifest["status"] = "partial"
                        stream.write("\n[Markdown export incomplete. Consult the page JSON and manifest.]\n")
                        break
                    target = out / entry["data"]
                    record = json.loads(target.read_text(encoding="utf-8"))
                    if not record.get("error"):
                        excluded = set()
                        for f in record["blocks"]:
                            key = margin_key(f, record["height_pt"])
                            if key in repeated:
                                f["margin_role_candidate"] = key[0]
                                excluded.add(f["id"])
                            elif f["role"] == "page_number_candidate":
                                excluded.add(f["id"])
                        record["body_flow_candidate"] = [n for n in record["flow"] if n not in excluded]
                        record["margin_candidate_ids"] = [n for n in record["flow"] if n in excluded]
                        write_json(target, record)
                    write_markdown(stream, record, repeated)
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    manifest["elapsed_seconds"] = round(time.perf_counter() - start, 3)
    write_json(manifest_path, manifest)
    print(json.dumps({"status": manifest["status"], "result": str(out),
                      "page_count": manifest.get("page_count"),
                      "selected_pages": manifest.get("selected_pages"),
                      "status_counts": manifest.get("status_counts"),
                      "error": manifest.get("error")}, ensure_ascii=True))
    return 2 if manifest["status"] == "failed" else (1 if manifest["status"] == "partial" else 0)


if __name__ == "__main__":
    raise SystemExit(main())
