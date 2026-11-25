"""Synthetic regression cases for source coverage, individual tokens and numeric ownership."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "minimal"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(EXAMPLE))

from run_example import fixture_responses
from translate import check, plan, prepare, read_json, write_json


def make_args(directory, **overrides):
    fields = {
        "source": EXAMPLE / "source.md", "run_dir": directory, "case_id": "synthetic-test",
        "pages": None, "section": None, "page_markers": True, "transcriptions": None,
        "glossary": EXAMPLE / "glossary.json", "chunk_chars": 5000, "keep_unit": [],
        "responses": None, "reviews": None,
    }
    return SimpleNamespace(**{**fields, **overrides})


class ExampleTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.args = make_args(self.root / "case")
        self.assertEqual(prepare(self.args)[1], 0)
        self.assertEqual(plan(self.args)[1], 0)
        self.planned = read_json(self.args.run_dir / "plan.json")
        self.units = fixture_responses(self.planned)
        self.source_unit = self.planned["units"][0]

    def submit(self):
        write_json(self.args.run_dir / "responses" / "chunk-0001.json", {"units": self.units})
        return check(self.args)[0]

    def aligned(self):
        return [json.loads(line) for line in (self.args.run_dir / "alignment.jsonl").read_text("utf-8").splitlines()]

    def record_review(self):
        write_json(self.args.run_dir / "reviews.json", {
            "method": "fixture_review",
            "reviews": [
                {"unit_id": row["unit_id"], "review_sha256": row["review_sha256"], "findings": []}
                for row in self.aligned() if row["status"] == "translated"
            ],
        })
        return check(self.args)[0]

    def test_missing_unit_reports_its_page(self):
        omitted = self.units.pop()
        report = self.submit()
        missing, = [issue for issue in report["issues"] if issue["code"] == "missing_unit"]
        self.assertEqual(report["status"], "partial")
        self.assertEqual(missing["unit_id"], omitted["unit_id"])
        self.assertEqual(missing["location"]["page"], 3)

    def test_only_wrong_glossary_occurrence_is_identified(self):
        terms = [entry for entry in self.source_unit["registry"] if entry["kind"] == "TERM"]
        self.assertEqual(len(terms), 3)
        self.units[0]["sentences"][1]["text"] = self.units[0]["sentences"][1]["text"].replace(terms[1]["token"], "出错比例")
        report = self.submit()
        missed, = [issue for issue in report["issues"] if issue["code"] == "term_occurrence_missing"]
        self.assertEqual(missed["token"], terms[1]["token"])
        self.assertEqual(missed["sentence_id"], terms[1]["sentence_id"])
        self.assertEqual(missed["location"]["page"], 2)
        records = [entry for entry in self.aligned()[0]["registry"] if entry["kind"] == "TERM"]
        self.assertEqual([entry["recovery_status"] for entry in records], ["checked", "missing", "checked"])

    def test_group_swap_fails_even_with_all_tokens_present(self):
        a, b = [entry["token"] for entry in self.source_unit["registry"] if entry["kind"] == "NUM"]
        for sentence in self.units[0]["sentences"][:2]:
            sentence["text"] = sentence["text"].replace(a, "\0").replace(b, a).replace("\0", b)
        report = self.submit()
        mismatches = [issue for issue in report["issues"] if issue["code"] == "numeric_binding_mismatch"]
        self.assertEqual({(issue["source"], issue["expected_group"], issue["observed_group"]) for issue in mismatches},
                         {("2.1%", "A", "B"), ("3.4%", "B", "A")})
        self.assertTrue(all(issue["sentence_id"] == self.source_unit["sentences"][0]["id"] for issue in mismatches))
        self.assertFalse(any(issue["code"] in {"token_missing", "token_duplicate"} for issue in report["issues"]))

    def test_reorder_while_preserving_ownership_and_split_merge(self):
        rows = self.units[0]["sentences"]
        rows[0], rows[1] = rows[1], rows[0]
        report = self.submit()
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["numeric_bindings"], {"matched": 2})
        self.assertEqual(report["status"], "partial")
        self.assertEqual(len(self.aligned()[1]["target_sentences"][0]["source_sentence_ids"]), 2)
        self.assertEqual(self.record_review()["status"], "success")

    def test_duplicate_and_extra_units_are_rejected(self):
        self.units.extend([self.units[0], {**self.units[1], "unit_id": "invented"}])
        report = self.submit()
        self.assertIn("duplicate_unit", [issue["code"] for issue in report["issues"]])
        self.assertIn("unexpected_unit", [issue["code"] for issue in report["issues"]])

    def test_token_from_other_unit_is_rejected(self):
        token = next(entry["token"] for entry in self.source_unit["registry"] if entry["kind"] == "TERM")
        self.units[1]["sentences"][0]["text"] += token
        report = self.submit()
        drift, = [issue for issue in report["issues"] if issue["code"] == "token_cross_unit"]
        self.assertEqual(drift["owner_unit_id"], self.units[0]["unit_id"])

    def test_wrong_source_sentence_is_rejected(self):
        row = self.units[0]["sentences"][0]
        row["source_sentence_ids"] = self.units[0]["sentences"][-1]["source_sentence_ids"]
        report = self.submit()
        self.assertIn("token_cross_sentence", {issue["code"] for issue in report["issues"]})

    def test_false_retention_cannot_waive_checks(self):
        self.units[1].update(sentences=[], retention="image_unreadable")
        report = self.submit()
        self.assertEqual(report["status"], "partial")
        self.assertIn("invalid_response_fields", {issue["code"] for issue in report["issues"]})
        self.assertIn(self.units[1]["unit_id"], report["units_to_repair"])

    def test_changed_glossary_only_invalidates_affected_unit(self):
        self.submit()
        changed = read_json(EXAMPLE / "glossary.json")
        changed["terms"][0]["target"] = "错误率"
        self.args.glossary = self.root / "updated.json"
        write_json(self.args.glossary, changed)
        plan(self.args)
        report = check(self.args)[0]
        stale, = [issue for issue in report["issues"] if issue["code"] == "stale_unit"]
        self.assertEqual(stale["unit_id"], self.units[0]["unit_id"])
        self.assertEqual(self.aligned()[1]["status"], "translated")

    def test_changed_target_requires_fresh_review(self):
        self.submit()
        self.assertEqual(self.record_review()["status"], "success")
        sentence = self.units[1]["sentences"][0]
        sentence["text"] = sentence["text"].replace("作者未报告", "作者没有报告")
        report = self.submit()
        self.assertEqual(report["units_awaiting_review"], [self.units[1]["unit_id"]])

    def test_glossary_omission_is_a_specific_registered_occurrence(self):
        glossary = read_json(EXAMPLE / "glossary.json")
        glossary["exceptions"] = [{
            "term_id": "T0003", "sentence_id": self.source_unit["sentences"][1]["id"], "occurrence": 1,
            "reason": "authorized_omission", "basis": "Synthetic authorization to omit the repeated metric name here.",
        }]
        self.args.glossary = self.root / "exceptions.json"
        write_json(self.args.glossary, glossary)
        plan(self.args)
        self.units = fixture_responses(read_json(self.args.run_dir / "plan.json"))
        self.units[0]["sentences"][-1]["text"] = self.units[0]["sentences"][-1]["text"].replace("测量了", "对该指标进行了测量")
        report = self.submit()
        self.assertEqual(report["term_occurrences"]["listed_exceptions"], 1)
        self.assertEqual(report["issues"], [])


class SourceTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)

    def test_explicit_missing_page_remains_in_coverage(self):
        args = make_args(self.root / "case", pages="2-4")
        prepare(args)
        plan(args)
        response = fixture_responses(read_json(args.run_dir / "plan.json"))
        write_json(args.run_dir / "responses" / "sample.json", {"units": response})
        report = check(args)[0]
        self.assertEqual(report["coverage"]["requested_pages"], 3)
        self.assertEqual(report["coverage"]["pages_with_text"], 2)
        self.assertIn({"code": "no_verified_text", "location": {"page": 4}, "detail": None}, report["issues"])

    def test_section_scope_and_table_cells(self):
        path = self.root / "source.md"
        path.write_text("# Intro\n\nBefore.\n\n# Results\n\n| Group | Rate |\n| --- | --- |\n| A | 2.1% |\n\n# Other\n\nAfter.\n", encoding="utf-8")
        args = make_args(self.root / "case", source=path, page_markers=False, section="Results")
        prepare(args)
        data = read_json(args.run_dir / "source_map.json")
        self.assertEqual(data["units"][0]["section"], ["Results"])
        cells = [unit for unit in data["units"] if unit["kind"] == "table_cell"]
        self.assertEqual([unit["location"]["column"] for unit in cells], [1, 2, 1, 2])
        self.assertEqual(cells[-1]["text"].strip(), "2.1%")

    def test_verified_transcription_record_is_retained(self):
        corrections = self.root / "corrections.json"
        write_json(corrections, [{"page": 2, "before": "2.l%", "after": "2.1%", "evidence": "synthetic p.2 crop C1"}])
        args = make_args(self.root / "case", transcriptions=corrections)
        prepare(args)
        source = read_json(args.run_dir / "source_map.json")
        self.assertEqual(source["transcriptions"][0]["before"], "2.l%")

    def test_missing_input_has_structured_result_and_distinct_reason(self):
        run = self.root / "missing"
        cmd = [sys.executable, "-B", str(ROOT / "scripts/translate.py"), "prepare",
               "--source", str(self.root / "absent.pdf"), "--run-dir", str(run), "--case-id", "missing"]
        proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(proc.returncode, 1)
        output = json.loads(proc.stdout)
        self.assertEqual((output["status"], output["reason"]), ("blocked", "material_missing"))
        self.assertEqual(output["input"]["case_id"], "missing")
        self.assertEqual(read_json(run / "result.json"), output)

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "Optional PDF dependency unavailable")
    def test_pdf_no_text_layer_and_physical_page_scope(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        first = writer.add_blank_page(width=600, height=800)
        writer.add_blank_page(width=600, height=800)
        first[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({
                NameObject("/F1"): DictionaryObject({
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }),
            }),
        })
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 10 Tf 40 700 Td (Example text.) Tj ET")
        first[NameObject("/Contents")] = writer._add_object(content)
        pdf = self.root / "sample.pdf"
        writer.write(pdf)
        args = make_args(self.root / "selected", source=pdf, pages="1-2", page_markers=False)
        report, code = prepare(args)
        self.assertEqual(code, 0)
        self.assertEqual([page["status"] for page in report["pages"]], ["readable", "no_text_layer"])
        args.run_dir = self.root / "unreadable"
        args.pages = "2"
        report, code = prepare(args)
        self.assertEqual((report["status"], code), ("blocked", 1))
        self.assertEqual(report["pages"][0]["page"], 2)


if __name__ == "__main__":
    unittest.main()
