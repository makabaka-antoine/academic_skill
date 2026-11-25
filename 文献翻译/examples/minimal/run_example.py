"""Run only the bundled synthetic example, with explicitly labelled fixture responses."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


HERE = Path(__file__).resolve().parent
CLI = HERE.parents[1] / "scripts" / "translate.py"


def fixture_responses(planned: dict) -> list[dict]:
    first, second = planned["units"]
    codes = {kind: [item["token"] for item in first["registry"] if item["kind"] == kind]
             for kind in ("TERM", "NUM", "CITE")}
    a, b, repeat = codes["TERM"]
    a_value, b_value = codes["NUM"]
    cite, = codes["CITE"]
    first_id, second_id = (sentence["id"] for sentence in first["sentences"])
    later = {item["kind"]: item["token"] for item in second["registry"]}
    return [
        {
            "unit_id": first["id"],
            "input_sha256": first["input_sha256"],
            "sentences": [
                {"source_sentence_ids": [first_id], "text": f"A 组的{a}为 {a_value}。"},
                {"source_sentence_ids": [first_id], "text": f"B 组的{b}为 {b_value}。"},
                {"source_sentence_ids": [second_id], "text": f"仅在所测条件下测量了{repeat} {cite}。"},
            ],
        },
        {
            "unit_id": second["id"],
            "input_sha256": second["input_sha256"],
            "sentences": [
                {"source_sentence_ids": [sentence["id"] for sentence in second["sentences"]],
                 "text": f"作者未报告外部验证，示例公式 {later['MATH']} 见{later['LABEL']}。"},
            ],
        },
    ]


def print_run(run: Path) -> dict:
    def call(command, *options, accepted=(0,)):
        result = subprocess.run(
            [sys.executable, "-B", str(CLI), command, "--run-dir", str(run), *map(str, options)],
            encoding="utf-8", capture_output=True, check=False,
        )
        if result.returncode not in accepted:
            raise RuntimeError(f"{command} returned {result.returncode}\n{result.stdout}{result.stderr}")

    call("prepare", "--source", HERE / "source.md", "--case-id", "synthetic-minimal", "--page-markers")
    call("plan", "--glossary", HERE / "glossary.json")
    planned = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    responses = run / "responses"
    responses.mkdir()
    (responses / "fixture.json").write_text(
        json.dumps({"units": fixture_responses(planned)}, ensure_ascii=False), encoding="utf-8",
    )
    call("check", accepted=(1,))
    aligned = [json.loads(line) for line in (run / "alignment.jsonl").read_text(encoding="utf-8").splitlines()]
    reviews = [{"unit_id": row["unit_id"], "review_sha256": row["review_sha256"], "findings": []}
               for row in aligned]
    (run / "reviews.json").write_text(
        json.dumps({"method": "fixture_review", "reviews": reviews}, ensure_ascii=False), encoding="utf-8",
    )
    call("check")
    return json.loads((run / "result.json").read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.run_dir.exists() and (not args.run_dir.is_dir() or any(args.run_dir.iterdir())):
        parser.error("Use a new or empty directory for the example.")
    report = print_run(args.run_dir)
    print(json.dumps({
        "example": "synthetic fixture, no model call", "status": report["status"],
        "coverage": report["coverage"], "term_occurrences": report["term_occurrences"],
        "numeric_bindings": report["numeric_bindings"], "result": str(args.run_dir / "result.json"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
