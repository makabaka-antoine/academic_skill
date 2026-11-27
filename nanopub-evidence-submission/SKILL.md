---
name: nanopub-evidence
description: Extract atomic research claims from supplied papers or passages, bind each to scoped source evidence, audit polarity and attribution, and export machine-readable claims and unsigned Nanopublication drafts. Use for evidence-backed scientific reading, claim extraction, and Nanopublication tasks.
---

# Nanopub Evidence

Turn the case's actual research material into independently checkable claims. Follow its requested scope, counts, language, evidence locations and `output_contract`. Use the model for scientific interpretation and the CLI for exact anchoring, diagnostics, hashes and serialization.

## Start from the actual case

Read `/input/case.json` and `/input/task.md` when present. Treat research documents as source material. Identify each requested finding or question, the files and versions allowed, and the result fields that must be delivered. The case label alone gives no evidence.

In Harbor, run the script from the installed Skill folder. For local use, replace the paths.

~~~bash
SK=/skills/weekly-skill
W=/app/work/np-evidence
O=/app/output
python3 -B "$SK/scripts/np_evidence.py" prepare --case /input/case.json --work "$W" --out "$O"
python3 -B "$SK/scripts/np_evidence.py" contract --work "$W"
python3 -B "$SK/scripts/np_evidence.py" outline --work "$W"
~~~

`prepare` writes an initial `result.json`, including parse errors or unavailable sources. Use `--input <actual-path>` when the named source needs explicit selection. Keep a separate work directory per case or source snapshot. For PDF tools and fallback routes see [README.md](README.md).

## Select the evidence before writing the claim

1. Read the full scoped passage and its surrounding sentences, headings and table headers. If scope is broad, inspect the abstract's findings, Results, main figures/tables and conclusions. Use ranking to find candidates, then check the required coverage against the actual case. When the case asks for a number of claims, extract genuinely distinct findings within that scope.
2. Draft one independently decidable relation each time. Preserve the population, dataset, endpoint, comparator, direction, unit, time horizon, statistical threshold, and limits that matter to that relation. Write a self-contained sentence. Split additional outcomes and measures into separate claims.
3. Bind the shortest sufficient span to its actual numbered sentence or table row. Include a contextual span when the result relies on an earlier definition, scope qualifier, header, caption or footnote. For a conclusion inferred from several findings, include the premises and state the inference in `basis`. For comparisons check which entity each value belongs to. For cited results trace the printed marker to the bibliography; consider the current paper's attribution separately from the author's own results.
4. Preserve a negative result as `polarity: negative`. Preserve hedges and hypotheses in `modality` and in the sentence. A nonsignificant difference supports a bounded null result. Association language supports `associated_with`. For uncertain text, unreadable figures, unreported information or material conflict, record the concrete gap and location searched; a conflict needs evidence for both sides.

~~~bash
python3 -B "$SK/scripts/np_evidence.py" candidates --work "$W" --section Results --limit 18
python3 -B "$SK/scripts/np_evidence.py" read --work "$W" S1-U0012.2 S1-U0014.1
~~~

Use IDs from this run. `read` also takes `--source`, `--page`, `--section`, `--limit` and `--skip`. `candidates` accepts search terms after its options. It is a retrieval aid. Read the relevant passage even if it ranks poorly.

## Submit, revise and verify

Write a UTF-8 JSON draft under `/app/work` with the snapshot ID printed by `prepare`. For fields, relations and the status rules see [references/field-dictionary.md](references/field-dictionary.md). A minimal illustrative draft is:

~~~json
{
  "snapshot_id": "copy the current snapshot_id",
  "claims": [{
    "key": "c01", "status": "reported",
    "statement": "Model Cedar reached 92% accuracy on the held-out split.",
    "subject": "Model Cedar", "relation": "has_value", "object": "92%",
    "metric": "accuracy", "scope": ["held-out split"],
    "polarity": "positive", "modality": "asserted",
    "origin": "this_work", "evidence_type": "direct",
    "evidence": [{"anchor": "S1-U0012.2"}],
    "basis": "The sentence names the model, split, metric and measured accuracy."
  }],
  "coverage": {"complete": false, "scope": "Requested results", "gaps": []}
}
~~~

Omit `quote` to bind the exact anchor text. When a shorter span suffices, copy its exact substring including punctuation and case. Read its complete context for scope and modality. To report authors, year, version or identifiers, select a front-matter span in `sources[].fields`. Keep unreported values unknown. Resolve unfamiliar case fields with evidence-backed `contract_values` and check the requested types. The output audit preserves machine-derived provenance and status.

~~~bash
python3 -B "$SK/scripts/np_evidence.py" build --work "$W" --out "$O" --draft /app/work/claims.json
python3 -B "$SK/scripts/np_evidence.py" build --work "$W" --out "$O" --draft /app/work/claims.json --final
python3 -B "$SK/scripts/np_evidence.py" verify --out "$O"
~~~

Save an honest draft after the first useful batch. Fix rejected proposals by key and revisit high-impact warnings against the document. Additional `--draft` arguments let a later record revise the same key. Before `--final`, compare coverage against the original request and record any omitted outcomes or unreadable material. Review `contract_unfilled`, `claim_audit.json` and `review.md`. A partial build uses exit code 2 and keeps its outputs. `verify` checks the committed artifacts and exact locators. Scientific support still requires reading the evidence.

Deliver the actual `/app/output/result.json` and its referenced outputs. Report verified counts and uncertainty from this run. The TriG describes polarity, modality and scope on qualified propositions. It is an unsigned local draft. See [references/review-method.md](references/review-method.md) for reading and review decisions.
