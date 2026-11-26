#!/usr/bin/env python3
"""Make page evidence from a local PDF. All reported boxes use displayed CropBox points."""
from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
from contextlib import redirect_stdout
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


VERSION = "2.0.0"
CAPTION = re.compile(r"^(?:extended data\s+)?(?:fig(?:ure)?\.?|table|scheme|图|表)\s*(?:[A-Z]?\d|[IVX]+\b)", re.I)
SECTION = re.compile(
    r"^(?:abstract|introduction|background|methods?|materials and methods|results|discussion|"
    r"results and discussion|conclusions?|references|acknowledg(?:e)?ments|appendix(?:\s+[A-Z])?|"
    r"摘要|关键词|引言|前言|材料与方法|方法|结果与讨论|结果|讨论|结论|参考文献|附录)$", re.I
)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"


def write_file(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
            name = tmp.name
            tmp.write(content.encode("utf-8") if isinstance(content, str) else content)
        os.replace(name, path)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


def digest(path):
    with Path(path).open("rb") as stream:
        h = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def command(args, timeout=20):
    tool = shutil.which(args[0])
    if not tool:
        raise RuntimeError(f"executable unavailable: {args[0]}")
    proc = subprocess.run([tool, *map(str, args[1:])], capture_output=True, timeout=timeout,
                          env={**os.environ, "LC_ALL": "C"})
    if proc.returncode:
        detail = proc.stderr.decode("utf-8", "replace").strip()[-650:]
        raise RuntimeError(f"{args[0]} exited {proc.returncode}: {detail}")
    return proc.stdout


def mupdf():
    try:
        import pymupdf
    except ImportError:
        return None
    return pymupdf


def choose_backend(request):
    if request != "poppler" and mupdf() is not None:
        return "mupdf"
    if request != "mupdf" and shutil.which("pdftotext") and shutil.which("pdfinfo"):
        return "poppler"
    raise RuntimeError("PDF backend unavailable; check PyMuPDF or Poppler pdfinfo/pdftotext")


def open_mupdf(path, password_env=None):
    doc = mupdf().open(str(path))
    if not doc.is_pdf:
        doc.close()
        raise ValueError("source is not a PDF")
    if doc.needs_pass and not (password_env and doc.authenticate(os.getenv(password_env, ""))):
        doc.close()
        raise ValueError("encrypted PDF; provide an authorized password through --password-env")
    return doc


def source_path(value):
    source = Path(value).resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"not a regular file: {source}")
    return source


def page_selection(spec, count):
    if not spec or not count:
        raise ValueError("specify available physical PDF pages")
    if spec == "all":
        return list(range(1, count + 1))
    selected = set()
    for raw in spec.split(","):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", raw.strip())
        if not match:
            raise ValueError(f"invalid physical page range: {raw}")
        start = int(match[1])
        stop = int(match[2] or match[1])
        if not 1 <= start <= stop <= count:
            raise ValueError(f"page selection {raw} outside 1..{count}")
        selected.update(range(start, stop + 1))
    return sorted(selected)


def box(values):
    return tuple(round(float(x), 3) for x in values)


def bounded_box(values, width, height):
    x0, y0, x1, y1 = values
    return box((max(0, x0), max(0, y0), min(width, x1), min(height, y1)))


def center_y(item):
    b = item["bbox"]
    return (b[1] + b[3]) / 2


def overlap(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def bounds(boxes):
    return box((min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes)))


def parse_box(spec, width, height):
    if spec is None:
        return 0, 0, width, height
    try:
        x0, y0, x1, y1 = map(float, spec.split(","))
    except ValueError as exc:
        raise ValueError("--bbox must be x0,y0,x1,y1 in displayed page points") from exc
    if not (0 <= x0 < x1 <= width + 0.05 and 0 <= y0 < y1 <= height + 0.05):
        raise ValueError(f"--bbox {spec} outside displayed page dimensions {width:g} by {height:g}")
    return bounded_box((x0, y0, x1, y1), width, height)


def mupdf_page(doc, number):
    page_selection(str(number), len(doc))
    return doc[number - 1]


def native_page(doc, n):
    f = mupdf()
    page = mupdf_page(doc, n)
    rotation = page.rotation_matrix
    width, height = page.rect.width, page.rect.height

    def display(rect):
        return bounded_box(f.Rect(rect) * rotation, width, height)

    flags = f.TEXTFLAGS_DICT & ~f.TEXT_PRESERVE_IMAGES
    flags &= ~getattr(f, "TEXT_USE_CID_FOR_UNKNOWN_UNICODE", 0)
    lines = []
    for native_group, block in enumerate(page.get_text("dict", flags=flags)["blocks"], 1):
        for line in block.get("lines", []):
            dx, dy = line["dir"]
            direction = box((rotation.a * dx + rotation.c * dy, rotation.b * dx + rotation.d * dy))
            runs = []
            for span in line["spans"]:
                b = display(span["bbox"])
                if b[0] >= b[2] or b[1] >= b[3] or not span["text"]:
                    continue
                origin = f.Point(span["origin"]) * rotation
                runs.append({"text": span["text"], "bbox": b, "font": span["font"],
                             "size_pt": round(span["size"], 3), "origin": box(origin),
                             "flags": span["flags"]})
            for group in split_runs(runs, direction):
                content = "".join(span["text"] for span in group)
                if content.strip():
                    lines.append({"bbox": bounds([span["bbox"] for span in group]), "text": content,
                                  "native_block": native_group, "direction": direction, "spans": group})
    images = []
    if hasattr(page, "get_image_info"):
        for item in page.get_image_info():
            b = display(item["bbox"])
            if b[0] < b[2] and b[1] < b[3]:
                images.append({"bbox": b, "pixels": [item["width"], item["height"]]})
    return {"width_pt": width, "height_pt": height, "printed_label": page.get_label() or None,
            "rotation": page.rotation, "lines": lines, "raster_regions": images}


def split_runs(runs, direction):
    group = []
    for span in runs:
        if group and direction[0] > 0.9:
            prev = group[-1]
            gap = span["bbox"][0] - prev["bbox"][2]
            if gap > max(24, 2.7 * max(span["size_pt"], prev["size_pt"])):
                yield group
                group = []
        group.append(span)
    if group:
        yield group


def poppler_count(path):
    info = command(["pdfinfo", "-box", path]).decode("utf-8", "replace")
    m = re.search(r"^Pages:\s*(\d+)", info, re.M)
    if not m:
        raise ValueError("pdfinfo did not report a page count")
    return int(m[1])


def poppler_rotations(path, start, stop):
    raw = command(["pdfinfo", "-f", start, "-l", stop, "-box", path]).decode("utf-8", "replace")
    return {int(n): int(deg) % 360 for n, deg in re.findall(r"^Page\s+(\d+)\s+rot:\s*(-?\d+)", raw, re.M)}


def poppler_page(path, n, rotation, timeout=20):
    raw = command(["pdftotext", "-f", n, "-l", n, "-cropbox", "-bbox-layout",
                   "-enc", "UTF-8", path, "-"], timeout=timeout)
    # Poppler can emit XML-invalid ASCII controls for malformed embedded text.
    root = ET.fromstring(re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"\xef\xbf\xbd", raw))
    page = root.find(".//{*}page")
    if page is None:
        raise ValueError("Poppler did not report the requested page")
    width, height = (float(page.attrib[key]) for key in ("width", "height"))
    if rotation in (90, 270):
        width, height = height, width

    def coords(node):
        return bounded_box(tuple(float(node.attrib[k]) for k in ("xMin", "yMin", "xMax", "yMax")),
                           width, height)

    lines = []
    for num, block in enumerate(page.findall(".//{*}block"), 1):
        for line in block.findall("{*}line"):
            words = [{"bbox": coords(word), "text": "".join(word.itertext())}
                     for word in line.findall("{*}word")]
            content = " ".join(word["text"] for word in words)
            b = coords(line)
            if content.strip() and b[0] < b[2] and b[1] < b[3]:
                lines.append({"bbox": b, "text": content, "native_block": num,
                              "direction": None, "spans": words})
    return {"width_pt": width, "height_pt": height, "printed_label": None,
            "rotation": rotation, "lines": lines, "raster_regions": []}


def header_role(line, height):
    s = line["text"].strip()
    if CAPTION.match(s):
        return "caption_candidate"
    title = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", s)
    if SECTION.fullmatch(title) or (len(s) <= 95 and title != s and s[-1] not in ".,;，；。"):
        return "heading_candidate"
    if re.fullmatch(r"\d{1,4}", s) and center_y(line) >= 0.92 * height:
        return "page_number_candidate"
    if center_y(line) >= 0.83 * height and re.match(r"^(?:\*+|[†‡§¶⁎]|[¹²³])\s*\S", s):
        return "footnote_candidate"
    return "text"


def column_split(lines, x0, x1, height):
    width = x1 - x0
    if width < 130:
        return None
    body = [ln for ln in lines if 0.07 * height < center_y(ln) < 0.94 * height
            and ln["bbox"][2] - ln["bbox"][0] > max(30, width * 0.13)
            and (ln["direction"] is None or ln["direction"][0] > 0.9)
            and ln["role"] == "text"]
    if len(body) < 8:
        return None
    options = []
    for step in range(16, 45):
        x = x0 + step * width / 60
        gap = max(2.5, width * 0.007)
        left = [ln for ln in body if ln["bbox"][2] <= x - gap]
        right = [ln for ln in body if ln["bbox"][0] >= x + gap]
        if min(len(left), len(right)) < 4:
            continue
        ys = sorted(center_y(ln) for ln in right)
        window = min(24, max(10, height * 0.022))
        peers = []
        for ln in left:
            y = center_y(ln)
            i = bisect_left(ys, y - window)
            if i < len(ys) and ys[i] <= y + window:
                peers.append(y)
        if len(peers) < 4 or max(peers) - min(peers) < 21:
            continue
        crossing = len(body) - len(left) - len(right)
        if len(peers) < crossing * 0.24:
            continue
        nearest_left = max(ln["bbox"][2] for ln in left)
        nearest_right = min(ln["bbox"][0] for ln in right)
        score = min(len(peers), len(right)) * 2 + min(len(left), len(right)) - crossing * 0.55
        options.append((score, nearest_right - nearest_left, (nearest_left + nearest_right) / 2))
    return round(max(options)[2], 3) if options else None


def order_lines(lines, width, height):
    gutters = set()

    def row_order(items):
        return sorted(items, key=lambda ln: (round(center_y(ln) / 2), ln["bbox"][0], ln["id"]))

    def region(items, left_edge, right_edge, depth):
        x = column_split(items, left_edge, right_edge, height) if depth < 2 else None
        if x is None:
            return row_order(items)
        gutters.add(x)
        ordered, left, right = [], [], []

        def flush():
            if left:
                ordered.extend(region(left, left_edge, x, depth + 1))
                left.clear()
            if right:
                ordered.extend(region(right, x, right_edge, depth + 1))
                right.clear()

        for item in row_order(items):
            a, _, b, _ = item["bbox"]
            if a < x - 4 and b > x + 4:
                flush()
                ordered.append(item)
            else:
                (left if a + b < 2 * x else right).append(item)
        flush()
        return ordered

    margin_roles = {"repeated_margin_candidate", "footnote_candidate", "page_number_candidate"}
    top, body, bottom = [], [], []
    for line in lines:
        if line["role"] in margin_roles:
            (top if center_y(line) < height / 2 else bottom).append(line)
        else:
            body.append(line)
    return row_order(top) + region(body, 0, width, 0) + row_order(bottom), sorted(gutters)


def index_page(data, n):
    warnings = []
    lines = data["lines"]
    for i, line in enumerate(lines, 1):
        line["id"] = f"p{n:04}-l{i:04}"
        line["role"] = header_role(line, data["height_pt"])
    text = "".join(line["text"] for line in lines)
    if not text:
        warnings.append("no_text_layer")
    elif "\ufffd" in text or "(cid:" in text or any("\ue000" <= c <= "\uf8ff" for c in text):
        warnings.append("unmapped_glyphs_review")
    if any(line["direction"] and line["direction"][0] < 0.9 for line in lines):
        warnings.append("rotated_or_non_ltr_lines_review")
    page_area = data["width_pt"] * data["height_pt"]
    if page_area and any(overlap(r["bbox"], r["bbox"]) / page_area > 0.35 for r in data["raster_regions"]):
        warnings.append("substantial_raster_region_review")
    ordered, gutters = order_lines(lines, data["width_pt"], data["height_pt"])
    if gutters:
        warnings.append("multicolumn_order_candidate_review")
    return {**data, "physical_page": n, "lines": ordered, "gutters_pt": gutters, "warnings": warnings}


def margin_keys(data):
    height = data["height_pt"]
    for line in data["lines"]:
        y = center_y(line)
        if y < 0.085 * height or y > 0.92 * height:
            s = re.sub(r"\d+", "#", re.sub(r"\s+", " ", line["text"].strip().casefold()))
            if s and line["role"] != "caption_candidate":
                yield (y > 0.92 * height, s), line


def parse_document(args):
    path = source_path(args.pdf)
    backend = choose_backend(args.backend)
    if backend == "poppler" and args.password_env:
        raise ValueError("password entry through an environment variable requires PyMuPDF")
    out = Path(args.out).resolve()
    doc = open_mupdf(path, args.password_env) if backend == "mupdf" else None
    try:
        count = len(doc) if doc is not None else poppler_count(path)
        pages = page_selection(args.pages, count)
        rotations = poppler_rotations(path, pages[0], pages[-1]) if doc is None else {}
        out.mkdir(parents=True, exist_ok=False)
        indexed = []
        failures = {}
        start = time.monotonic()
        source = {"path": str(path), "filename": path.name, "sha256": digest(path)}
        margin_counts = Counter()
        for n in pages:
            remaining = args.seconds - (time.monotonic() - start)
            if remaining <= 0:
                failures[n] = "index time budget exhausted before page extraction"
                continue
            try:
                data = (native_page(doc, n) if doc is not None
                        else poppler_page(path, n, rotations.get(n), min(20, remaining)))
                data = index_page(data, n)
                if backend == "poppler":
                    data["warnings"].append("word_spacing_reconstructed_from_poppler")
                name = f"p{n:04}.json"
                write_file(out / name, json_text(data))
                indexed.append((n, name, data["warnings"]))
                margin_counts.update({key for key, _ in margin_keys(data)})
            except Exception as exc:
                failures[n] = f"{type(exc).__name__}: {exc}"
        for i, (n, name, _) in enumerate(indexed):
            data = json.loads((out / name).read_text(encoding="utf-8"))
            changed = False
            for key, line in margin_keys(data):
                if margin_counts[key] >= 2 and line["role"] in ("text", "page_number_candidate"):
                    line["role"] = "repeated_margin_candidate"
                    changed = True
            if changed:
                data["lines"], data["gutters_pt"] = order_lines(data["lines"], data["width_pt"], data["height_pt"])
                data["warnings"][:] = [warning for warning in data["warnings"] if warning != "multicolumn_order_candidate_review"]
                if data["gutters_pt"]:
                    data["warnings"].append("multicolumn_order_candidate_review")
                write_file(out / name, json_text(data))
                indexed[i] = n, name, data["warnings"]
        manifest = {"source": source, "parser_version": VERSION, "backend": backend,
                    "backend_version": getattr(mupdf(), "__version__", None) if doc is not None else None,
                    "coordinate_system": "displayed CropBox, top-left, PDF points",
                    "page_count": count, "selected_physical_pages": pages,
                    "pages": [{"physical_page": n, "file": name, "warnings": warn} for n, name, warn in indexed],
                    "page_failures": [{"physical_page": n, "reason": reason} for n, reason in failures.items()],
                    "status": "partial" if failures and indexed else "failed" if failures else "indexed",
                    "elapsed_seconds": round(time.monotonic() - start, 3)}
        if doc is not None:
            manifest["outlines"] = [row for row in doc.get_toc() if row[2] in pages]
        write_file(out / "manifest.json", json_text(manifest))
        print(json_text({"index": str(out / "manifest.json"), "status": manifest["status"],
                         "backend": backend, "pages_requested": len(pages), "pages_indexed": len(indexed),
                         "pages_to_review": [n for n, _, warnings in indexed if warnings]}), end="")
        return 2 if not indexed else int(bool(failures))
    finally:
        if doc is not None:
            doc.close()


def show(args):
    root = Path(args.index)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    requested = page_selection(args.pages, manifest["page_count"]) if args.pages else None
    if requested and any(n not in manifest["selected_physical_pages"] for n in requested):
        raise ValueError("a requested page is outside this index's selected range")
    rx = re.compile(args.regex, re.I) if args.regex else None
    remaining = args.limit
    for entry in manifest["pages"]:
        n = entry["physical_page"]
        if requested is not None and n not in requested:
            continue
        data = json.loads((root / entry["file"]).read_text(encoding="utf-8"))
        region = parse_box(args.bbox, data["width_pt"], data["height_pt"]) if args.bbox else None
        lines = [ln for ln in data["lines"] if region is None or overlap(ln["bbox"], region) > 0]
        if rx:
            indices = set()
            for i, line in enumerate(lines):
                if rx.search(line["text"]):
                    indices.update(range(max(0, i - args.context), min(i + args.context + 1, len(lines))))
            lines = [lines[i] for i in sorted(indices)]
            if not lines:
                continue
        print(f"# Physical page {n} | PDF label {data['printed_label']!r} | "
              f"{data['width_pt']:g} × {data['height_pt']:g} pt | {', '.join(data['warnings']) or 'no parser warnings'}")
        for line in lines:
            if remaining == 0:
                print("[Display limit reached. Narrow --pages, --bbox, or --regex, or raise --limit.]")
                return 0
            xys = ",".join(f"{x:g}" for x in line["bbox"])
            print(f"[{line['id']} {line['role']} {xys}] {line['text']}")
            remaining -= 1
    for failure in manifest["page_failures"]:
        if requested is None or failure["physical_page"] in requested:
            print(f"[Page {failure['physical_page']} not indexed: {failure['reason']}]")
    return 0


def render_bytes(path, n, backend, bbox, dpi, password_env):
    scale = dpi / 72
    if backend == "mupdf":
        f = mupdf()
        with open_mupdf(path, password_env) as doc:
            page = mupdf_page(doc, n)
            region = parse_box(bbox, page.rect.width, page.rect.height)
            pix = page.get_pixmap(matrix=f.Matrix(scale, scale), clip=f.Rect(region), alpha=False)
            return pix.tobytes("png"), (pix.x, pix.y), region, (page.rect.width, page.rect.height)
    if password_env:
        raise ValueError("password entry through an environment variable requires PyMuPDF")
    page_selection(str(n), poppler_count(path))
    data = poppler_page(path, n, poppler_rotations(path, n, n).get(n))
    width, height = data["width_pt"], data["height_pt"]
    region = parse_box(bbox, width, height)
    x0, y0, x1, y1 = region
    x, y = math.floor(x0 * scale), math.floor(y0 * scale)
    w, h = math.ceil(x1 * scale) - x, math.ceil(y1 * scale) - y
    args = ["pdftoppm", "-f", n, "-l", n, "-r", dpi, "-cropbox", "-png", "-singlefile"]
    if bbox:
        args.extend(["-x", x, "-y", y, "-W", w, "-H", h])
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "crop"
        command([*args, path, target], timeout=25)
        return target.with_suffix(".png").read_bytes(), (x, y), region, (width, height)


def render_or_ocr(args):
    path = source_path(args.pdf)
    backend = choose_backend(args.backend)
    png, pixel_origin, region, (width, height) = render_bytes(
        path, args.page, backend, args.bbox, args.dpi, args.password_env)
    if args.action == "render":
        write_file(args.out, png)
        print(json_text({"image": str(Path(args.out).resolve()), "source": str(path), "sha256": digest(path), "backend": backend,
                         "physical_page": args.page, "bbox": region, "dpi": args.dpi}), end="")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        image_path = Path(tmp) / "ocr.png"
        image_path.write_bytes(png)
        tsv = command(["tesseract", image_path, "stdout", "-l", args.lang,
                       "--psm", args.psm, "tsv"], timeout=args.timeout)
    scale = args.dpi / 72
    words = []
    for row in csv.DictReader(io.StringIO(tsv.decode("utf-8", "replace")), delimiter="\t"):
        if row.get("level") != "5" or not (row.get("text") or "").strip():
            continue
        x = int(row["left"]) + pixel_origin[0]
        y = int(row["top"]) + pixel_origin[1]
        b = bounded_box((x / scale, y / scale, (x + int(row["width"])) / scale,
                         (y + int(row["height"])) / scale), width, height)
        words.append({"id": f"p{args.page:04}-o{len(words) + 1:04}", "bbox": b, "text": row["text"],
                      "ocr_line": [int(row[k]) for k in ("block_num", "par_num", "line_num")],
                      "confidence": float(row["conf"])})
    result = {"source": str(path), "sha256": digest(path), "physical_page": args.page, "bbox": region,
              "language": args.lang, "psm": args.psm, "dpi": args.dpi, "method": "tesseract-tsv",
              "status": "candidate" if words else "no_words_recognized", "words": words}
    write_file(args.out, json_text(result))
    print(json_text({"ocr": str(Path(args.out).resolve()), "status": result["status"],
                     "word_count": len(words)}), end="")
    return int(not words)


def edge_positions(values, tolerance=1.4):
    groups = []
    for value in sorted(set(values)):
        if not groups or value - groups[-1][0] > tolerance:
            groups.append([])
        groups[-1].append(value)
    return [round(statistics.mean(g), 3) for g in groups]


def table_grid(cells, slot_limit=6000):
    xs = edge_positions([c["bbox"][i] for c in cells for i in (0, 2)])
    ys = edge_positions([c["bbox"][i] for c in cells for i in (1, 3)])
    rows, cols = len(ys) - 1, len(xs) - 1
    if rows < 1 or cols < 1 or rows * cols > slot_limit:
        return {"shape": [max(rows, 0), max(cols, 0)], "warnings": ["grid_dimensions_review"]}
    owners = [[[] for _ in range(cols)] for _ in range(rows)]
    warnings = set()

    def nearest(edges, x):
        return min(range(len(edges)), key=lambda i: abs(edges[i] - x))

    for cell in cells:
        x0, y0, x1, y1 = cell["bbox"]
        r0, r1 = nearest(ys, y0), nearest(ys, y1)
        c0, c1 = nearest(xs, x0), nearest(xs, x1)
        if r0 >= r1 or c0 >= c1:
            warnings.add("cell_collapsed_when_snapping_edges")
            continue
        cell.update(row=r0 + 1, column=c0 + 1, rowspan=r1 - r0, colspan=c1 - c0)
        for r in range(r0, r1):
            for c in range(c0, c1):
                owners[r][c].append(cell["id"])
    holes = [[r + 1, c + 1] for r, row in enumerate(owners) for c, ids in enumerate(row) if not ids]
    conflicts = [[r + 1, c + 1] for r, row in enumerate(owners) for c, ids in enumerate(row) if len(ids) > 1]
    if holes:
        warnings.add("unassigned_grid_slots")
    if conflicts:
        warnings.add("conflicting_grid_slots")
    return {"shape": [rows, cols], "x_edges_pt": xs, "y_edges_pt": ys, "slot_owners": owners,
            "unassigned_slots": holes, "overlapping_slots": conflicts, "warnings": sorted(warnings)}


def table_candidates(args):
    f = mupdf()
    if f is None or not hasattr(f.Page, "find_tables"):
        raise RuntimeError("table grid extraction needs PyMuPDF with Page.find_tables; render and transcribe the region")
    if args.strategy == "text" and not args.bbox:
        raise ValueError("provide a visually identified --bbox for the text-aligned table strategy")
    path = source_path(args.pdf)
    tables = []
    with open_mupdf(path, args.password_env) as doc:
        page = mupdf_page(doc, args.page)
        clip = parse_box(args.bbox, page.rect.width, page.rect.height)
        rotation = page.rotation_matrix
        derotate = page.derotation_matrix
        unrotated = f.Rect(clip) * derotate
        page.set_rotation(0)
        with redirect_stdout(sys.stderr):
            found = page.find_tables(clip=unrotated, strategy=args.strategy)
        for i, table in enumerate(found.tables, 1):
            table_id = f"p{args.page:04}-t{i:02}"
            extracted = table.extract()
            contents = {}
            for j, row in enumerate(table.rows):
                for k, raw_cell in enumerate(row.cells):
                    if raw_cell is not None:
                        raw_text = extracted[j][k] if j < len(extracted) and k < len(extracted[j]) else None
                        contents[tuple(raw_cell)] = raw_text
            cells = [{"id": f"{table_id}-c{j:03}", "bbox": box(f.Rect(b) * rotation), "text": contents.get(tuple(b))}
                     for j, b in enumerate(dict.fromkeys(tuple(c) for c in table.cells), 1)]
            grid = table_grid(cells)
            if args.strategy == "text":
                grid["warnings"].append("text_alignment_grid_review_all_spans")
            if any(cell["text"] is None for cell in cells):
                grid["warnings"].append("some_cell_text_unavailable")
            tables.append({"id": table_id, "bbox": box(f.Rect(table.bbox) * rotation),
                           "cells": sorted(cells, key=lambda c: (c["bbox"][1], c["bbox"][0])), **grid})
    result = {"source": str(path), "sha256": digest(path), "physical_page": args.page, "region_bbox": clip,
              "strategy": args.strategy, "status": "candidates" if tables else "no_candidate_found", "tables": tables}
    write_file(args.out, json_text(result))
    print(json_text({"tables": str(Path(args.out).resolve()), "status": result["status"],
                     "table_count": len(tables)}), end="")
    return int(not tables)


def doctor(_args):
    f = mupdf()
    info = {"parser_version": VERSION, "python": sys.version.split()[0],
            "pymupdf": getattr(f, "__version__", None), "find_tables": bool(f and hasattr(f.Page, "find_tables")),
            "executables": {name: shutil.which(name) for name in ("pdfinfo", "pdftotext", "pdftoppm", "tesseract")}}
    if info["executables"]["tesseract"]:
        try:
            info["ocr_languages"] = command(["tesseract", "--list-langs"], timeout=5).decode().splitlines()[1:]
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            info["ocr_error"] = str(exc)
    print(json_text(info), end="")
    return 0


def within(lo, hi):
    def check(value):
        n = int(value)
        if not lo <= n <= hi:
            raise argparse.ArgumentTypeError(f"expected {lo}..{hi}")
        return n
    return check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor").set_defaults(run=doctor)
    idx = sub.add_parser("index")
    idx.add_argument("pdf")
    idx.add_argument("--backend", choices=("auto", "mupdf", "poppler"), default="auto")
    idx.add_argument("--password-env")
    idx.add_argument("--out", required=True)
    idx.add_argument("--pages", required=True, help="physical pages, e.g. 1-3,5 or all if in scope")
    idx.add_argument("--seconds", type=within(1, 600), default=100)
    idx.set_defaults(run=parse_document)
    see = sub.add_parser("read")
    see.add_argument("index")
    see.add_argument("--pages")
    see.add_argument("--bbox")
    see.add_argument("--regex")
    see.add_argument("--context", type=within(0, 15), default=1)
    see.add_argument("--limit", type=within(1, 3000), default=110)
    see.set_defaults(run=show)
    for action in ("render", "ocr", "tables"):
        p = sub.add_parser(action)
        p.add_argument("pdf")
        p.add_argument("--page", required=True, type=int)
        p.add_argument("--bbox")
        p.add_argument("--out", required=True)
        p.add_argument("--password-env")
        if action == "tables":
            p.add_argument("--strategy", default="lines_strict", choices=("lines_strict", "lines", "text"))
            p.set_defaults(run=table_candidates)
            continue
        p.add_argument("--backend", choices=("auto", "mupdf", "poppler"), default="auto")
        p.add_argument("--dpi", type=within(72, 450), default=240)
        p.set_defaults(run=render_or_ocr)
        if action == "ocr":
            p.add_argument("--lang", required=True)
            p.add_argument("--psm", type=within(0, 13), default=6)
            p.add_argument("--timeout", type=within(1, 90), default=25)
    args = parser.parse_args()
    try:
        return args.run(args)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, ET.ParseError, re.error) as exc:
        print(json_text({"operation": args.action, "status": "failed", "reason": str(exc)}), end="", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
