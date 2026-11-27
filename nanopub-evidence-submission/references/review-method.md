# Evidence review method

## Reading and atomicity

Read the case scope before ranking. Start at the paper's own Methods, Results, tables and Conclusions as applicable. Candidate ranking prioritizes result words and numerical cues. It can miss a key finding stated in an unusual style, a conclusion with no number, or details within a table. Read the scoped materials independently of the ranking.

Use the falsifiability test. If one half of a sentence could be false while the other remains true, make two claims. Preserve the smallest meaningful unit of scientific interpretation, including the measurement and its scope. For an effect size, give the comparator and the endpoint. For a comparison, say whether the metric is larger, smaller or better and indicate which direction is favorable. Expose a limitation as a separate claim when the paper states it and the task asks for limitations.

## Minimal sufficient evidence

Pick the shortest exact quote that carries the proposition. Keep the whole sentence available for checking. A selected fragment that cuts off “may”, a negation, or a material boundary still inherits the sentence's meaning. A row may require a header or caption. Add each needed unit as a distinct anchor and explain its role. For an indirect claim name which premises lead to it and preserve the source's epistemic strength.

Check these separately before finalizing:

- Entity and direction. Read the result aloud with its subject and object and compare that with the text or table.
- Quantity and scope. Check numbers, signs, units, operators, time windows, data splits, comparator, uncertainty and context.
- Attribution. Resolve the citation on a statement about prior work. For primary metadata look at the document's title, authors or explicit version information.
- Coverage. Match each question against the accepted records or the specific reason why it remains unresolved.

## Evidence and artifact checks

| Check | Observable result |
|---|---|
| Exact anchor | Match only within the selected sentence or row and re-slice the extracted unit at the recorded offsets |
| Value | Confirm numeric tokens and reported statistical operators in selected spans and compare recognized number–unit pairs |
| Context | Check the full anchored sentence for scoped negation, hedging and visible restrictions |
| Meaning | Flag explicit reversed comparisons, association promoted to causality, suspect attribution, and some coordinated claims |
| Revisions | Bind drafts to the prepared case, task, files and extracted text. Reject a stale proposal or a changed source |
| Export | Rebuild and compare JSONL, CSV and TriG. Parse the emitted TriG subset and check the graph edges. Cross-check named graphs with `rdflib` when available |

Exact matching and finite-language checks address common errors. Reviewers decide entailment, appropriate aggregation, context adequacy and scientific correctness. The check report distinguishes warnings and rejected claims. Bilingual lexical cues are narrow and support both languages imperfectly. Equation, image and nonstandard supplement content may need human review. Compare OCR to the page image before relying on exact numbers.

## Scientific and RDF semantics

Each accepted claim exports a local qualified `rdf:Statement`. A consumer can read `rdf:subject`, `rdf:predicate`, `rdf:object` and the condition, modality and polarity fields together. Direct RDF relationships are not asserted by reification. Content is anchored through a source file identifier and `oa:TextQuoteSelector`. Physical page and offsets describe the recovered extraction snapshot. The quotation selector, source digest and immutable unit digest make later comparisons possible.

## Reproducing the development checks

Run `python3 -B scripts/selftest.py` and `python3 -B scripts/selftest.py --demo --out demo-output`. The synthetic fixture includes corrected polarity, hedged and hypothetical relations, a comparison, a qualified table row, a bibliography-only DOI and an unreported question. The test suite alters prepared drafts and checks the resulting diagnostics and stable outputs. These are local regression measurements. The organizer supplies the common input, baseline, official model and final evaluation.
