"""Shared data contracts and evidence coordinates for nanopub-evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path

VERSION = "1.0.0"
SCHEMA = "nanopub-evidence/1"
BASE = "https://example.org/nanopub-evidence/"
VOCAB = BASE + "v1/"
ROLES = {"support", "context", "counter", "reference"}
META_FIELDS = ("title", "authors", "year", "doi", "arxiv_id", "version", "venue", "url")
RELATIONS = {
    "has_value": "The subject measure has the stated value or range.",
    "higher_than": "The subject is numerically higher than the object on the named measure.",
    "lower_than": "The subject is numerically lower than the object on the named measure.",
    "outperforms": "The subject performs better on the named criterion and evaluation scope.",
    "differs_from": "The subject differs from the object on the named measure.",
    "associated_with": "The subject and object are associated in the studied population or data.",
    "increases": "The subject raises the stated outcome relative to the named baseline.",
    "decreases": "The subject lowers the stated outcome relative to the named baseline.",
    "causes": "The source explicitly attributes the outcome to the subject as a cause.",
    "affects": "The source explicitly reports an effect whose direction is unspecified.",
    "predicts": "The subject predicts the object under the stated model or evaluation.",
    "detects": "The subject detects the specified signal, event or feature.",
    "uses": "The subject employs the object in a stated procedure.",
    "evaluated_on": "The subject is evaluated on the stated data or population.",
    "trained_on": "The subject is trained on the stated data.",
    "produces": "The subject yields the stated output.",
    "contains": "The subject whole contains the object part.",
    "part_of": "The subject part belongs to the object whole.",
    "located_in": "The subject is located in the stated object.",
    "derived_from": "The subject originates from the specified object.",
    "is_a": "The subject belongs to the named class.",
    "consistent_with": "The subject is compatible with the observation or hypothesis named as object.",
    "requires": "The stated object is necessary for the subject in the specified scope.",
    "limits": "The subject restricts the stated object or inference.",
    "supports": "The subject provides evidence for the named statement.",
    "other": "The supplied relation_label states a more precise relation.",
}


class InputError(ValueError):
    """An actionable error with a stable machine-readable kind."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON property {key!r}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"{value} is outside the JSON number grammar")

    try:
        result = json.loads(Path(path).read_text(encoding="utf-8-sig"),
                            object_pairs_hook=unique, parse_constant=invalid_constant)
        canonical(result).encode("utf-8")
        return result
    except (OSError, ValueError, UnicodeError) as error:
        raise InputError("json_input", f"{Path(path).name}: {error}") from error


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def hash_bytes(value):
    return hashlib.sha256(value).hexdigest()


def fingerprint(value):
    return hash_bytes(canonical(value).encode("utf-8"))


def file_hash(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _stream_hash(handle)


def _stream_hash(handle):
    digest = hashlib.sha256()
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
        except BaseException:
            handle.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def issue(kind, message, severity="error", **location):
    return {"kind": kind, "severity": severity, "message": message, **location}


def clean(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def words(value):
    return re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", clean(value))


def folded_match(value, corpus):
    wanted = words(value)
    available = words(corpus)
    if not wanted:
        return False
    return any(available[i:i + len(wanted)] == wanted for i in range(len(available) - len(wanted) + 1))


def sentences(text):
    """Return offsets into text. Protect common abbreviations followed by spaces."""
    ranges = []
    start = 0
    for match in re.finditer(r"[。！？]+[”’」』）]*|[.!?]+[\"'”’)\]]*(?=\s|$)", text):
        end = match.end()
        before = text[max(start, match.start() - 20):match.start()]
        if end < len(text) and match.group().startswith(".") and re.search(
            r"(?:\bet al|\be\.g|\bi\.e|\bfigs?|\beqs?|\btabs?|\brefs?|\bvs|\bdr|\bprof|\bno|\bvol|\betc)$", before, re.I
        ):
            continue
        _add_range(ranges, text, start, end)
        start = end
    _add_range(ranges, text, start, len(text))
    return ranges


def _add_range(ranges, text, start, end):
    segment = text[start:end]
    if segment.strip():
        left = start + len(segment) - len(segment.lstrip())
        ranges.append([left, start + len(segment.rstrip())])


class Evidence:
    def __init__(self, sources):
        self.units = {}
        for source in sources:
            for unit in source["units"]:
                self.units[unit["id"]] = (source, unit)

    def anchor(self, anchor):
        if not isinstance(anchor, str):
            raise InputError("anchor", "Anchor must be a string such as S1-U0004.2.")
        match = re.fullmatch(r"(S[1-9]\d*-U\d{4,})(?:\.([1-9]\d*))?", anchor)
        if not match or match[1] not in self.units:
            raise InputError("anchor", f"Unknown anchor {anchor!r}. Read the current snapshot.")
        source, unit = self.units[match[1]]
        sentence = int(match[2]) if match[2] else None
        if sentence is not None and sentence > len(unit["spans"]):
            raise InputError("anchor", f"{unit['id']} has {len(unit['spans'])} sentence(s) or row(s).")
        start, end = unit["spans"][sentence - 1] if sentence else (0, len(unit["text"]))
        return source, unit, sentence, start, end

    def locate(self, requested, default_role="support"):
        if not isinstance(requested, dict):
            raise InputError("evidence_shape", "An evidence item needs an anchor and optional quote.")
        anchor = requested.get("anchor")
        source, unit, sentence, left, right = self.anchor(anchor)
        role = requested.get("role", default_role)
        if not isinstance(role, str) or role not in ROLES:
            raise InputError("evidence_role", "Choose support, context, counter or reference.")
        if role in {"support", "counter"} and unit["kind"] in {"bibliography", "back_matter"}:
            raise InputError("evidence_scope", f"{anchor} is {unit['kind']}. Use it for attribution or context.")
        text = unit["text"]
        quote = requested.get("quote")
        if quote is None:
            quote = text[left:right]
            if len(quote) > 1200:
                raise InputError("span_size", f"{anchor} is long. Select a numbered sentence/row or a short exact quote.")
        elif not isinstance(quote, str) or not quote.strip():
            raise InputError("quote", f"Give a non-empty exact quote inside {anchor}, or omit quote.")
        if len(quote) > 1600:
            raise InputError("span_size", f"The quote at {anchor} exceeds 1600 code points. Select the minimal supporting span.")
        supplied_start = requested.get("start")
        if supplied_start is None:
            start = text.find(quote, left, right)
            if start < 0:
                raise InputError("quote_outside_anchor", f"The quote is not an exact substring inside {anchor}.")
            if text.find(quote, start + 1, right) >= 0:
                raise InputError("ambiguous_quote", f"The quote repeats inside {anchor}. Give its exact unit start offset.")
        else:
            start = supplied_start
            if type(start) is not int or start < left or start + len(quote) > right or text[start:start + len(quote)] != quote:
                raise InputError("offset", f"The requested offset does not reproduce the quote inside {anchor}.")
        end = start + len(quote)
        proof = fingerprint([source["sha256"], unit["id"], unit["text_sha256"], anchor, start, end, quote, role])
        locator = {
            "anchor": anchor, "unit": unit["id"], "sentence": sentence, "page": unit["page"],
            "lines": unit["lines"], "section": unit["section"], "kind": unit["kind"],
            "json_pointer": unit["pointer"],
            "bbox": unit["bbox"], "bbox_scope": "block" if unit["bbox"] else None,
            "char_start": start, "char_end": end, "offset_unit": "unicode_codepoint_in_unit",
            "unit_text_sha256": unit["text_sha256"], "table_or_figure": unit["label"],
        }
        return {"id": "ev-" + proof[:24], "role": role, "quote": text[start:end],
                "source_id": source["id"], "source_sha256": source["sha256"], "locator": locator,
                "extraction": unit["extraction"]}

    def full_context(self, evidence):
        _, unit, _, left, right = self.anchor(evidence["locator"]["anchor"])
        return unit["text"][left:right]
