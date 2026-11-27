#!/usr/bin/env python3
"""Reproducible synthetic demo and regression tests for the public Skill."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.dont_write_bytecode = True

from assess import assess_claim, audit, resolve_metadata
from core import Evidence, InputError, atomic_bytes, hash_bytes, json_bytes, read_json
from output import verify_bundle, write_bundle
from rdf import parse_emitted_graphs
from sources import parse_file, prepare

PACKAGE = Path(__file__).resolve().parents[1]
EXAMPLES = PACKAGE / "examples"
CLI = PACKAGE / "scripts" / "np_evidence.py"


def options(work, out, explicit=None):
    return SimpleNamespace(work=work, out=out, input=explicit, ocr="off", ocr_language="eng", ocr_limit=0)


def load_demo(work, out):
    state = prepare(EXAMPLES / "case.json", None, options(work, out))
    draft = read_json(EXAMPLES / "example-draft.json")
    draft["snapshot_id"] = state["snapshot_id"]
    return state, draft


def cli(*args):
    return subprocess.run([sys.executable, "-B", str(CLI), *map(str, args)], capture_output=True,
                          encoding="utf-8", check=False, timeout=35)


def run_demo(out):
    with tempfile.TemporaryDirectory(prefix="np-evidence-demo-") as scratch:
        work, draft_path = Path(scratch) / "work", Path(scratch) / "draft.json"
        prepared = cli("prepare", "--case", EXAMPLES / "case.json", "--work", work, "--out", out)
        if prepared.returncode:
            raise RuntimeError(prepared.stdout + prepared.stderr)
        draft = read_json(EXAMPLES / "example-draft.json")
        draft["snapshot_id"] = json.loads(prepared.stdout)["snapshot_id"]
        atomic_bytes(draft_path, json_bytes(draft))
        built = cli("build", "--work", work, "--out", out, "--draft", draft_path, "--final")
        if built.returncode:
            raise RuntimeError(built.stdout + built.stderr)
        return verify_bundle(out)


class Regression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="np-evidence-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work, self.out = self.root / "work", self.root / "out"
        self.state, self.draft = load_demo(self.work, self.out)
        self.index = Evidence(self.state["sources"])

    def item(self, key):
        return copy.deepcopy(next(c for c in self.draft["claims"] if c["key"] == key))

    def kinds(self, record, index=None):
        tested = assess_claim(record, index or self.index)
        return {c["kind"] for c in tested["checks"]}

    def fixture(self, text, extension=".md"):
        path = self.root / ("article" + extension)
        path.write_text(text, encoding="utf-8")
        return Evidence([parse_file(path, "S1", options(self.work, self.out))])

    def test_example_runs_through_cli(self):
        result = run_demo(self.out)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["case_id"], "fictional-lumenbench-demo")
        self.assertEqual(result["claims"], 20)
        payload = read_json(self.out / "result.json")
        self.assertFalse(payload["contract_unfilled"])
        self.assertEqual(payload["verification"]["unresolved"], 1)
        self.assertEqual(payload["sources"][0]["metadata"]["authors"]["value"], ["Mina Example", "Jian Example"])
        self.assertEqual(len(payload["evidence_locations"]), 22)

    def test_quote_from_different_sentence(self):
        item = self.item("c06")
        item["evidence"][0]["quote"] = self.index.locate(item["evidence"][0])["quote"]
        item["evidence"][0]["anchor"] = "S1-U0005.2"
        self.assertIn("quote_outside_anchor", self.kinds(item))

    def test_entire_multi_sentence_paragraph_needs_narrow_anchor(self):
        item = self.item("c06")
        item["evidence"] = [{"anchor": "S1-U0005"}]
        self.assertIn("narrow_anchor", self.kinds(item))

    def test_single_digit_must_be_in_evidence(self):
        item = self.item("c11")
        item["statement"] = item["statement"].replace("size 1", "size 2")
        item["scope"] = ["batch size 2", "CPU"]
        self.assertIn("number_not_in_span", self.kinds(item))

    def test_wrong_unit_is_detected(self):
        item = self.item("c11")
        item["statement"] = item["statement"].replace("24 ms", "24 s")
        item["object"] = "24 s"
        self.assertIn("value_unit_mismatch", self.kinds(item))

    def test_statistical_operator_is_detected(self):
        index = self.fixture("## Results\n\nCedar reduced noise in the fixed condition (p < 0.05).")
        item = self.item("c15")
        item.update(statement="Cedar reduced noise (p = 0.05).", subject="Cedar", object="noise",
                    scope=["fixed condition"], evidence=[{"anchor": "S1-U0001.1"}])
        self.assertIn("statistical_operator", self.kinds(item, index))

    def test_table_value_needs_support_row(self):
        item = self.item("c19")
        item["evidence"][0]["role"] = "context"
        item["evidence"][1]["role"] = "support"
        self.assertIn("value_support", self.kinds(item))

    def test_reverse_comparison_is_detected(self):
        item = self.item("c10")
        item.update(subject="Model Birch", object="Model Cedar",
                    statement="Model Birch had a higher F1 score than Model Cedar on the held-out LumenBench split.")
        self.assertIn("comparison_direction", self.kinds(item))
        item.update(relation="lower_than", statement=item["statement"].replace("higher", "lower"))
        self.assertNotIn("comparison_direction", self.kinds(item))

    def test_missing_or_flipped_polarity_is_rejected(self):
        item = self.item("c13")
        del item["polarity"]
        self.assertIn("explicit_polarity", self.kinds(item))
        item["polarity"] = "positive"
        self.assertIn("polarity_statement", self.kinds(item))
        item["statement"] = item["statement"].replace("did not differ", "differed")
        self.assertIn("polarity_evidence", self.kinds(item))

    def test_nonsignificance_cannot_become_equivalence(self):
        item = self.item("c13")
        item["statement"] = "Preset P and Preset Q have equivalent median calibration error."
        self.assertIn("null_is_not_equivalence", self.kinds(item))

    def test_short_quote_cannot_remove_a_hedge(self):
        item = self.item("c17")
        item["evidence"][0]["quote"] = "reduce sensor drift over four hours."
        item["statement"] = "The fixed exposure setting reduces sensor drift over four hours."
        item["modality"] = "asserted"
        self.assertIn("modality_strengthened", self.kinds(item))

    def test_hypothesis_remains_qualified_in_rdf(self):
        evaluated = audit(self.state, [self.draft])
        result = write_bundle(self.state, evaluated, self.out, "final")
        r = next(c for c in result["claims"] if c["key"] == "c18")
        trig = (self.out / "nanopubs.trig").read_text(encoding="utf-8")
        self.assertIn(f"<{r['nanopub_uri']}/statement>", trig)
        self.assertIn('/modality> "hypothesis" .', trig)
        self.assertIn('/polarity> "negative" .', trig)
        self.assertFalse(any("<https://example.org/nanopub-evidence/v1/relation/" in line.split()[1]
                             for line in trig.splitlines() if line.startswith("  ")))

    def test_association_cannot_become_causality(self):
        item = self.item("c14")
        item.update(relation="causes", statement="Lower light causes more false positives in the dusk subset.")
        self.assertIn("association_as_effect", self.kinds(item))

    def test_bibliography_doi_never_becomes_primary_identifier(self):
        sources, issues = resolve_metadata(self.state, [], self.index)
        self.assertIsNone(sources[0]["metadata"]["doi"]["value"])
        self.assertTrue(sources[0]["identifier"].startswith("sha256:"))
        wrong = {"source_id": "S1", "fields": {"doi": {
            "value": "10.9999/example.2024.1010", "anchor": "S1-U0012.1"
        }}}
        _, issues = resolve_metadata(self.state, [wrong], self.index)
        self.assertEqual(issues[0]["kind"], "metadata_source")

    def test_nested_json_reference_doi_is_not_front_matter(self):
        path = self.root / "structured.json"
        path.write_bytes(json_bytes({"title": "Example comparison", "results": ["Cedar reached 91%."],
                                     "references": [{"title": "Cited title", "doi": "10.9999/cited"}]}))
        source = parse_file(path, "S1", options(self.work, self.out))
        self.assertFalse(any(candidate["field"] == "doi" for candidate in source["metadata_candidates"]))

    def test_structured_numeric_field_has_exact_pointer(self):
        index = self.fixture('{"measurements":[{"gain": 5.25}]}', ".json")
        proof = index.locate({"anchor": "S1-U0001.1", "quote": "5.25"})
        self.assertEqual(proof["locator"]["json_pointer"], "/measurements/0/gain")
        self.assertEqual(proof["quote"], "5.25")

    def test_missing_explicit_source_does_not_pick_another(self):
        case = self.root / "case.json"
        case.write_bytes(json_bytes({"case_id": "missing", "material": {"path": "missing.md"}}))
        (self.root / "unrelated.md").write_text("# Unrelated\n\nUnrelated content.", encoding="utf-8")
        result = cli("prepare", "--case", case, "--work", self.work, "--out", self.out)
        self.assertEqual(result.returncode, 0, result.stdout)
        payload = read_json(self.out / "result.json")
        self.assertEqual(payload["status"], "unable_to_determine")
        self.assertFalse(payload["sources"])

    def test_unicode_offsets_and_negation(self):
        index = self.fixture("## 结果\n\n在💡条件下，模型甲未超过模型乙。模型甲耗时4毫秒。")
        quote = "模型甲未超过模型乙"
        proof = index.locate({"anchor": "S1-U0001.1", "quote": quote})
        self.assertEqual(proof["locator"]["char_start"], len("在💡条件下，"))
        item = self.item("c10")
        item.update(subject="模型甲", object="模型乙", relation="outperforms", statement="模型甲超过模型乙。",
                    metric="表现", scope=["💡条件"], evidence=[{"anchor": "S1-U0001.1", "quote": quote}])
        self.assertIn("polarity_evidence", self.kinds(item, index))

    def test_repeated_quote_requires_offset(self):
        index = self.fixture("## Results\n\nCedar and Cedar are equivalent labels.")
        with self.assertRaisesRegex(InputError, "repeats"):
            index.locate({"anchor": "S1-U0001.1", "quote": "Cedar"})
        proof = index.locate({"anchor": "S1-U0001.1", "quote": "Cedar", "start": 10})
        self.assertEqual(proof["locator"]["char_start"], 10)

    def test_conflict_needs_distinct_counterevidence(self):
        item = {"key": "u9", "status": "conflict", "statement": "Two contradictory statements.",
                "reason": "One selected source is missing.", "evidence": [{"anchor": "S1-U0005.1"}]}
        self.assertIn("conflict_anchors", self.kinds(item))

    def test_scope_dropped_is_visible(self):
        item = self.item("c15")
        item["scope"] = []
        item["statement"] = "Filter Indigo reduced measurement noise by 18%."
        self.assertIn("scope_missing", self.kinds(item))

    def test_citation_marker_must_match_reference(self):
        index = self.fixture("## Results\n\nHart et al. [1] report that Model Cedar uses temperature scaling.\n\n"
                             "## References\n\n[1] Hart Example. Fictional work. 2024. DOI: 10.9999/cited\n\n"
                             "[2] Blair Example. Other fiction. 2023. DOI: 10.9999/other")
        item = self.item("c05")
        item.update(origin="cited_work", citation="[1]", evidence=[
            {"anchor": "S1-U0001.1"}, {"anchor": "S1-U0003", "role": "reference"}
        ])
        self.assertIn("citation_mismatch", self.kinds(item, index))
        item["evidence"][1]["anchor"] = "S1-U0002"
        self.assertEqual(assess_claim(item, index)["cited_doi"], "10.9999/cited")

    def test_revisions_replace_one_key_and_duplicates_merge(self):
        item = self.item("c06")
        item["key"] = "copy-c06"
        fix = self.item("c07")
        fix["basis"] = "A revised explanation grounded in the same sentence."
        evaluated = audit(self.state, [self.draft, {"snapshot_id": self.state["snapshot_id"], "claims": [item, fix]}])
        result = write_bundle(self.state, evaluated, self.out, "final")
        self.assertEqual(len(result["claims"]), 20)
        self.assertEqual([r["kind"] for r in evaluated[3]], ["proposal_revision", "duplicate_evidence_merged"])

    def test_changed_file_invalidates_work_and_previous_output(self):
        path = self.root / "paper.md"
        path.write_bytes((EXAMPLES / "fictional-study.md").read_bytes())
        prep = cli("prepare", "--input", path, "--work", self.work, "--out", self.out)
        draft = {**self.draft, "snapshot_id": json.loads(prep.stdout)["snapshot_id"]}
        draft_path = self.root / "draft.json"
        draft_path.write_bytes(json_bytes(draft))
        args = ("build", "--work", self.work, "--out", self.out, "--draft", draft_path, "--final")
        self.assertEqual(cli(*args).returncode, 0)
        path.write_text("# Replacement document\n\nNot the original.", encoding="utf-8")
        self.assertEqual(cli(*args).returncode, 1)
        result = read_json(self.out / "result.json")
        self.assertEqual(result["phase"], "invalidated")
        self.assertEqual(result["verification"]["accepted"], 0)

    def test_wrong_snapshot_draft_does_not_overwrite_valid_output(self):
        write_bundle(self.state, audit(self.state, [self.draft]), self.out, "final")
        good = (self.out / "result.json").read_bytes()
        with self.assertRaisesRegex(InputError, "snapshot_id"):
            audit(self.state, [{**self.draft, "snapshot_id": "not-current"}])
        self.assertEqual((self.out / "result.json").read_bytes(), good)

    def test_case_projection_is_typed_and_unknown_fields_mark_partial(self):
        state = copy.deepcopy(self.state)
        fields = state["contract"]["properties"]
        fields["status"] = {"enum": ["成功", "部分完成"]}
        fields["measurement_type"] = {"type": "string"}
        fields["assertions"] = {"type": "array", "items": {"type": "object",
                                "properties": {"claim_id": {"type": "string"}, "statement": {"type": "string"}},
                                "required": ["claim_id", "statement"], "additionalProperties": False}}
        result = write_bundle(state, audit(state, [self.draft]), self.out, "final")
        self.assertEqual(result["status"], "部分完成")
        self.assertIsNone(result["measurement_type"])
        self.assertEqual(set(result["assertions"][0]), {"claim_id", "statement"})
        draft = {**self.draft, "contract_values": {"measurement_type": "optical", "status": "pretend"}}
        result = write_bundle(state, audit(state, [draft]), self.out, "final")
        self.assertEqual(result["status"], "成功")
        self.assertEqual(result["measurement_type"], "optical")

    def test_rebuild_is_reproducible(self):
        a = write_bundle(self.state, audit(self.state, [self.draft]), self.out, "final")
        b = write_bundle(self.state, audit(self.state, [self.draft]), self.out, "final")
        self.assertEqual(a, b)

    def test_unclosed_trig_rejected_even_with_changed_manifest(self):
        write_bundle(self.state, audit(self.state, [self.draft]), self.out, "final")
        path = self.out / "nanopubs.trig"
        bad = path.read_bytes().rstrip()[:-1]
        with self.assertRaisesRegex(InputError, "Unclosed graph"):
            parse_emitted_graphs(bad)
        path.write_bytes(bad)
        result = read_json(self.out / "result.json")
        result["file_sha256"]["nanopubs.trig"] = hash_bytes(bad)
        (self.out / "result.json").write_bytes(json_bytes(result))
        with self.assertRaisesRegex(InputError, "TriG differs"):
            verify_bundle(self.out)

    def test_emitted_trig_parser_checks_quoted_and_escaped_literals(self):
        text = '# test\n<https://example.org/g> {\n  <https://example.org/s> <https://example.org/p> "A \\"B\\"\\nC" .\n}\n'
        parsed = parse_emitted_graphs(text.encode("utf-8"))
        self.assertEqual(len(parsed["https://example.org/g"]), 1)
        with self.assertRaisesRegex(InputError, "Unexpected"):
            parse_emitted_graphs(text.replace(r"\nC", r"\qC").encode("utf-8"))

    def test_unreported_claim_retains_search_boundaries(self):
        result = write_bundle(self.state, audit(self.state, [self.draft]), self.out, "final")
        record = next(e for e in result["uncertainty"] if e["kind"] == "not_reported")
        self.assertIn("Results", record["searched"])
        self.assertEqual(result["verification"]["unresolved"], 1)

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "PDF fixture writer requires pypdf")
    def test_text_pdf_and_a_blank_page(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        text = DecodedStreamObject()
        text.set_data(
            b"BT /F1 12 Tf 50 720 Td (Results) Tj 0 -20 Td "
            b"(On the test split, Model Cedar reached 92 percent recall.) Tj ET"
        )
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        page[NameObject("/Contents")] = text
        writer.add_blank_page(width=612, height=792)
        path = self.root / "two-pages.pdf"
        writer.write(path)
        source = parse_file(path, "S1", options(self.work, self.out))
        self.assertEqual(source["pages"], 2)
        self.assertEqual([p["page"] for p in source["unreadable"]], [2])
        unit = next(u for u in source["units"] if "Model Cedar" in u["text"])
        self.assertEqual(unit["page"], 1)
        self.assertEqual(unit["section"], "Results")
        proof = Evidence([source]).locate({"anchor": unit["id"] + ".1"})
        self.assertIn("92 percent recall", proof["quote"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--out", default="demo-output")
    args = parser.parse_args()
    if args.demo:
        print(json.dumps(run_demo(Path(args.out).resolve()), ensure_ascii=False, indent=2))
        return 0
    tested = unittest.main(argv=[sys.argv[0]], verbosity=2, exit=False)
    return 0 if tested.result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
