# Harbor execution

Read the live `/input/case.json`, task and real attachments. Take exact `case_id`, field names, nesting and status vocabulary from the live contract. Locate the available `environment/resources.yaml` and use its actual tools. Keep intermediate work in `/app/work` and actual deliverables in `/app/output`. These commands assume the installed root `/skills/weekly-skill`. Resolve the actual root if different.

```sh
python3 /skills/weekly-skill/scripts/case_io.py --case /input/case.json --work /app/work
```

Read `intake.json` and relevant snapshots or native files. For large input directories repeat `--source path` for the required materials. Text has line locators, PDF page locators and Word main-text paragraph locators. Compare PDF columns, figures, superscripts, signs and tables against the real page before citing. Use local OCR or rendering for unreadable pages. Pass a declared PDF text executable with `--pdftotext` if discovery fails. Read the task itself in full, including any prose-only fields. A source excerpt locates the evidence. Its scientific interpretation needs reading.

Before drafting, save `scope.json` in work with one short ID per requested section, deliverable or meaningful aligned unit. Cover every request, including separate evidence notes. For a selective UTF-8 revision, store `"require_replay":true`, and put source `allowed_lines` in the edit plan. Run `edit_audit.py apply` to create the edited copy with the untouched bytes preserved. Keep exact original, revised, position, type and reason per changed sentence or coupled group. Use a read-only audit for diagnosis. For authorized reordering, writing from sources, translation or format conversion, verify exact output excerpts against sources.

Save a useful partial artifact early. Treat absent materials as `material_missing`, conflicts as `judgment_unresolved` and failed operations as `execution_failed`. State the affected scope. Continue on the unaffected portions. Mark the result partial when required text, artifacts or preview are missing, using the case's vocabulary. An open author query can coexist with a completed editing scope when all requested editorial work is delivered.

## Verify and install the result

Write the requested artifacts and the actual draft JSON. Attach the source map, changes, issues and honest status through the required fields. If the contract has no appropriate slot, put the record in `evidence.json` and reference it through an allowed field if possible. Generate and inspect the actual PDF or PNG preview for native Office, slides and editable diagrams. Keep audit material outside manuscript prose.

The internal verification ledger (`ledger.json`) binds units to source and target locations and checks for missing deliverables. For a numeric comparison, give each number its own exact short source and target excerpt. Check the other group's value in a separate unit when necessary. Use `source_exclude` and `target_exclude` for rival labels. For a reviewed structural conversion, choose a target excerpt in the actual result field or a separate inspected preview/extraction record. For a textual proof of native content use an extracted text artifact recorded in the ledger. Independently inspect the real native artifact and its accompanying preview.

```sh
python3 /skills/weekly-skill/scripts/delivery_gate.py \
  --case /input/case.json --intake /app/work/intake.json \
  --scope /app/work/scope.json --ledger /app/work/ledger.json \
  --draft /app/work/result-draft.json --output /app/output \
  --report /app/work/delivery-check.json
```

Fix reported issues, then reopen the installed `result.json` and declared outputs. The gate checks locations, artifacts and observed byte-level changes. Meaning, visual integrity and requirements the contract expresses only in prose still need review. If the helper is unavailable or a native locator is unsupported, perform the checks with available tools and identify what was checked. If recovery is impossible for a required part, leave an honest case-compatible partial result and report the exact issue.

### Ledger example

For `{"units":["comparison"],"require_replay":true}`, the internal ledger could be:

```json
{
  "artifacts": ["revised.md"],
  "replay": {"source":"draft.md","target":"revised.md","report":"applied.json"},
  "units": [
    {
      "id":"comparison", "state":"delivered",
      "sources":[{"file":"draft.md","at":8,"quote":"The reference cohort recorded 17%"}],
      "target":{"file":"revised.md","at":8,"quote":"The reference cohort recorded 17%"},
      "bindings":[{"source_owner":"reference cohort","target_owner":"reference cohort","value":"17%","source_exclude":["pilot cohort"],"target_exclude":["pilot cohort"]}]
    }
  ]
}
```

Use `source_index` (zero-based) for a binding with several sources. Explicitly reviewed equivalents may give `source_value` and `target_value`. A target can use `{"field":["abstract"],"quote":"actual excerpt"}` for embedded prose. A format-only unit can use `"kind":"format_only"` and a target excerpt. A blocked unit has `{"id":"…","state":"blocked","kind":"material_missing","reason":"how this limits delivery"}` and the ledger sets `"partial":true`. Verify the draft status describes the same limitation. The internal keys supplement the actual external contract.
