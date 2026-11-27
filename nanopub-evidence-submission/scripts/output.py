"""Case-contract projection, consistent artifacts and replayable verification."""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from core import VERSION, SCHEMA, InputError, atomic_bytes, fingerprint, hash_bytes, json_bytes, read_json
from rdf import check_graph_structure, nanopub_uri, parse_emitted_graphs, render_trig

CORE = {"case_id", "status", "sources", "uncertainty", "change_records", "claims",
        "schema_version", "snapshot_id", "coverage", "files", "verification", "phase", "input"}
FIELDS = {
    "id": ("id", "claim_id", "assertion_id", "evidence_id"),
    "claim_text": ("claim_text", "statement", "claim", "text", "assertion_text"),
    "subject": ("subject", "s"), "relation": ("relation", "predicate", "p"), "object": ("object", "o"),
    "scope": ("scope", "conditions", "qualifiers"), "quote": ("quote", "excerpt", "evidence_text", "exact_quote"),
    "locator": ("locator", "location", "evidence_location"), "page": ("page", "page_number"),
    "source_id": ("source_id", "document_id"), "nanopub_uri": ("nanopub_uri", "uri", "nanopublication_id"),
    "origin": ("origin", "attribution"),
}


def _name(key):
    return re.sub(r"[\s_-]+", "", str(key)).casefold()


def contract_fields(contract):
    if isinstance(contract, dict):
        for nested_name in ("result.json", "result_json", "result_schema", "schema"):
            if isinstance(contract.get(nested_name), (dict, list)):
                return contract_fields(contract[nested_name])
        if isinstance(contract.get("properties"), dict):
            return contract["properties"]
        for group in ("fields", "required_fields", "required"):
            if isinstance(contract.get(group), (dict, list)):
                return contract_fields(contract[group])
        return {key: value for key, value in contract.items()
                if key not in {"description", "title", "$schema", "type", "format", "file", "filename", "path", "artifacts",
                               "additionalProperties", "required", "notes", "examples"}}
    if isinstance(contract, list):
        fields = {}
        for item in contract:
            if isinstance(item, str):
                fields[item] = {}
            elif isinstance(item, dict):
                key = item.get("name") or item.get("field") or item.get("key")
                if isinstance(key, str):
                    fields[key] = item
        return fields
    return {}


def _spec(value):
    if isinstance(value, dict):
        return value
    if value in ("array", "object", "string", "integer", "number", "boolean"):
        return {"type": value}
    return {}


def _enforce(value, spec):
    spec = _spec(spec)
    if "enum" in spec and isinstance(spec["enum"], list) and value not in spec["enum"]:
        raise ValueError("value is outside the requested enum")
    kind = spec.get("type")
    rules = {"array": lambda x: isinstance(x, list), "object": lambda x: isinstance(x, dict),
             "string": lambda x: isinstance(x, str), "number": lambda x: type(x) in {int, float},
             "integer": lambda x: type(x) is int, "boolean": lambda x: type(x) is bool, "null": lambda x: x is None}
    types = kind if isinstance(kind, list) else [kind]
    applicable = [rules[t] for t in types if isinstance(t, str) and t in rules]
    if applicable and not any(check(value) for check in applicable):
        raise ValueError(f"expected {kind}")
    if isinstance(value, list):
        if type(spec.get("minItems")) is int and len(value) < spec["minItems"]:
            raise ValueError(f"expected at least {spec['minItems']} entries")
        if type(spec.get("maxItems")) is int and len(value) > spec["maxItems"]:
            raise ValueError(f"expected at most {spec['maxItems']} entries")
        return [_enforce(v, spec.get("items", {})) for v in value]
    properties = spec.get("properties")
    if isinstance(value, dict) and isinstance(properties, dict):
        adapted = {} if spec.get("additionalProperties") is False else value.copy()
        for key, child_spec in properties.items():
            if key in value:
                adapted[key] = _enforce(value[key], child_spec)
                continue
            for standard, aliases in FIELDS.items():
                if any(_name(key) == _name(alias) for alias in aliases) and standard in value:
                    adapted[key] = _enforce(value[standard], child_spec)
                    break
        missing = [k for k in spec.get("required", []) if k not in adapted]
        if missing:
            raise ValueError("missing nested field(s): " + ", ".join(missing))
        return adapted
    return value


def status_value(status, spec):
    enum = _spec(spec).get("enum")
    if not isinstance(enum, list) or status in enum:
        return status
    aliases = {
        "success": {"complete", "completed", "ok", "成功", "已完成"},
        "partial": {"partial_success", "partially_complete", "incomplete", "部分完成", "部分成功"},
        "unable_to_determine": {"unknown", "unavailable", "undetermined", "insufficient_evidence", "unable", "无法判断"},
        "failed": {"failure", "error", "运行失败", "失败"},
    }
    return next((entry for entry in enum if isinstance(entry, str) and entry.casefold() in aliases[status]), status)


def _claim_summary(claims):
    return "\n".join(f"{c['claim_text']} [{', '.join(e['locator']['anchor'] for e in c['evidence'] if e['role'] == 'support')}]"
                     for c in claims)


def derive_contract(base, contract, supplied, accepted, records, evidence):
    aliases = {
        "input": {"inputid", "inputidentification", "inputs", "inputfiles", "输入标识"},
        "sources": {"source", "sourcemetadata", "publicationmetadata", "provenance", "bibliography", "来源", "来源元数据"},
        "claims": {"assertions", "atomicclaims", "nanopublications", "nanopubs", "findings", "knowledgeunits", "断言", "原子断言"},
        "uncertainty": {"uncertainties", "limitations", "unknowns", "gaps", "不确定性", "证据边界"},
        "change_records": {"changes", "changelog", "变更记录"},
        "status": {"runstatus", "runstate", "运行状态", "状态"},
        "case_id": {"caseid", "案例标识"}, "verification": {"validation", "checks", "质量检查"},
        "files": {"artifacts", "outputfiles", "export", "serialization", "导出文件"},
    }
    output, unresolved = {}, []
    for key, spec in contract_fields(contract).items():
        normalized = _name(key)
        canonical_name = key if key in base else next((dest for dest, names in aliases.items() if normalized in names), None)
        shape = _spec(spec).get("type")
        known = True
        if key in supplied and key not in CORE and canonical_name not in CORE:
            value = supplied[key]
        elif canonical_name == "status":
            value = status_value(base["status"], spec)
        elif canonical_name == "claims" and shape == "string":
            value = _claim_summary(accepted)
        elif canonical_name == "input":
            value = base["input"]
            if shape == "array":
                value = value["files"]
            elif shape == "string":
                value = (str(value["case_id"]) if value["case_id"] is not None else
                         (value["files"][0]["sha256"] if len(value["files"]) == 1 else None))
        elif canonical_name:
            value = base[canonical_name]
        elif normalized in {"evidence", "evidencelinks", "evidencemap", "evidencelocations", "证据", "证据定位"}:
            value = evidence
        elif normalized in {"unresolvedclaims", "unsupportedclaims", "notreported", "conflicts"}:
            value = [r for r in records if r["decision"] != "accepted"]
        elif normalized in {"answer", "summary", "mainresult", "result", "results", "答案", "主结果", "结果"}:
            value = accepted if shape == "array" else ({"claims": accepted} if shape == "object" else _claim_summary(accepted))
        elif normalized in {"trig", "trigfile", "nanopublicationtrig", "rdf", "导出"}:
            value = "nanopubs.trig"
        else:
            known, value = False, None
        try:
            if not known or value is None:
                raise ValueError("an evidence-derived mapping or an explicit case-specific value is needed")
            output[key] = _enforce(value, spec)
        except ValueError as error:
            output[key] = None
            unresolved.append({"field": key, "reason": str(error)})
    if contract and not contract_fields(contract):
        if not supplied:
            unresolved.append({"field": "output_contract", "reason": "Read the textual contract and supply its case-specific fields."})
        output.update({k: v for k, v in supplied.items() if k not in CORE})
    return output, unresolved


def all_proofs(records, sources):
    proofs = {e["id"]: e for record in records for e in record["evidence"]}
    proofs.update({e["id"]: e for source in sources for field in source["metadata"].values() for e in field["evidence"]})
    return list(proofs.values())


def index_for_proofs(state, proofs):
    ids = {e["locator"]["unit"] for e in proofs}
    return [dict({"source_id": source["id"], "source_sha256": source["sha256"]}, **unit)
            for source in state["sources"] for unit in source["units"] if unit["id"] in ids]


def verify_spans(proofs, index):
    by_unit = {u["id"]: u for u in index}
    for proof in proofs:
        loc = proof["locator"]
        unit = by_unit[loc["unit"]]
        start, end = loc["char_start"], loc["char_end"]
        if loc["sentence"] is None:
            left, right = 0, len(unit["text"])
            anchor = unit["id"]
        else:
            left, right = unit["spans"][loc["sentence"] - 1]
            anchor = f"{unit['id']}.{loc['sentence']}"
        digest = hash_bytes(unit["text"].encode("utf-8"))
        identity = fingerprint([unit["source_sha256"], unit["id"], digest, anchor, start, end, proof["quote"], proof["role"]])
        if (not (left <= start < end <= right) or unit["text"][start:end] != proof["quote"]
                or loc["anchor"] != anchor or loc["unit_text_sha256"] != digest or proof["id"] != "ev-" + identity[:24]
                or proof["source_id"] != unit["source_id"] or proof["source_sha256"] != unit["source_sha256"]
                or any(loc[key] != unit[key] for key in ("page", "section", "bbox", "kind"))):
            raise InputError("locator_roundtrip", f"Locator mismatch at {loc['anchor']}.")


def csv_bytes(records):
    cols = ["id", "claim_text", "subject", "relation", "object", "metric", "scope", "polarity", "modality",
            "origin", "status", "confidence", "source_ids", "evidence_ids", "anchors", "nanopub_uri"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, cols, lineterminator="\n")
    writer.writeheader()
    for record in records:
        flat = {k: record[k] for k in cols if k in record}
        flat.update(scope=json.dumps(record["scope"], ensure_ascii=False),
                    confidence=record["confidence"]["level"], source_ids=" ".join(record["source_ids"]),
                    evidence_ids=" ".join(e["id"] for e in record["evidence"]),
                    anchors=" ".join(e["locator"]["anchor"] for e in record["evidence"]))
        writer.writerow(flat)
    return stream.getvalue().encode("utf-8")


def _review_text(base, records):
    lines = ["# Evidence review", "", f"Case ID {base['case_id']}", f"Status {base['status']}", "",
             "| Proposal | Decision | Confidence | Anchors | Review |", "|---|---|---|---|---|"]
    for r in records:
        anchors = ", ".join(e["locator"]["anchor"] for e in r["evidence"])
        checks = ", ".join(c["kind"] for c in r["checks"])
        lines.append(f"| {r['key']} | {r['decision']} | {r['confidence']['level']} | {anchors} | {checks} |")
    lines += ["", "## Uncertainty", ""]
    for row in base["uncertainty"]:
        message = row.get("message") or row.get("reason") or row.get("field") or row["kind"]
        lines.append("- " + str(message).replace("\n", " "))
    return ("\n".join(lines) + "\n").encode("utf-8")


def check_rdf(rendered, accepted):
    graphs = parse_emitted_graphs(rendered)
    check_graph_structure(graphs, accepted)
    try:
        from rdflib import Dataset
    except ImportError:
        return "closed_subset_parse_and_graph_checks"
    try:
        dataset = Dataset()
        dataset.parse(data=rendered.decode("utf-8"), format="trig")
    except Exception as error:
        raise InputError("rdf_syntax", f"Independent RDF parse failed: {error}") from error
    contexts = {str(g.identifier) for g in dataset.contexts() if len(g)}
    if contexts != set(graphs):
        raise InputError("rdf_graphs", "The independent RDF parser disagrees on the named graphs.")
    return "independent_rdflib_parse"


def write_bundle(state, evaluated, out, phase):
    sources, source_issues, records, changes, coverage, supplied = evaluated
    out = Path(out)
    accepted = [r for r in records if r["decision"] == "accepted"]
    source_map = {s["id"]: s for s in sources}
    for claim in accepted:
        claim["nanopub_uri"] = nanopub_uri(claim, source_map)
    rejected = [r for r in records if r["decision"] == "rejected"]
    unresolved = [r for r in records if r["decision"] == "unresolved"]
    proofs = all_proofs(records, sources)
    proof_index = index_for_proofs(state, proofs)
    verify_spans(proofs, proof_index)
    trig = render_trig(accepted, sources)
    rdf_mode = check_rdf(trig, accepted)
    coverage = coverage or {"complete": False, "scope": "", "gaps": []}
    gaps = coverage.get("gaps", [])
    coverage_done = coverage.get("complete") is True and bool(str(coverage.get("scope") or "").strip()) and gaps == []
    uncertainty = list(state["problems"]) + [c for c in source_issues if c["severity"] != "notice"]
    for src in sources:
        uncertainty.extend({**warning, "source_id": src["id"]} for warning in src["warnings"])
        uncertainty.extend({"kind": "unreadable_page", "source_id": src["id"], **page} for page in src["unreadable"])
        if src["missing_metadata"]:
            uncertainty.append({"kind": "bibliographic_fields_unknown", "source_id": src["id"],
                                "fields": src["missing_metadata"],
                                "message": f"{src['name']}: unconfirmed fields " + ", ".join(src["missing_metadata"])})
    for record in rejected:
        uncertainty.append({"kind": "rejected_claim", "claim_key": record["key"], "checks": record["checks"],
                            "message": f"{record['key']}: " + " | ".join(c["message"] for c in record["checks"] if c["severity"] == "error")})
    for record in unresolved:
        uncertainty.append({"kind": record["status"], "claim_key": record["key"], "reason": record["reason"], "searched": record["searched"]})
    for record in accepted:
        uncertainty.extend({**c, "claim_id": record["id"]} for c in record["checks"] if c["severity"] == "warn")
    if isinstance(gaps, list):
        uncertainty.extend({"kind": "uncovered_scope", "message": str(gap)} for gap in gaps)
    if not coverage_done:
        uncertainty.append({"kind": "coverage_incomplete", "message": "The case's requested scope has yet to be fully audited."})
    if accepted:
        incomplete = bool(state["problems"] or rejected or any(s["unreadable"] for s in sources)
                          or any(s["severity"] == "error" for s in source_issues)
                          or any(r["status"] in {"conflict", "unclear", "unsupported"} for r in unresolved))
        status = "success" if phase == "final" and coverage_done and not incomplete else "partial"
    elif any(s["state"] in {"readable", "partial"} for s in sources) and phase == "prepared":
        status = "partial"
    elif state["problems"] and not sources and {p["kind"] for p in state["problems"]} - {"input_missing", "no_readable_text", "encrypted_pdf"}:
        status = "failed"
    else:
        status = "unable_to_determine"
    descriptor = {"case_id": state["case_id"], "files": [{k: s[k] for k in ("id", "name", "sha256", "format")} for s in sources]}
    files = {key: key for key in ("claims.jsonl", "claims.csv", "nanopubs.trig", "evidence_index.json", "claim_audit.json", "review.md")}
    verification = {"accepted": len(accepted), "rejected": len(rejected), "unresolved": len(unresolved),
                    "locators_reproduced": len(proofs), "rdf_validation": rdf_mode,
                    "signed": False, "human_review": "pending"}
    result = {
        "schema_version": SCHEMA, "skill_version": VERSION, "phase": phase, "case_id": state["case_id"],
        "snapshot_id": state["snapshot_id"], "status": status, "input": descriptor, "sources": sources,
        "claims": accepted, "coverage": coverage, "uncertainty": uncertainty,
        "change_records": [{"kind": "source_indexed", "source_id": s["id"], "backend": s["backend"]} for s in sources] + changes,
        "files": files, "verification": verification,
    }
    fields = contract_fields(state["contract"])
    contract_input = (state["contract"], supplied, accepted, records, all_proofs(accepted, []))
    mapped, unfilled = derive_contract(result, *contract_input)
    if unfilled and result["status"] == "success":
        result["status"] = "partial"
        mapped, unfilled = derive_contract(result, *contract_input)
    result["run_status"] = result["status"]
    result["uncertainty"].extend({"kind": "unfilled_contract", **u} for u in unfilled)
    for key, value in mapped.items():
        if key not in result or (key == "status" and value is not None):
            result[key] = value
        elif value is not None and key in fields and key not in {"uncertainty", "run_status"}:
            result[key] = value
    result["contract_unfilled"] = unfilled
    audited = {"schema_version": SCHEMA, "snapshot_id": state["snapshot_id"], "records": records, "source_issues": source_issues,
               "source_metadata": sources, "verification": verification}
    outputs = {
        "claims.jsonl": b"".join((json.dumps(r, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8") for r in accepted),
        "claims.csv": csv_bytes(accepted), "nanopubs.trig": trig,
        "evidence_index.json": json_bytes({"snapshot_id": state["snapshot_id"], "units": proof_index}),
        "claim_audit.json": json_bytes(audited), "review.md": _review_text(result, records),
    }
    # result.json is the commit record. File digests detect an interrupted or
    # interleaved build even when folded files have already been replaced.
    result["file_sha256"] = {name: hash_bytes(data) for name, data in outputs.items()}
    for name, data in outputs.items():
        atomic_bytes(out / name, data)
    atomic_bytes(out / "result.json", json_bytes(result))
    return result


def verify_bundle(out):
    out = Path(out)
    result = read_json(out / "result.json")
    expected = {"claims.jsonl", "claims.csv", "nanopubs.trig", "evidence_index.json", "claim_audit.json", "review.md"}
    manifest = result.get("file_sha256", {})
    if set(manifest) != expected:
        raise InputError("output_manifest", "The committed output manifest is incomplete.")
    for name, digest in manifest.items():
        if hash_bytes((out / name).read_bytes()) != digest:
            raise InputError("output_hash", f"{name} changed after the bundle was committed.")
    audit = read_json(out / "claim_audit.json")
    index = read_json(out / "evidence_index.json")
    if len({value["snapshot_id"] for value in (audit, index, result)}) != 1:
        raise InputError("output_snapshot", "Artifacts come from different input snapshots.")
    records, sources = audit["records"], audit["source_metadata"]
    accepted = [r for r in records if r["decision"] == "accepted"]
    proofs = all_proofs(records, sources)
    verify_spans(proofs, index["units"])
    jsonl = [json.loads(line) for line in (out / "claims.jsonl").read_text(encoding="utf-8").split("\n") if line]
    if jsonl != accepted or (out / "claims.csv").read_bytes() != csv_bytes(accepted):
        raise InputError("output_alignment", "JSONL, CSV and audit differ.")
    source_map = {s["id"]: s for s in sources}
    if any(nanopub_uri(c, source_map) != c["nanopub_uri"] for c in accepted):
        raise InputError("output_alignment", "A draft graph URI does not match its claim and evidence.")
    rendered = render_trig(accepted, sources)
    if (out / "nanopubs.trig").read_bytes() != rendered:
        raise InputError("output_trig", "TriG differs from canonical serialization of the audited records.")
    return {"ok": True, "case_id": result["case_id"], "status": result["status"],
            "claims": len(accepted), "exact_spans": len(proofs), "rdf": check_rdf(rendered, accepted),
            "contract_unfilled": result["contract_unfilled"]}
