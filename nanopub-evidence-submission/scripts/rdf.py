"""Serialize qualified, unsigned Nanopublication drafts."""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from core import BASE, VOCAB, VERSION, InputError, fingerprint

NP = "http://www.nanopub.org/nschema#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
PROV = "http://www.w3.org/ns/prov#"
DCT = "http://purl.org/dc/terms/"
OA = "http://www.w3.org/ns/oa#"
XSD = "http://www.w3.org/2001/XMLSchema#"
TOOL = BASE + "software/" + VERSION
ABSOLUTE_IRI = r"<https?://[^\x00-\x20<>\"{}|^\\\x60]+>"
STRING = r'''"(?:[^"\\\r\n]|\\(?:[tbnrf\\"']|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}))*"'''
GRAPH_START = re.compile(rf"({ABSOLUTE_IRI}) \{{")
TRIPLE = re.compile(rf"({ABSOLUTE_IRI}) ({ABSOLUTE_IRI}) ({ABSOLUTE_IRI}|{STRING}(?:\^\^{ABSOLUTE_IRI})?) \.")


def iri(value):
    # Generated IRIs are absolute. Escape disallowed RDF IRIREF characters,
    # including those that could occur in an identifier copied from a paper.
    return "<" + quote(value, safe="/:#?&=+%@[];.,!$()*_-~") + ">"


def lit(value):
    return json.dumps(str(value), ensure_ascii=False)


def graph_text(name, triples):
    return "\n".join([f"{iri(name)} {{", *(f"  {iri(s)} {iri(p)} {o} ." for s, p, o in triples), "}"])


def source_uri(digest):
    return BASE + "source/" + digest


def locator_triples(target, proof):
    loc = proof["locator"]
    selector = target + "/quote"
    triples = [
        (target, RDF + "type", iri(OA + "SpecificResource")),
        (target, OA + "hasSource", iri(source_uri(proof["source_sha256"]))),
        (target, OA + "hasSelector", iri(selector)),
        (selector, RDF + "type", iri(OA + "TextQuoteSelector")),
        (selector, OA + "exact", lit(proof["quote"])),
        (target, VOCAB + "anchor", lit(loc["anchor"])),
        (target, VOCAB + "role", lit(proof["role"])),
        (target, VOCAB + "unitTextSha256", lit(loc["unit_text_sha256"])),
        (target, VOCAB + "startInUnit", f'{lit(loc["char_start"])}^^{iri(XSD + "nonNegativeInteger")}'),
        (target, VOCAB + "endInUnit", f'{lit(loc["char_end"])}^^{iri(XSD + "nonNegativeInteger")}'),
    ]
    for key, term in (("section", "section"), ("table_or_figure", "tableOrFigure"),
                      ("json_pointer", "jsonPointer"), ("lines", "lines"), ("bbox", "blockBoundingBox")):
        value = loc.get(key)
        if value is not None:
            triples.append((target, VOCAB + term, lit(json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value)))
    if loc["page"] is not None:
        triples.append((target, VOCAB + "physicalPage", f'{lit(loc["page"])}^^{iri(XSD + "positiveInteger")}'))
    return triples


def source_triples(source):
    node = source_uri(source["sha256"])
    triples = [
        (node, RDF + "type", iri(PROV + "Entity")),
        (node, DCT + "identifier", lit(source["identifier"])),
        (node, VOCAB + "fileSha256", lit(source["sha256"])),
        (node, VOCAB + "fileName", lit(source["name"])),
    ]
    for key, term in (("title", "title"), ("authors", "creator"), ("year", "issued"),
                      ("version", "hasVersion"), ("venue", "isPartOf"), ("doi", "identifier"),
                      ("arxiv_id", "identifier"), ("url", "identifier")):
        item = source["metadata"][key]
        if item["value"] is None:
            continue
        values = item["value"] if isinstance(item["value"], list) else [item["value"]]
        triples.extend((node, DCT + term, lit(value)) for value in values)
        for proof in item["evidence"]:
            field_node = node + "/" + key
            target = field_node + "/" + proof["id"]
            triples.extend([(node, VOCAB + "bibliographicField", iri(field_node)),
                            (field_node, VOCAB + "field", lit(key)),
                            (field_node, VOCAB + "evidence", iri(target))])
            triples.extend(locator_triples(target, proof))
    return triples


def nanopub_uri(claim, source_map):
    # The claim ID identifies the scoped proposition. The draft graph URI also
    # binds its evidence and bibliographic revisions.
    payload = {k: v for k, v in claim.items() if k not in {"key", "merged_keys", "nanopub_uri"}}
    payload["source_metadata"] = [source_map[s]["metadata"] for s in claim["source_ids"]]
    return BASE + "np/" + fingerprint([VERSION, payload])


def render_trig(claims, sources):
    source_map = {s["id"]: s for s in sources}
    blocks = ["# nanopub-evidence v" + VERSION + " | unsigned, local nanopublication drafts",
              "# Each assertion describes a qualified proposition as rdf:Statement."]
    for claim in claims:
        n = nanopub_uri(claim, source_map)
        a, p, i, statement = n + "/assertion", n + "/provenance", n + "/pubinfo", n + "/statement"
        anchors = sorted({e["source_sha256"] for e in claim["evidence"] if e["role"] == "support"})
        subj = BASE + "entity/" + fingerprint([claim["subject"], anchors])[:24]
        obj = BASE + "entity/" + fingerprint([claim["object"], anchors])[:24]
        relation = VOCAB + "relation/" + claim["relation"]
        head = [(n, RDF + "type", iri(NP + "Nanopublication")), (n, NP + "hasAssertion", iri(a)),
                (n, NP + "hasProvenance", iri(p)), (n, NP + "hasPublicationInfo", iri(i))]
        assertion = [
            (statement, RDF + "type", iri(RDF + "Statement")), (statement, RDF + "subject", iri(subj)),
            (statement, RDF + "predicate", iri(relation)),
            (statement, RDF + "object", lit(claim["object"]) if claim["relation"] == "has_value" else iri(obj)),
            (subj, RDFS + "label", lit(claim["subject"])), (statement, RDF + "value", lit(claim["claim_text"])),
            (statement, VOCAB + "claimId", lit(claim["id"])), (statement, VOCAB + "polarity", lit(claim["polarity"])),
            (statement, VOCAB + "modality", lit(claim["modality"])), (statement, VOCAB + "origin", lit(claim["origin"])),
            (statement, VOCAB + "claimType", lit(claim["claim_type"])),
        ]
        if claim["relation"] != "has_value":
            assertion.append((obj, RDFS + "label", lit(claim["object"])))
        assertion.extend((statement, VOCAB + "condition", lit(scope)) for scope in claim["scope"])
        for key, term in (("metric", "metric"), ("relation_label", "relationLabel"), ("citation", "citation")):
            if claim.get(key):
                assertion.append((statement, VOCAB + term, lit(claim[key])))
        provenance = [(a, VOCAB + "evidenceType", lit(claim["evidence_type"])),
                      (a, VOCAB + "alignmentConfidence", lit(claim["confidence"]["level"]))]
        for source_id in claim["source_ids"]:
            source = source_map[source_id]
            provenance.append((a, PROV + "wasDerivedFrom", iri(source_uri(source["sha256"]))))
            provenance.extend(source_triples(source))
        if claim["cited_doi"]:
            provenance.append((a, VOCAB + "citesWork", iri("https://doi.org/" + claim["cited_doi"])))
        provenance.extend((a, VOCAB + "confidenceBasis", lit(note)) for note in claim["confidence"]["basis"])
        for proof in claim["evidence"]:
            target = n + "/" + proof["id"]
            provenance.append((a, VOCAB + "evidence", iri(target)))
            provenance.extend(locator_triples(target, proof))
        pubinfo = [(n, DCT + "creator", iri(TOOL)), (n, VOCAB + "publicationState", lit("unsigned-draft")),
                   (n, VOCAB + "softwareVersion", lit(VERSION)), (TOOL, RDF + "type", iri(PROV + "SoftwareAgent"))]
        blocks.extend(graph_text(name, list(dict.fromkeys(triples))) for name, triples in
                      ((n + "/Head", head), (a, assertion), (p, provenance), (i, pubinfo)))
    return ("\n\n".join(blocks) + "\n").encode("utf-8")


def parse_emitted_graphs(data):
    """Check the closed TriG grammar emitted here, with no general parser claims."""
    graphs, current = {}, None
    for line_no, raw in enumerate(data.decode("utf-8").split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        start = GRAPH_START.fullmatch(line)
        if start and current is None and start[1][1:-1] not in graphs:
            current = start[1][1:-1]
            graphs[current] = set()
        elif line == "}" and current is not None and graphs[current]:
            current = None
        elif current is not None and (triple := TRIPLE.fullmatch(line)):
            subject, predicate, obj = triple.groups()
            graphs[current].add((subject[1:-1], predicate[1:-1], obj))
        else:
            raise InputError("rdf_syntax", f"Unexpected emitted TriG syntax at line {line_no}.")
    if current is not None:
        raise InputError("rdf_syntax", f"Unclosed graph {current}.")
    return graphs


def check_graph_structure(graphs, accepted):
    roles = {"assertion": "hasAssertion", "provenance": "hasProvenance", "pubinfo": "hasPublicationInfo"}
    expected = {claim["nanopub_uri"] + "/" + role for claim in accepted for role in ("Head", *roles)}
    if set(graphs) != expected or len(expected) != 4 * len(accepted):
        raise InputError("rdf_graphs", "Expected exactly four distinct named graphs for each accepted claim.")
    for claim in accepted:
        n = claim["nanopub_uri"]
        head = graphs[n + "/Head"]
        declarations = {row for row in head if row[1:] == (RDF + "type", iri(NP + "Nanopublication"))}
        if declarations != {(n, RDF + "type", iri(NP + "Nanopublication"))}:
            raise InputError("rdf_head", f"Invalid head for {claim['id']}.")
        for role, predicate in roles.items():
            if {row[2] for row in head if row[:2] == (n, NP + predicate)} != {iri(n + "/" + role)}:
                raise InputError("rdf_head", f"Missing or repeated {role} link for {claim['id']}.")
        if not any(s == n + "/assertion" or o == iri(n + "/assertion") for s, _, o in graphs[n + "/provenance"]):
            raise InputError("rdf_provenance", "Provenance must reference the assertion graph.")
        if not any(s == n or o == iri(n) for s, _, o in graphs[n + "/pubinfo"]):
            raise InputError("rdf_pubinfo", "Publication info must reference the nanopublication.")
