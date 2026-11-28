# Local editorial audit

The audit scripts use Python 3.10+ and its standard library. Resolve the script relative to the installed Skill. Use a UTF-8 source snapshot and separate work/output paths. A layout extraction is a snapshot, so retain a locator back to its PDF or source document.

## Editor commands

Run these from the Skill root or replace the script and asset paths with their resolved paths. The input and report paths below are examples.

```sh
python3 scripts/edit_audit.py inspect source.md --report work/inspect.json
python3 scripts/edit_audit.py apply source.md work/edit-plan.json --output output/revised.md --report work/applied.json
python3 scripts/edit_audit.py check source.md output/revised.md --glossary assets/glossary.example.tsv --language en --report work/final-audit.json
```

- `inspect` captures the source SHA-256, anchors, and character and line positions. Select `--format latex` for an extracted TeX fragment with a non-TeX suffix. To inspect actual glossary entries, add `--glossary` and the target `--language`.
- `apply` is useful for selective edits of the same text format. Supply the inspected `source_sha256` and `edits`, each with `id`, exact `before`, `after`, `types` (array), and a specific `reason`. Use a full sentence for the plan when its markup is supported. If the same text occurs twice, supply the original one-based `line` or zero-based Unicode `start` and keep `before` exact. Offsets exclude a UTF-8 BOM and include original line endings. A stale plan aborts. Invalid or overlapping edits are reported and unaffected edits can still apply. Reopen the actual candidate and repair or report rejected work.
- For selective editing set `"allowed_lines":[[9,11],[14,14]]` (inclusive source line numbers). Every replacement must fit fully inside an allowed interval, including multiline replacements. Use the resulting report for byte-level replay at delivery.
- `check` compares the two saved texts. `strict` checks the observed anchors, including their order. Use `--profile aligned` for matching already-aligned bilingual units when names such as `表 2` and `Table 2` or recognized unit spellings differ. It flags anchor-order changes for review. Neither mode performs citation reformatting or semantic translation. Review authorized structural or formatting differences against the actual task.
- The glossary scan is case-sensitive and literal. It locates explicit variants and conflicting mappings, skips recognized markup and reports possible first-use issues. Check acronym scopes and unlisted variants from the manuscript. Do not auto-apply a glossary match.

Exit codes are `0` for observed checks, `1` for findings or a partially applied edit plan, and `2` for invalid input or execution failure. A partial `apply` may produce an editable file. Read its report before using it. A combined boundary failure writes a report and stops before replacing the candidate. Keep the paths separate and inspect the actual candidate identity in the report.

## Meaning and source review

Pair every changed sentence with the original and mark the entity, evidence attachment, comparison, conditions, quantifier, polarity, uncertainty and causal status. For a dense sentence, do this clause by clause. Verify numbers with their owner, unit and precision. When an unchanged number changes owner, a negation moves, or a reference supports a different clause, revert or repair even if the checker passes.

The checker observes common numeric and unit forms, citation and object markers, basic Markdown and LaTeX syntax, and changes to selected wording cues. It does not parse every macro or unit, interpret scientific claims, read a bibliography source, or verify an image. A cue difference asks for reading, and no cue difference can guarantee semantic preservation. In a new format, audit the actual extracted output as well as reviewing the rendered file where required.

## Case result check

```sh
python3 scripts/check_result.py /input/case.json /app/output/result.json --artifact revised.md
```

The checker discovers conventional artifact fields and tests the named paths and basic PDF, PNG or Office file structure. Pass `--artifact` for additional deliverables under custom field names. It checks the input case ID and supported field types. For full Harbor delivery, use [the provenance and coverage gate](harbor.md). Prose-only requirements, custom schemas, rendering quality and scientific fidelity still require direct review. `manual_review` records remaining structural checks. `issues` uses exit code 1, and execution or input errors use exit code 2.
