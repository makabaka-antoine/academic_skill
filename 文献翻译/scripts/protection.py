"""Register source occurrences and verify the Agent's masked translations."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
import re


MATH = re.compile(
    r"(?<!\\)\$\$[\s\S]+?(?<!\\)\$\$|(?<![\\$])\$(?!\$)[^\n$]+?(?<!\\)\$(?!\$)"
    r"|\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\]"
    r"|\\begin\{(?P<env>(?:equation|align|gather|multline)\*?)\}[\s\S]+?\\end\{(?P=env)\}"
)
CITE = re.compile(
    r"\\(?:cite[A-Za-z]*\*?|(?:eq|auto|page)?ref|label)(?:\[[^\]\n]*\]){0,2}\{[^{}\n]+\}"
    r"|\[\d+(?:\s*[,;–-]\s*\d+)*\](?!\()|\[@[^\]\n]+\]"
    r"|\([A-Z][A-Za-z'’-]+(?:\s+et al\.|\s+(?:and|&)\s+[A-Z][A-Za-z'’-]+)?,?\s+(?:18|19|20)\d{2}[a-z]?\)"
)
LABEL = re.compile(
    r"(?<!\w)(?P<type>fig(?:ure)?\.?|table|eq(?:uation)?\.?)\s+"
    r"(?P<index>\([A-Z]?\d+(?:[a-z]|\.\d+)*\)|[A-Z]?\d+(?:[a-z]|\.\d+)*)(?![A-Za-z0-9_])", re.I
)
LITERAL = re.compile(r"(?<!`)`+[^`\r\n]+`+(?!`)|https?://[^\s<>\])]+(?<![.,;:!?])")
NUMBER = r"[+\-−]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE][+\-−]?\d+)?"
NUM = re.compile(
    rf"(?<![A-Za-z0-9_.])(?:(?:[<>]=?|[≤≥≈])[ \t]*)?{NUMBER}"
    rf"(?:[ \t]*(?:±|–|-)[ \t]*{NUMBER})?"
    r"(?:[ \t]*(?:[%％‰]|°[ \t]*[CF]|μg|µg|μm|µm|mmHg|mL|mg|kg|ng|cm|mm|km|"
    r"kHz|MHz|Hz|ms|min|s|h|g|L|m|K|days?|weeks?|months?|years?)(?![A-Za-z]))?"
    r"(?![A-Za-z0-9_])"
)
PATTERNS = (("LIT", LITERAL), ("MATH", MATH), ("CITE", CITE), ("LABEL", LABEL), ("NUM", NUM))
MARKER = re.compile(r"⟦[^⟦⟧\r\n]+⟧")
BARE_TOKEN = re.compile(r"(?<![A-Za-z0-9_])(?:TERM_T\d+|MATH|CITE|LABEL|NUM|LIT)_U\d+_O\d+(?![A-Za-z0-9_])")
ABBREV = re.compile(r"(?:\b(?:e\.g|i\.e|et al|figs?|eqs?|refs?|vs|Dr|Prof)|(?:\b[A-Z]\.)+[A-Z])\.$", re.I)
EXPLICIT_GROUP = re.compile(r"(?i:\b(?:group|arm|cohort|model|method)\s+)([A-Z][0-9]?)(?![A-Za-z0-9_])")
BARE_GROUP = re.compile(r"\b([A-Z])(?=\s+(?i:was|were|had|has|showed|achieved|yielded|recorded)\b|\s*[:=])")
BARE_PAIR = re.compile(r"\b([A-Z])(?:\s*,\s*|\s+and\s+)\s*([A-Z])\b", re.I)
VALUE_CUE = re.compile(r"\b(?:was|were|had|has|recorded|showed|achieved|yielded|reported|reached|obtained|scored)\b|[:=：为达是]", re.I)
TERM_EXCEPTIONS = {"ambiguous_keep", "authorized_omission", "approved_expansion", "not_a_term"}


def digest(value) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(data.encode("utf-8")).hexdigest()


def overlaps(start: int, end: int, items: list[dict]) -> bool:
    return any(start < entry["end"] and end > entry["start"] for entry in items)


def protected_spans(text: str) -> list[dict]:
    found = []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if overlaps(start, end, found):
                continue
            if kind == "LABEL":
                name = match["type"].lower()
                label = "图" if name.startswith("fig") else ("表" if name.startswith("table") else "式")
                target = f"{label} {match['index']}"
            else:
                target = match[0]
            found.append({"kind": kind, "start": start, "end": end, "source": match[0], "target": target})
    return sorted(found, key=lambda item: item["start"])


def sentence_spans(unit_id: str, text: str, protected: list[dict]) -> list[dict]:
    breaks = [0]
    for match in re.finditer(r"""[.!?]+["”’')\]]*(?=\s|$)|[。！？]+["”’）]*""", text):
        if overlaps(match.start(), match.end(), protected):
            continue
        if text[match.start()] == "." and match.end() < len(text) and ABBREV.search(text[:match.start() + 1]):
            continue
        breaks.append(match.end())
    if breaks[-1] != len(text):
        breaks.append(len(text))
    sentences = []
    for start, end in zip(breaks, breaks[1:]):
        raw = text[start:end]
        if raw.strip():
            sentences.append({"id": f"{unit_id}.s{len(sentences) + 1:03d}", "start": start, "end": end, "text": raw})
    return sentences


def strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def read_glossary(data: dict | None) -> dict:
    if data is None:
        return {"terms": [], "exceptions": []}
    if not isinstance(data, dict) or not isinstance(data.get("terms"), list):
        raise ValueError("Glossary must contain a terms array.")
    terms, seen = [], set()
    for entry in data["terms"]:
        if not isinstance(entry, dict):
            raise ValueError("Each glossary entry must be an object.")
        term_id, forms = entry.get("id"), entry.get("source_forms")
        target = entry.get("target", "")
        status = entry.get("selection_status", "auto_proposed")
        allowed, scope = entry.get("allowed_targets", []), entry.get("unit_ids", [])
        basis = entry.get("basis", "")
        if not isinstance(term_id, str) or not re.fullmatch(r"T\d{4,}", term_id) or term_id in seen:
            raise ValueError(f"Use a unique term ID such as T0003, found {term_id!r}.")
        if not strings(forms) or not forms or not strings(allowed) or not strings(scope):
            raise ValueError(f"Invalid source forms, allowed targets or unit scope for {term_id}.")
        if not isinstance(status, str) or status not in {"auto_proposed", "author_confirmed", "disputed"}:
            raise ValueError(f"Invalid selection_status for {term_id}.")
        if not isinstance(target, str) or (status != "disputed" and not target.strip()):
            raise ValueError(f"{term_id} requires a target unless disputed.")
        if not isinstance(basis, str) or (status == "disputed" and not basis.strip()):
            raise ValueError(f"Record the ambiguity and its source for disputed term {term_id}.")
        if any(MARKER.search(item) or any(c in item for c in "⟦⟧") or BARE_TOKEN.search(item)
               for item in [*forms, *allowed, target]):
            raise ValueError(f"Reserved token syntax in {term_id}.")
        seen.add(term_id)
        terms.append({
            "id": term_id, "source_forms": sorted({form.strip() for form in forms}), "target": target.strip(),
            "allowed_targets": sorted({form.strip() for form in allowed}),
            "unit_ids": sorted(set(scope)), "case_sensitive": entry.get("case_sensitive") is True,
            "selection_status": status, "basis": basis,
        })
    exceptions = data.get("exceptions", [])
    if not isinstance(exceptions, list):
        raise ValueError("Glossary exceptions must be an array.")
    keys = set()
    for entry in exceptions:
        if not isinstance(entry, dict) or not isinstance(entry.get("reason"), str) or entry["reason"] not in TERM_EXCEPTIONS:
            raise ValueError("Unsupported glossary exception.")
        term_id, sentence_id, occurrence = (entry.get(key) for key in ("term_id", "sentence_id", "occurrence"))
        if not isinstance(term_id, str) or term_id not in seen or not isinstance(sentence_id, str) or type(occurrence) is not int or occurrence < 1:
            raise ValueError("An exception must identify an existing term, source sentence and positive occurrence.")
        key = (term_id, sentence_id, occurrence)
        if key in keys or not isinstance(entry.get("basis"), str) or not entry["basis"].strip():
            raise ValueError("Each exception needs a unique occurrence and a reason grounded in source or user intent.")
        target = entry.get("target", "")
        if (not isinstance(target, str) or any(c in target for c in "⟦⟧") or BARE_TOKEN.search(target)
                or (entry["reason"] in {"approved_expansion", "not_a_term"} and not target.strip())):
            raise ValueError(f"Invalid exception target for {key}.")
        keys.add(key)
    return {"terms": terms, "exceptions": exceptions}


def term_patterns(terms: list[dict]) -> list[tuple[dict, re.Pattern]]:
    result = []
    for term in terms:
        options = [r"\s+".join(re.escape(piece) for piece in form.split())
                   for form in sorted(term["source_forms"], key=lambda s: (-len(s), s))]
        flags = 0 if term["case_sensitive"] else re.I
        result.append((term, re.compile(r"(?<!\w)(?:" + "|".join(options) + r")(?!\w)", flags)))
    return result


def mentions(unit: dict, protected: list[dict], rules: list[tuple[dict, re.Pattern]]) -> list[dict]:
    found = []
    for term, pattern in rules:
        if term["unit_ids"] and unit["id"] not in term["unit_ids"]:
            continue
        for match in pattern.finditer(unit["text"]):
            if not overlaps(match.start(), match.end(), protected):
                found.append({"kind": "TERM", "start": match.start(), "end": match.end(), "source": match[0], "term": term})
    found.sort(key=lambda item: (item["start"], -item["end"]))
    chosen = []
    for item in found:
        if chosen and item["start"] < chosen[-1]["end"]:
            if (item["start"], item["end"]) == (chosen[-1]["start"], chosen[-1]["end"]):
                raise ValueError(f"Overlapping glossary concepts in {unit['id']} at {item['source']!r}; scope the entries.")
            continue
        chosen.append(item)
    return chosen


def group_labels(text: str) -> set[str]:
    labels = set(EXPLICIT_GROUP.findall(text))
    if len(labels) < 2:
        labels.update(BARE_GROUP.findall(text))
    if len(labels) < 2 and re.search(r"\brespectively\b", text, re.I):
        labels.update(label for pair in BARE_PAIR.findall(text) for label in pair if label.isupper())
    return labels if len(labels) >= 2 else set()


def assign_groups(text: str, slots: list[tuple[str, int]], labels: set[str]) -> dict[str, str]:
    """Match explicit, simple one-value group statements or an explicit respective list."""
    if not slots or not labels:
        return {}
    pattern = r"(?<![A-Za-z0-9_])(?:" + "|".join(sorted(labels, key=lambda item: (-len(item), item))) + r")(?![A-Za-z0-9_])"
    positions = list(re.finditer(pattern, text))
    names = {match[0] for match in positions}
    if not positions or len(names) != len(positions):
        return {}
    ordered = sorted(slots, key=lambda item: item[1])
    if len(positions) >= 2 and re.search(r"\brespectively\b|分别|依次", text, re.I):
        after = [(token, start) for token, start in ordered if start > positions[-1].end()]
        before = [(token, start) for token, start in ordered if start < positions[0].start()]
        values = after if len(after) == len(positions) else before
        if len(values) == len(positions):
            return {token: match[0] for (token, _), match in zip(values, positions)}
    associations = {}
    for index, owner in enumerate(positions):
        limit = positions[index + 1].start() if index + 1 < len(positions) else len(text)
        values = [(token, start) for token, start in ordered if owner.end() <= start < limit]
        if len(values) != 1:
            return {}
        token, start = values[0]
        clause = text[owner.end():start]
        if not VALUE_CUE.search(clause) and not re.fullmatch(r"[\s组臂：:=]*", clause):
            return {}
        associations[token] = owner[0]
    return associations


def compile_unit(unit: dict, rules: list[tuple], exceptions: dict, consumed: set[tuple]) -> dict:
    text = unit["text"]
    if any(c in text for c in "⟦⟧") or BARE_TOKEN.search(text):
        raise ValueError(f"Reserved token syntax in source unit {unit['id']}. Supply an unmasked source.")
    protected = protected_spans(text)
    all_items = sorted(protected + mentions(unit, protected, rules), key=lambda item: item["start"])
    sentences = sentence_spans(unit["id"], text, all_items)
    labels = group_labels(text)
    registry = []
    counts = Counter()
    for sentence in sentences:
        within_sentence = Counter()
        items = []
        start = sentence["start"]
        for candidate in all_items:
            if not start <= candidate["start"] < sentence["end"]:
                continue
            item = dict(candidate)
            term = item.pop("term", None)
            namespace = f"TERM_{term['id']}" if term else item["kind"]
            counts[namespace] += 1
            token = f"⟦{namespace}_U{unit['index']:05d}_O{counts[namespace]:03d}⟧"
            item.update(token=token, unit_id=unit["id"], sentence_id=sentence["id"], recovery_status="pending")
            if term:
                within_sentence[term["id"]] += 1
                key = (term["id"], sentence["id"], within_sentence[term["id"]])
                exception = exceptions.get(key)
                item.update(term_id=term["id"], basis=term["basis"], selection_status=term["selection_status"],
                            target=term["target"], allowed_targets=term["allowed_targets"])
                if term["selection_status"] == "disputed":
                    item.update(exception="ambiguous_keep", target=item["source"], allowed_targets=[])
                if exception:
                    consumed.add(key)
                    reason = exception["reason"]
                    choices = {"ambiguous_keep": item["source"], "authorized_omission": ""}
                    item.update(exception=reason, basis=exception["basis"], allowed_targets=[],
                                target=choices.get(reason, exception.get("target", "")))
            items.append(item)
        numerical_slots = [(item["token"], item["start"] - start) for item in items if item["kind"] == "NUM"]
        owners = assign_groups(sentence["text"], numerical_slots, labels)
        masked = sentence["text"]
        for item in reversed(items):
            masked = masked[:item["start"] - start] + item["token"] + masked[item["end"] - start:]
            if item["token"] in owners:
                item["expected_group"] = owners[item["token"]]
        sentence["masked"] = masked.strip()
        registry.extend(items)
    return {"sentences": sentences, "registry": registry, "group_labels": sorted(labels)}


def issue_for(unit: dict, code: str, detail: str, item: dict | None = None, **fields) -> dict:
    result = {"code": code, "detail": detail, "unit_id": unit["id"], "location": unit["location"], **fields}
    if item:
        for field in ("token", "kind", "term_id", "sentence_id", "start", "end", "source", "expected_group"):
            if field in item:
                result[field] = item[field]
    return result


def check_response(unit: dict, response: dict, owners: dict[str, str]) -> dict:
    errors, signals, target_rows = [], [], []
    registry = {item["token"]: item for item in unit["registry"]}
    expected = {sentence["id"] for sentence in unit["sentences"]}
    seen_sentences, occurrences, binding = set(), Counter(), {}
    rows, forms = response.get("sentences"), response.get("term_forms", {})
    if set(response) - {"unit_id", "input_sha256", "sentences", "term_forms"}:
        errors.append(issue_for(unit, "invalid_response_fields", "Extra fields cannot waive translation checks."))
    if not isinstance(rows, list) or not rows or not isinstance(forms, dict):
        errors.append(issue_for(unit, "invalid_response", "Expected a nonempty sentences array and optional term_forms object."))
        return {"errors": errors, "signals": signals, "sentences": [], "registry": unit["registry"]}
    for token, target in forms.items():
        entry = registry.get(token)
        if entry is None or entry["kind"] != "TERM" or target not in [entry["target"], *entry["allowed_targets"]]:
            errors.append(issue_for(unit, "term_form_not_allowed", "The target form is not registered.", entry, requested_token=token))

    for row in rows:
        if not isinstance(row, dict) or set(row) != {"source_sentence_ids", "text"}:
            errors.append(issue_for(unit, "invalid_sentence", "Every target row needs source_sentence_ids and text."))
            continue
        ids, text = row["source_sentence_ids"], row["text"]
        if not strings(ids) or not ids or len(set(ids)) != len(ids) or not set(ids) <= expected or not isinstance(text, str) or not text.strip():
            errors.append(issue_for(unit, "invalid_sentence", "Empty text or invalid source sentence mapping."))
            continue
        seen_sentences.update(ids)
        matches = list(MARKER.finditer(text))
        lexical = MARKER.sub(lambda match: " " * len(match[0]), text)
        bound = [(match[0], match.start()) for match in matches if registry.get(match[0], {}).get("expected_group")]
        observed = assign_groups(lexical, bound, set(unit["group_labels"]))
        for match in matches:
            token = match[0]
            item = registry.get(token)
            occurrences[token] += 1
            if item is None:
                owner = owners.get(token)
                errors.append(issue_for(unit, "token_cross_unit" if owner else "token_unknown",
                                        "This token is not registered to the unit.", token=token, owner_unit_id=owner))
                continue
            if item["sentence_id"] not in ids:
                errors.append(issue_for(unit, "token_cross_sentence", "Token source is absent from this target sentence mapping.",
                                        item, target_source_sentence_ids=ids))
            group = item.get("expected_group")
            if group:
                actual = observed.get(token)
                binding[token] = "unverified" if actual is None else ("matched" if actual == group else "mismatch")
                if actual is None:
                    signals.append(issue_for(unit, "group_binding_unverified", "Check the value's attribution against the source.", item))
                elif actual != group:
                    errors.append(issue_for(unit, "numeric_binding_mismatch",
                                            f"{item['source']} belongs to {group} in the source and is attached to {actual} in the candidate.",
                                            item, observed_group=actual, target_source_sentence_ids=ids))
        if any(c in lexical for c in "⟦⟧") or BARE_TOKEN.search(lexical):
            errors.append(issue_for(unit, "token_malformed", "Unrecognized protection marker."))
        for added in protected_spans(lexical):
            errors.append(issue_for(unit, "unregistered_protected_text", "Use the registered token rather than copying or adding protected text.",
                                    target_excerpt=added["source"], target_source_sentence_ids=ids))

        def replacement(match):
            entry = registry.get(match[0])
            if entry is None:
                return match[0]
            target = forms.get(match[0], entry["target"])
            return target if isinstance(target, str) else entry["target"]

        restored = MARKER.sub(replacement, text).strip()
        if not restored:
            errors.append(issue_for(unit, "empty_restored_sentence", "The recovered target sentence is empty."))
        target_rows.append({"source_sentence_ids": ids, "text": restored})
    for missing in sorted(expected - seen_sentences):
        errors.append(issue_for(unit, "source_sentence_missing", "No target row maps to the source sentence.", sentence_id=missing))
    records = []
    for token, item in registry.items():
        count = occurrences[token]
        if not count:
            code = "term_occurrence_missing" if item["kind"] == "TERM" else "token_missing"
            errors.append(issue_for(unit, code, "Required registered occurrence is absent.", item))
        elif count != 1:
            errors.append(issue_for(unit, "token_duplicate", f"Registered occurrence appears {count} times.", item))
        affected = any(error.get("token") == token for error in errors)
        state = "missing" if not count else ("needs_repair" if affected else "checked")
        records.append({**item, "recovery_status": state, "observed_count": count,
                        "binding_status": binding.get(token, "not_applicable")})
    if not errors:
        records = [{**item, "recovery_status": "restored",
                    "recovered_as": forms.get(item["token"], item["target"])} for item in records]
    return {"errors": errors, "signals": signals, "sentences": target_rows if not errors else [], "registry": records}
