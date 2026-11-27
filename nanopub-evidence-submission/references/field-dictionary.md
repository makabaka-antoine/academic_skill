# Claim and output fields

## Draft input

The JSON draft carries `snapshot_id` from `prepare` and a `claims` array. Additional draft files may revise a previous claim by `key`. Keys are unique within each file. Revisions are rechecked against the same source snapshot.

| Field | Meaning |
|---|---|
| `key` | Stable proposal key, such as `c01` |
| `status` | `reported`, `not_reported`, `unclear`, `conflict` or `unsupported` |
| `statement` | Self-contained claim or unresolved question |
| `subject`, `relation`, `object` | One subject, directed relation and object. Required for reported claims |
| `metric` | Measurement or comparator property, when applicable |
| `scope` | Array of necessary contextual qualifiers. Required for reported claims. Use an empty array when there is no additional condition |
| `polarity` | `positive` or `negative`. Required and explicit |
| `modality` | `asserted`, `hedged` or `hypothesis`. Required and explicit |
| `origin` | `this_work`, `cited_work` or `background` |
| `evidence_type` | `direct` if the source states the claim, `indirect` if several anchored premises support an explicitly explained inference |
| `claim_type` | Optional role such as `finding`, `method`, `data`, `definition`, `limitation` or `hypothesis` |
| `basis` | Brief account of how the selected spans support the exact scope, direction, values and attribution |
| `citation` | Printed reference marker or author-year text for an attributed finding |
| `subject_in_source`, `object_in_source` | Exact source-language form when the output uses a translation or explained coreference |
| `relation_label` | Precise predicate wording for relation `other` |
| `reason`, `searched` | Explain the unresolved claim in `reason`. Give reviewed sections, pages or anchors as an array of strings in `searched`. Both are needed for `not_reported` |

Every evidence item has an `anchor` from `read`. For example `S1-U0012.2` selects the second sentence or row of the named unit. `quote` is optional. Omit it for the whole selected span. If provided it must be an exact nonempty substring. Repeated text requires `start`, a zero-based offset into the unit. The role defaults to `support`. Additional roles are `context`, `counter` and `reference`. A conflicting claim carries separate support and counter anchors. Attach table headers and captions as context when needed. Use the actual measurement row as support.

Bibliography and acknowledgments can serve as context or reference locations. Cite the body sentence that attributes a claim to a prior work, with its bibliography entry as `reference`.

An unresolved question can omit reported-only fields such as polarity, modality and relation. For example:

~~~json
{
  "key": "u01", "status": "not_reported",
  "statement": "The study reports winter inference time.",
  "reason": "Results give recall and error. A winter timing result is absent from the inspected sections.",
  "searched": ["Methods", "Results, paragraphs 2–4", "Supplementary Table 1"]
}
~~~

## Source fields

The file manifest records SHA-256 of the original bytes. Its `metadata` contains `title`, `authors`, `year`, `doi`, `arxiv_id`, `version`, `venue` and `url`. Each confirmed value includes a method and anchored evidence. Unknown values are null. Author names preserve the order in the selected span.

Optional draft input for a metadata selection:

~~~json
{
  "sources": [{
    "source_id": "S1",
    "fields": {
      "title": {"value": "Visible title as printed", "anchor": "S1-U0001"},
      "year": {"value": 2026, "anchor": "S1-U0002", "quote": "Published: 2026"}
    }
  }]
}
~~~

These shown fields are part of the same draft object as `snapshot_id` and `claims`. Use an explicit print or front-matter declaration for a bibliographic identifier. Identify each exact file version by SHA-256 even when a DOI or arXiv identifier is available. For citation records, retain the link to the observed work separately.

## Relation vocabulary

Use the explicit orientation of the printed evidence.

| Relation | Use |
|---|---|
| `has_value` | The subject has the printed number, range or uncertainty on `metric`. Include the unit in the object |
| `higher_than`, `lower_than` | Numerical ordering of the named measure |
| `outperforms` | Better on a stated evaluation criterion. Some metrics improve when their values fall |
| `differs_from` | Difference on the stated endpoint. For a bounded null result set polarity to negative and retain its threshold |
| `increases`, `decreases`, `affects` | Explicit effect on the named outcome and conditions |
| `associated_with` | Association, co-occurrence or correlation |
| `causes` | An effect the source explicitly attributes to a cause |
| `predicts`, `detects`, `supports`, `consistent_with` | Prediction, observed detection, evidence for a proposition or agreement with a hypothesis |
| `uses`, `evaluated_on`, `trained_on`, `produces`, `requires` | Method, data role, output or condition necessary for an outcome |
| `contains`, `part_of` | Whole to part, and part to whole, respectively |
| `located_in`, `derived_from`, `is_a`, `limits` | Location, origin, class or scope of applicability |
| `other` | More precise predicate described in `relation_label` |

## Case contract and artifacts

`output_contract` controls the keys and types of `result.json`. The mapper supports field lists, field maps and common JSON Schema `properties`, `required`, `type`, `enum` and array bounds. Use custom `contract_values` for case-specific fields that require judgment. Use case-appropriate types and refer to actual claim or evidence IDs. If a field is still unresolved, see `contract_unfilled` and its reason. `claim_audit.json`, `claims.jsonl` and TriG retain the canonical records when a case requires a narrower JSON projection.

For example, `assertions` projects the accepted claims automatically. For a requested custom string `review_summary`, add `"contract_values": {"review_summary": "The bounded recall comparison is supported by c02 and c03."}` to the draft, using the actual records from the run.

| Output field | Interpretation |
|---|---|
| `case_id` | Identifier from the original case. Null when no identifier was supplied |
| `input`, `sources` | Source list, versions, readability, parser states and anchored bibliographic fields |
| `claims` | Accepted atomic propositions, with evidence and alignment confidence |
| `coverage` | `complete` true only after comparing the run to the actual requested scope. `scope` summarizes what was read. `gaps` lists omissions |
| `uncertainty` | Missing fields, unparsed pages, coverage gaps and decisions that need review |
| `change_records` | Indexing, proposal revisions and merging of equivalent proposals |
| `status` | `success`, `partial`, `unable_to_determine` or `failed`, projected to compatible contract enums when requested |
| `file_sha256` | Digests of committed companion artifacts |

A reported and accepted claim has an `id` for the scoped proposition and a `nanopub_uri` for this specific draft, which also binds the selected evidence and metadata. The `evidence` array lists evidence IDs, roles, exact quotes, source IDs and locators. Physical PDF pages are one-based. Character offsets in the extracted unit are Unicode code points with an inclusive start and exclusive end. JSON Pointer, raw lines or PDF block bounds appear when available.

Confidence describes alignment confidence in the extraction. Its `basis` records the analyst's reasoning and specific diagnostics. Warnings remain visible. A rejected proposal stays in `claim_audit.json` and never becomes an accepted claim.
