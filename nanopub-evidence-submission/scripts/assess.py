"""Audit grounded proposals. Language cues are diagnostic, never entailment proofs."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from core import Evidence, InputError, META_FIELDS, RELATIONS, clean, fingerprint, folded_match, issue, words
from sources import DOI, blank_metadata

STATUSES = {"reported", "not_reported", "unclear", "conflict", "unsupported"}
ORIGINS = {"this_work", "cited_work", "background"}
MODALITIES = {"asserted", "hedged", "hypothesis"}
NEGATION = re.compile(
    r"\b(?:not(?!\s+(?:only|just)\b)|no (?:evidence|difference|association|significant|increase|effect|"
    r"change|correlation|improvement|reduction|benefit|advantage)|neither|failed to|didn['’]t|doesn['’]t|"
    r"wasn['’]t|weren['’]t|cannot (?:detect|support|explain|predict)|non[- ]significant)\b|"
    r"未(?:发现|检测到|观察到|超过|优于|达到|显著|改变|提高|降低|相关|能|见)|并未|"
    r"没有(?:发现|差异|显著|效果|提高|降低|证据|相关)|无(?:显著|统计学|明显)?(?:差异|相关性|证据|变化)|"
    r"不(?:显著|支持|相关|优于|超过|高于|低于|存在差异)",
    re.I,
)
HEDGE = re.compile(r"\b(?:may|might|could|possibly|potentially|likely|suggest(?:s|ed)?|appears? to|"
                   r"seems? to|hypothesi[sz](?:e|ed)|hypothesis|cannot (?:exclude|rule out))\b|"
                   r"可能|或许|推测|初步提示|提示|尚不能排除|有待验证|假说|假设", re.I)
HYPOTHESIS = re.compile(r"\b(?:we hypothesi[sz]e|hypothesis that|we conjecture|we speculate)\b|"
                        r"我们假设|假说认为|推测.*有待验证", re.I)
UNIVERSAL = re.compile(r"\b(?:always|every|all populations|all datasets|universally|never|in all cases)\b|"
                       r"始终|从不|所有(?:人群|数据集|情况)|任何(?:人群|数据集|情况)|普遍适用", re.I)
ASSOC = re.compile(r"\b(?:associat\w*|correlat\w*|covari\w*|linked to|co-?occur\w*)\b|相关|关联|共现", re.I)
EFFECT = re.compile(r"\b(?:caus(?:e|es|ed|ing|al)|lead(?:s|ing)? to|led to|result(?:s|ed|ing)? in|"
                    r"driv(?:es|en|ing)|produced|by random assignment|randomi[sz]ed)\b|导致|造成|引起|随机分配", re.I)
COMPARISON = re.compile(r"\b(higher|greater|larger|more|lower|smaller|less|better|worse)\b.{0,45}?\bthan\b|"
                        r"(高于|大于|多于|低于|小于|少于|优于|劣于)", re.I)
LIMIT = re.compile(r"\b(?:only (?:in|on|at|for|under|when)|limited to|restricted to)\s+([^.;,\n]{3,110})|"
                   r"(?:仅在|只在|仅限于|仅适用于|局限于)([^。；，\n]{2,60})", re.I)
PRONOUN = re.compile(r"^(?:it|this|that|they|these|those|we|the (?:method|model|result)|其|它|该|"
                     r"该方法|此方法|本方法|他们|该结果|这一结果)$", re.I)
CONTRAST = re.compile(r"\b(?:but|whereas|while|although|however)\b|[;；]|但(?:是)?|而(?:在|对)|相比之下", re.I)
COORDINATED = re.compile(r"(?:[;；]|,\s+and\b|，并且|，同时)\s*(?:\w+\s+){0,5}"
                         r"(?:increas\w*|decreas\w*|us(?:es|ed)|outperform\w*|predict\w*|detect\w*|"
                         r"contains?|shows?|提高|降低|检出|增加|减少|优于)", re.I)
NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(?P<value>[+\-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)"
                    r"(?:[eE][+\-]?\d+)?)(?![0-9]|\.\d)(?P<unit>\s*(?:%|‰|°C|°F|"
                    r"(?:milliseconds?|seconds?|minutes?|hours?|days?|years?|fold|"
                    r"GHz|MHz|Hz|kPa|MPa|Pa|km|mm|cm|nm|µm|μm|mg|kg|µg|μg|ms|mL|µL|μL|"
                    r"mM|µM|μM|dB|GB|MB|fps|ms|s|g|h|K|m|L)\b|毫秒|秒|分钟|小时|摄氏度|倍|天))?"
                    r"(?![A-Za-z_])", re.I)
STAT = re.compile(r"(?<![\w])(?P<metric>[pn])\s*(?P<op><=|>=|[<=>≤≥~≈])\s*(?P<value>(?:\d+(?:\.\d+)?|\.\d+)(?:e[+-]?\d+)?)", re.I)
FIELD_MAP = {"arxiv": "arxiv_id", "identifier": "doi"}


def numeric_text(text):
    text = re.sub(r"https?://[^\s<>()]+|\b10\.\d{4,9}/[-._;()/:A-Z0-9]+|"
                  r"\[\s*\d+(?:\s*[-–,;]\s*\d+)*\s*\]|\b(?:fig(?:ure)?|table|section|eq(?:uation)?)\.?\s+\d+\b",
                  " ", text, flags=re.I)
    text = unicodedata.normalize("NFKC", text).replace("−", "-").replace("μ", "µ")
    return re.sub(r"(\d(?:\.\d+)?)\s*[×x]\s*10\s*\^?\s*([+-]\d+)", r"\1e\2", text)


def _quantity(token):
    return Decimal(token.replace(",", ""))


def _unit(unit):
    unit = (unit or "").strip().casefold().replace("µ", "μ")
    return {"seconds": "s", "second": "s", "milliseconds": "ms", "millisecond": "ms", "倍": "fold",
            "天": "day", "days": "day", "毫秒": "ms", "秒": "s", "分钟": "minute", "小时": "h",
            "hours": "h", "hour": "h", "摄氏度": "°c"}.get(unit, unit)


def numbers(text):
    found = set()
    for match in NUMBER.finditer(numeric_text(text)):
        try:
            found.add((_quantity(match["value"]), _unit(match["unit"])))
        except InvalidOperation:
            continue
    return found


def statistics(text):
    return {
        (m["metric"].casefold(), m["op"].replace("≤", "<=").replace("≥", ">="), _quantity(m["value"]))
        for m in STAT.finditer(numeric_text(text))
    }


def string(record, name):
    value = record.get(name)
    return value.strip() if isinstance(value, str) else ""


def strings(value):
    return [x.strip() for x in value if isinstance(x, str) and x.strip()] if isinstance(value, list) else []


def _matching_identifier(value, quote):
    return isinstance(value, str) and value.strip() and value.casefold() in quote.casefold()


def _metadata_value(field, value, proof, index):
    _, unit, _, _, _ = index.anchor(proof["locator"]["anchor"])
    if unit["kind"] not in {"title", "front"}:
        raise InputError("metadata_source", f"{field} needs a front-matter anchor, separate from bibliography and results.")
    text = proof["quote"]
    if field == "authors":
        if not isinstance(value, list) or not value or len(value) != len(strings(value)):
            raise InputError("metadata_value", "authors must be the ordered list of names as printed.")
        positions = []
        for name in value:
            here = clean(text).find(clean(name))
            if here < 0:
                raise InputError("metadata_value", "Each author name must occur in the selected source span.")
            positions.append(here)
        if positions != sorted(set(positions)):
            raise InputError("metadata_value", "Author names must follow the order printed in the source span.")
        return value
    if field == "doi":
        if not isinstance(value, str):
            raise InputError("metadata_value", "DOI must be a string from the article header.")
        doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I)
        if (not DOI.fullmatch(doi) or not _matching_identifier(doi, text)
                or not re.search(r"(?:\bdoi\s*[:：]|https?://(?:dx\.)?doi\.org/)", text, re.I)
                or re.search(r"\b(?:et al\.?|correction to|erratum for|cited in|reference to)\b|\[\d+\]", text, re.I)
                or len({x.casefold().rstrip(".") for x in DOI.findall(text)}) != 1):
            raise InputError("metadata_value", "Point to the primary article's explicit DOI declaration, separated from citations.")
        return doi
    if field == "year":
        year = str(value)
        if not re.fullmatch(r"(?:19|20)\d{2}", year) or not re.search(rf"(?<!\d){year}(?!\d)", text):
            raise InputError("metadata_value", "Select the printed publication or version year.")
        return int(year)
    if field == "arxiv_id" and not (isinstance(value, str) and re.fullmatch(
        r"(?:\d{4}\.\d{4,5}|[A-Za-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", value
    )):
        raise InputError("metadata_value", "Select an explicitly printed arXiv identifier.")
    if field == "url" and not str(value).startswith(("https://", "http://")):
        raise InputError("metadata_value", "Select a URL printed for this source.")
    if not _matching_identifier(value, text) and not (isinstance(value, str) and clean(value) and clean(value) in clean(text)):
        raise InputError("metadata_value", f"The {field} value must be visible in the cited material.")
    return value


def resolve_metadata(state, submitted, index):
    proposals = defaultdict(list)
    issues = []
    for entry in submitted:
        if not isinstance(entry, dict) or not isinstance(entry.get("source_id"), str) or not isinstance(entry.get("fields"), dict):
            issues.append(issue("metadata_shape", "Each source proposal requires source_id and a fields object."))
            continue
        proposals[entry["source_id"]].append(entry["fields"])
    sources = []
    for source in state["sources"]:
        source_id = source["id"]
        metadata = blank_metadata()
        candidates = defaultdict(list)
        for value in source["metadata_candidates"]:
            candidates[value["field"]].append(value)
        for field, entries in candidates.items():
            if len({fingerprint(e["value"]) for e in entries}) == 1:
                item = entries[0]
                try:
                    proof = index.locate(item, "context")
                    value = _metadata_value(field, item["value"], proof, index)
                    metadata[field] = {"value": value, "state": "anchored", "method": item["method"], "evidence": [proof]}
                except InputError:
                    pass
            else:
                metadata[field]["state"] = "conflicting"
                issues.append(issue("metadata_conflict", f"Several front-matter values for {field}. Select the correct declaration.",
                                    severity="warn", source_id=source_id, field=field))
        for fields in proposals.pop(source_id, []):
            for raw_name, item in fields.items():
                name = FIELD_MAP.get(raw_name, raw_name)
                try:
                    if name not in META_FIELDS or not isinstance(item, dict):
                        raise InputError("metadata_shape", f"{raw_name} requires a value, anchor and optional quote.")
                    proof = index.locate({**item, "role": "context"}, "context")
                    if proof["source_id"] != source_id:
                        raise InputError("metadata_source", "The anchor belongs to a different file.")
                    value = _metadata_value(name, item.get("value"), proof, index)
                    prior = metadata[name]
                    if prior.get("value") not in (None, value):
                        issues.append(issue("metadata_revised", f"An anchored selection superseded the automatic candidate for {name}.",
                                            severity="notice", source_id=source_id, field=name))
                    metadata[name] = {"value": value, "state": "anchored", "method": "selected source span", "evidence": [proof]}
                except InputError as error:
                    issues.append(issue(error.kind, str(error), source_id=source_id, field=name))
        summary = {key: source[key] for key in ("id", "name", "sha256", "bytes", "format", "backend", "pages", "unreadable", "warnings", "state")}
        summary["metadata"] = metadata
        summary["missing_metadata"] = [key for key, value in metadata.items() if value["value"] is None]
        summary["identifier"] = next((f"{key}:{metadata[key]['value']}" for key in ("doi", "arxiv_id") if metadata[key]["value"]),
                                     "sha256:" + source["sha256"])
        sources.append(summary)
    for source_id in proposals:
        issues.append(issue("metadata_source", f"No prepared source {source_id!r}.", source_id=source_id))
    return sources, issues


def _span_checks(record, index, claim, errors):
    add = errors.append
    subject, relation, obj = (claim[n] for n in ("subject", "relation", "object"))
    statement = claim["claim_text"]
    selected = [e for e in claim["evidence"] if e["role"] in {"support", "context"}]
    support = [e for e in selected if e["role"] == "support"]
    if not support:
        add(issue("no_support", "Attach at least one support anchor within the actual source."))
        return
    for proof in support:
        anchor = proof["locator"]["anchor"]
        _, unit, sentence, _, _ = index.anchor(anchor)
        if sentence is None and len(unit["spans"]) > 1:
            add(issue("narrow_anchor", f"Select the numbered sentence or row inside {anchor}. Bind each needed span separately."))
    if claim["origin"] == "this_work" and len({e["source_sha256"] for e in support}) > 1:
        add(issue("origin_sources", "Separate direct findings from different works, or make their synthesis explicit."))
    text = "\n".join(e["quote"] for e in selected)
    full = [index.full_context(e) for e in support]
    scope_text = " ".join([statement, subject, obj, claim["metric"] or "", *claim["scope"]])
    available = numbers(text)
    have_numbers = {value for value, unit in available}
    missing = sorted(value for value, _ in numbers(scope_text) if value not in have_numbers)
    if missing:
        add(issue("number_not_in_span", "Number(s) missing from selected support/context spans: " + ", ".join(map(str, missing))))
    explicitly_measured = {value for value, unit in available if unit}
    wrong_units = [(value, unit) for value, unit in numbers(scope_text)
                   if unit and value in explicitly_measured and (value, unit) not in available]
    if wrong_units:
        add(issue("value_unit_mismatch", "Value–unit pair not seen in the evidence: " + ", ".join(f"{v} {u}" for v, u in wrong_units)))
    if statistics(scope_text) - statistics(text):
        add(issue("statistical_operator", "Check the printed statistical operator and value, including = versus <."))
    if relation == "has_value":
        values = {v for v, _ in numbers(obj)}
        observed = {v for e in support for v, _ in numbers(e["quote"])}
        if not values:
            add(issue("value_object", "A has_value object needs the measurement as printed, including its unit when available."))
        elif not values <= observed:
            add(issue("value_support", "The measured object needs to appear in a support span, such as the actual table row."))
    for role, value in (("subject", string(record, "subject_in_source") or subject), ("object", string(record, "object_in_source") or obj)):
        if value and not folded_match(value, text) and not (role == "object" and relation == "has_value"):
            tokens = set(words(value))
            retained = tokens & set(words(text))
            add(issue("entity_surface", f"{role} {value!r} is not an exact token sequence in the selected spans. "
                      "Use its printed form or document its coreference.", "warn" if retained or record.get("evidence_type") == "indirect" else "error"))
    if claim["polarity"] == "positive" and NEGATION.search(statement):
        add(issue("polarity_statement", "The drafted proposition contains a negation inconsistent with positive polarity."))
    evidence_negates = all(NEGATION.search(s) and not CONTRAST.search(s) for s in full)
    if evidence_negates and claim["polarity"] == "positive":
        add(issue("polarity_evidence", "The complete support sentence negates the relation. Preserve negative polarity or select the correct clause."))
    if claim["polarity"] == "negative" and not any(NEGATION.search(s) for s in full):
        add(issue("polarity_unconfirmed", "A negative proposition needs a named negation or explicitly bounded null result.", "warn"))
    if re.search(r"\b(?:equal|equivalent|the same)\b|完全相同|等效", statement, re.I) and any(
        re.search(r"no (?:statistically )?significant difference|not (?:(?:statistically )?significantly differ|differ (?:statistically )?significantly)|无显著差异|差异不显著", x, re.I)
        for x in full
    ):
        add(issue("null_is_not_equivalence", "A nonsignificant difference does not establish equality or equivalence."))
    if claim["modality"] == "asserted" and (HEDGE.search(statement) or all(HEDGE.search(s) and not CONTRAST.search(s) for s in full)):
        add(issue("modality_strengthened", "Retain the hedge or hypothesis from the full support sentence."))
    if claim["modality"] != "hypothesis" and all(HYPOTHESIS.search(s) and not CONTRAST.search(s) for s in full):
        add(issue("hypothesis_as_finding", "The support phrases this as a hypothesis."))
    if relation in {"causes", "affects"} and any(ASSOC.search(s) for s in full) and not any(EFFECT.search(s) for s in full):
        add(issue("association_as_effect", "The support states an association. Use associated_with or locate explicit effect evidence."))
    elif relation == "causes" and not any(EFFECT.search(s) for s in full):
        add(issue("causality_review", "Explicit causal language was not detected. Confirm the paper asserts this direction of cause.", "warn"))
    if UNIVERSAL.search(scope_text) and not UNIVERSAL.search(text):
        add(issue("scope_expansion", "The claim adds a universal scope not found in the selected evidence."))
    for sentence in full:
        for boundary in LIMIT.finditer(sentence):
            phrase = next(x for x in boundary.groups() if x).strip()
            wanted = set(words(phrase)) - {"a", "the", "of", "in", "and", "to", "is", "are"}
            if wanted and not (wanted & set(words(scope_text))):
                add(issue("scope_missing", f"Evidence contains a restriction to preserve: {phrase!r}.", "warn"))
    if relation in {"higher_than", "lower_than", "outperforms"} and claim["polarity"] == "positive":
        _direction(record, claim, full, errors)
        if not claim["metric"] and not claim["scope"]:
            add(issue("comparison_scope", "State the comparison metric, data or experimental condition.", "warn"))
    if COORDINATED.search(statement):
        add(issue("atomicity", "The claim appears to coordinate multiple findings. Check whether each can be independently false.", "warn"))
    if claim["origin"] == "this_work" and any(
        re.match(r"^\s*(?!we\b|our\b)[A-Z][\w-]+(?:\s+et al\.|\s+\[\d+\])", sentence) for sentence in full
    ):
        add(issue("attribution_review", "Evidence appears to attribute the finding to another work. Verify the author and citation.", "warn"))
    if any(e["extraction"] == "ocr" for e in selected):
        add(issue("ocr_review", "Verify this span against the page image, especially values and symbols.", "warn"))


def _direction(record, claim, contexts, issues):
    relation = claim["relation"]
    sub = clean(string(record, "subject_in_source") or claim["subject"])
    obj = clean(string(record, "object_in_source") or claim["object"])
    if not sub or not obj or sub in obj or obj in sub:
        return
    for sentence in contexts:
        folded = clean(sentence)
        match = COMPARISON.search(folded)
        if not match or NEGATION.search(folded) or CONTRAST.search(folded) or folded.count(sub) != 1 or folded.count(obj) != 1:
            continue
        sp, op = folded.index(sub), folded.index(obj)
        if not (min(sp, op) < match.start() < max(sp, op)):
            continue
        adjective = match[1] or match[2]
        low = adjective in {"lower", "smaller", "less", "worse", "低于", "小于", "少于", "劣于"}
        if relation == "outperforms" and adjective not in {"better", "worse", "优于", "劣于"}:
            # Numerical order does not say which direction is better for a metric.
            continue
        if (sp < op) != (low == (relation == "lower_than")):
            issues.append(issue("comparison_direction", "The subject and object reverse the explicit comparison in the support."))
            return


def _reference(record, claim, index, issues):
    if claim["origin"] != "cited_work":
        return
    references = [e for e in claim["evidence"] if e["role"] == "reference"]
    marker = string(record, "citation")
    claim["citation"] = marker or None
    claim["cited_doi"] = None
    if not references:
        issues.append(issue("citation_unresolved", "Attach a bibliography anchor for the cited work or report the missing reference.", "warn"))
    if not marker:
        issues.append(issue("citation_marker", "Record the source's printed citation marker or author and year.", "warn"))
    elif not any(marker.casefold() in index.full_context(e).casefold() for e in claim["evidence"] if e["role"] in {"support", "context"}):
        issues.append(issue("citation_alignment", "The citation marker is absent from the supporting context.", "warn"))
    article = [e["quote"] for e in references if e["locator"]["kind"] == "bibliography"]
    if marker and re.fullmatch(r"\[\d+\]", marker) and article:
        n = marker[1:-1]
        if not any(re.match(rf"^\s*(?:\[{n}\]|{n}[.)])(?:\s|$)", index.full_context(e))
                   for e in references if e["locator"]["kind"] == "bibliography"):
            issues.append(issue("citation_mismatch", f"Bibliography evidence must match citation {marker}."))
    found = {m.rstrip(".,;") for line in article for m in DOI.findall(line)}
    if len(found) == 1:
        claim["cited_doi"] = next(iter(found))


def assess_claim(record, index):
    errors = []
    key = string(record, "key")
    status = string(record, "status")
    evidence_type = string(record, "evidence_type")
    origin = string(record, "origin")
    proposal = {
        "key": key, "status": status, "claim_text": string(record, "statement"),
        "subject": string(record, "subject"), "relation": string(record, "relation"), "object": string(record, "object"),
        "scope": strings(record.get("scope")), "metric": string(record, "metric") or None,
        "polarity": string(record, "polarity"), "modality": string(record, "modality"),
        "claim_type": string(record, "claim_type") or "finding", "relation_label": string(record, "relation_label") or None,
        "evidence_type": evidence_type or None, "origin": origin or None, "evidence": [],
        "basis": string(record, "basis"), "reason": string(record, "reason") or None,
        "searched": strings(record.get("searched")), "citation": None, "cited_doi": None,
    }
    for name in ("subject_in_source", "object_in_source"):
        if string(record, name):
            proposal[name] = string(record, name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", key):
        errors.append(issue("claim_key", "Use a stable proposal key, for example c01."))
    if status not in STATUSES:
        errors.append(issue("claim_status", "Use reported, not_reported, unclear, conflict or unsupported."))
    if not proposal["claim_text"]:
        errors.append(issue("claim_text", "State the proposed claim or the explicit question being audited."))
    evidence = record.get("evidence", [])
    if not isinstance(evidence, list):
        errors.append(issue("evidence_shape", "evidence must be a list of anchor objects."))
        evidence = []
    bound = {}
    for item in evidence:
        try:
            proof = index.locate(item)
            bound[proof["id"]] = proof
        except InputError as error:
            errors.append(issue(error.kind, str(error)))
    proposal["evidence"] = list(bound.values())
    if status == "reported":
        if any(not proposal[key] for key in ("subject", "relation", "object", "basis")):
            errors.append(issue("claim_fields", "A reported claim requires subject, relation, object and a support basis."))
        if proposal["relation"] not in RELATIONS or (proposal["relation"] == "other" and not proposal["relation_label"]):
            errors.append(issue("relation", "Use an enumerated relation, or other with a precise relation_label."))
        if proposal["polarity"] not in {"positive", "negative"}:
            errors.append(issue("explicit_polarity", "Specify positive or negative, including for a negative result."))
        if proposal["modality"] not in MODALITIES:
            errors.append(issue("explicit_modality", "Specify asserted, hedged or hypothesis."))
        if origin not in ORIGINS or evidence_type not in {"direct", "indirect"}:
            errors.append(issue("explicit_attribution", "Specify origin and direct/indirect evidence type."))
        if "scope" not in record or not isinstance(record["scope"], list) or len(proposal["scope"]) != len(record["scope"]):
            errors.append(issue("scope_shape", "Use a scope list with conditions visible in the evidence. Use [] for a statement with no additional condition."))
        if any(PRONOUN.fullmatch(proposal[name]) for name in ("subject", "object")):
            errors.append(issue("entity_pronoun", "Use a named subject and object that make sense independently."))
        if any(e["role"] == "counter" for e in proposal["evidence"]):
            errors.append(issue("counterevidence_reported", "Record conflicting evidence with status=conflict and compare the scopes."))
        if evidence_type == "indirect" and len({e["locator"]["anchor"] for e in proposal["evidence"] if e["role"] in {"support", "context"}}) < 2:
            errors.append(issue("indirect_bridge", "An indirect claim needs separate anchors for the premises used in its reasoning."))
        if not errors:
            _span_checks(record, index, proposal, errors)
            _reference(record, proposal, index, errors)
    else:
        if not proposal["reason"]:
            errors.append(issue("uncertainty_reason", "Explain why the proposed claim remains unresolved."))
        if status == "not_reported" and not proposal["searched"]:
            errors.append(issue("search_scope", "List the inspected source sections or pages for a not_reported decision."))
        supporting = {e["locator"]["anchor"] for e in proposal["evidence"] if e["role"] == "support"}
        counter = {e["locator"]["anchor"] for e in proposal["evidence"] if e["role"] == "counter"}
        if status == "conflict" and not (supporting and counter and supporting != counter):
            errors.append(issue("conflict_anchors", "Attach distinct support and counter anchors for both sides."))
    proposal["checks"] = errors
    return finalize_claim(proposal)


def semantic_key(record, counters=False):
    source_hashes = sorted({e["source_sha256"] for e in record["evidence"] if e["role"] == "support"})
    fields = [record["subject"], record["relation"], record["relation_label"] or "", record["object"],
              record["metric"] or "", record["origin"] or "", record["citation"] or "", record["modality"],
              record["claim_type"]]
    return [list(map(clean, fields)), sorted({clean(value) for value in record["scope"]}),
            sorted((str(v.normalize()), u) for v, u in numbers(record["claim_text"])), source_hashes,
            None if counters else record["polarity"]]


def finalize_claim(claim):
    claim["source_ids"] = list(dict.fromkeys(e["source_id"] for e in claim["evidence"]))
    rejected = any(check["severity"] == "error" for check in claim["checks"])
    claim["decision"] = "rejected" if rejected else ("accepted" if claim["status"] == "reported" else "unresolved")
    stable_id = semantic_key(claim) if claim["status"] == "reported" else [claim["key"], claim["claim_text"], claim["status"]]
    claim["id"] = "claim-" + fingerprint(stable_id)[:24]
    warnings = [i["message"] for i in claim["checks"] if i["severity"] != "notice"]
    level = ("low" if claim["decision"] != "accepted" else
             ("medium" if warnings or claim["evidence_type"] != "direct" else "high"))
    claim["confidence"] = {
        "level": level, "dimension": "evidence_alignment",
        "basis": [x for x in [claim["basis"], *warnings, claim["reason"]] if x],
        "checks": ["exact_locator", "selected_span_numbers", "rule_based_lexical_cues"] if claim["decision"] == "accepted" else [],
    }
    return claim


def audit(state, drafts):
    index = Evidence(state["sources"])
    all_records, source_inputs, changes = {}, [], []
    coverage = None
    contract_values = {}
    for draft in drafts:
        if not isinstance(draft, dict) or draft.get("snapshot_id") != state["snapshot_id"]:
            raise InputError("stale_draft", "Draft snapshot_id must equal the current prepare snapshot. Re-read the source if it changed.")
        if "claims" not in draft or not isinstance(draft["claims"], list):
            raise InputError("draft_shape", "A draft needs a claims list (which can be empty for metadata-only updates).")
        keys = set()
        for record in draft["claims"]:
            if not isinstance(record, dict):
                raise InputError("draft_shape", "Every claim proposal must be a JSON object.")
            key = string(record, "key")
            if not key or key in keys:
                raise InputError("proposal_key", "Every draft needs non-empty, unique claim keys.")
            keys.add(key)
            if key in all_records:
                changes.append({"kind": "proposal_revision", "claim_key": key})
            all_records[key] = record
        if "sources" in draft:
            if not isinstance(draft["sources"], list):
                raise InputError("draft_shape", "sources must be a list of source field proposals.")
            source_inputs.extend(draft["sources"])
        if "coverage" in draft:
            if not isinstance(draft["coverage"], dict):
                raise InputError("draft_shape", "coverage must be an object with complete, scope and gaps.")
            coverage = draft["coverage"]
        if "contract_values" in draft:
            if not isinstance(draft["contract_values"], dict):
                raise InputError("draft_shape", "contract_values must be an object.")
            contract_values.update(draft["contract_values"])
    sources, source_issues = resolve_metadata(state, source_inputs, index)
    tested = [assess_claim(r, index) for r in all_records.values()]
    records, unique = [], {}
    for record in tested:
        if record["decision"] != "accepted" or record["id"] not in unique:
            records.append(record)
            if record["decision"] == "accepted":
                unique[record["id"]] = record
            continue
        previous = unique[record["id"]]
        evidence = {e["id"]: e for e in previous["evidence"] + record["evidence"]}
        previous["evidence"] = list(evidence.values())
        previous.setdefault("merged_keys", []).append(record["key"])
        previous["checks"].extend(c for c in record["checks"] if c not in previous["checks"])
        finalize_claim(previous)
        changes.append({"kind": "duplicate_evidence_merged", "kept": previous["key"], "merged": record["key"]})
    signed = defaultdict(list)
    for record in records:
        if record["decision"] == "accepted":
            signed[fingerprint(semantic_key(record, counters=True))].append(record)
    for variants in signed.values():
        if len({r["polarity"] for r in variants}) > 1:
            for record in variants:
                record["checks"].append(issue("contradictory_proposals", "Opposite polarities in the same evidenced scope. Recheck or describe a conflict."))
                finalize_claim(record)
    return sources, source_issues, records, changes, coverage, contract_values
