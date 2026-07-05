# Product Quality Contract

## Purpose

This contract is mandatory in `product_quality` mode.

A run cannot claim product success because text extraction shows zero ASCII English. Visual quality and layout integrity must also pass.

## Product Gates

| Gate | Tool evidence | Model evidence | Blocks success |
|---|---|---|---:|
| page_count | PyMuPDF page count | none | yes |
| page_geometry | PyMuPDF page rects | none | yes |
| text_residue | PyMuPDF text + regex | D5 | yes |
| text_fit | insertion return codes, bbox checks | D5 | yes |
| clipping | output PNG and bbox checks | D5 | yes |
| collision | bbox intersections and PNG review | D5/D7 | yes |
| visual_similarity | metrics + PNG review | D7 | yes |
| line_fragmentation | Chinese line length and region width usage compared with source role | D7 | yes for paragraph/footnote pages |
| paragraph_density | active paragraph text area and line density compared with source | D7 | yes for body-text pages |
| internal_paragraph_gap | gap between active paragraphs, separated from end-of-article blank space | D7 | yes for body-text pages |
| font_hierarchy_ratio | source-relative font scale between note/body/table/title roles | D7 | yes |
| sidebar_orientation | source-relative orientation for rotated navigation labels | D7 | yes when side navigation exists |
| sidebar_orientation_group_consistency | all labels in the same side-navigation group share the source writing mode | D7 | yes when side navigation exists |
| sidebar_glyph_orientation | rotated side-navigation text back-rotates into a horizontal readable label | D7 | yes when side navigation exists |
| paragraph_rhythm | paragraph y coverage, inter-paragraph gap, and text-area ratio | D7 | yes for body-text pages |
| table_integrity | line/grid/cell comparison + PNG review | D7 | yes for table pages |
| chart_integrity | chart region preservation + label readability | D7 | yes for chart pages |
| footnote_readability | font size, line count, area, PNG review | D7 | yes for footnote pages |
| translation_authenticity | translation provider and semantic evidence | D2/D9 | yes |
| semantic_coverage | translation unit coverage | D2/D9 | yes |
| semantic_translation_preflight | `validate_semantic_translations.py` output | D2 | yes |
| backfill_generation | generation evidence | D5/D9 | yes |

## Visual Metrics

At minimum:

| Metric | Meaning | Typical status rule |
|---|---|---|
| `text_area_ratio` | output text bbox area / source text bbox area | fail if too low/high for region type |
| `y_span_ratio` | output y coverage / source y coverage | fail if major collapse/expansion |
| `line_count_ratio` | output line count / source line count | warn/fail by page type |
| `median_gap_delta` | output median line gap - source median line gap | fail on severe rhythm change |
| `font_size_ratio` | output median font size / source median font size | fail if unreadable or visually mismatched |
| `font_hierarchy.small_to_body_ratio` | small-note/table-note font size divided by body font size | fail if note text becomes body-sized or unreadably small |
| `font_hierarchy.large_to_body_ratio` | heading/title font size divided by body font size | fail if title/body hierarchy collapses |
| `small_to_body_ratio_delta` | output small/body ratio minus source small/body ratio | fail/warn when role hierarchy changes visibly |
| `blank_area_delta` | output blank area - source blank area | fail if visible holes emerge |
| `background_delta` | sampled fill color difference | fail if visible redaction blocks |
| `fragmentation_ratio` | output median line width / source median line width in the same region role | fail if Chinese paragraphs are broken into visibly short lines without a source-layout reason |
| `region_reflow_ratio` | inserted region count / inserted unit count | warn if paragraph pages are near 1.0, because that usually means line-by-line copy layout |

Thresholds must be page-type and region-type specific. A table cell cannot use the same thresholds as a body paragraph.

## Product-Quality Failure Rule

In `product_quality` mode:

```text
quality_status in [fail, blocking_warn] -> Lx_RepairLoop or S_FAIL_QUALITY
```

The workflow cannot proceed to `S_DONE_PRODUCT_ACCEPTED` until every blocking quality gate passes.

`product_quality` also requires:

```json
{
  "translation_provider": "not deterministic_placeholder",
  "translation_quality": "semantic_translation",
  "semantic_coverage": "full_semantic_translation",
  "translation_validation_verdict": "PASS"
}
```

If the run uses `deterministic_placeholder`, the correct terminal state is `S_FAIL_CAPABILITY` or `S_FAIL_QUALITY`; it cannot be counted as a product-quality attempt that is merely awaiting visual repair.

If `docs\input\semantic_translations\<regression_id>.translations.json` is missing or fails validation, the correct terminal state is `S_FAIL_CAPABILITY` before candidate generation.

## Automation Coverage Boundary

Current executable validator:

```text
pdf_translation_workflow_core\tools\validators\evaluate_pdf_quality.py
```

Visual adjudication should be supplied as a separate artifact when available:

```powershell
python pdf_translation_workflow_core\tools\validators\evaluate_pdf_quality.py `
  --source <source.pdf> `
  --output <candidate.pdf> `
  --generation-evidence <candidate_generation_evidence.json> `
  --visual-adjudication <visual_adjudication.json> `
  --out <product_quality_gates.json>
```

`visual_adjudication.json` must report at least:

```json
{
  "verdict": "PASS|FAIL|PASS_WITH_WARN",
  "dimensions": [
    {"dimension": "line_fragmentation", "status": "PASS|FAIL|PASS_WITH_WARN"},
    {"dimension": "paragraph_density", "status": "PASS|FAIL|PASS_WITH_WARN"},
    {"dimension": "internal_paragraph_gap", "status": "PASS|FAIL|PASS_WITH_WARN"},
    {"dimension": "end_blank_allowed", "status": "PASS|FAIL|PASS_WITH_WARN"},
    {"dimension": "font_hierarchy_ratio", "status": "PASS|FAIL|PASS_WITH_WARN"},
    {"dimension": "sidebar_orientation", "status": "PASS|FAIL|PASS_WITH_WARN"},
    {"dimension": "sidebar_orientation_group_consistency", "status": "PASS|FAIL|PASS_WITH_WARN"},
    {"dimension": "sidebar_glyph_orientation", "status": "PASS|FAIL|PASS_WITH_WARN"},
    {"dimension": "paragraph_rhythm", "status": "PASS|FAIL|PASS_WITH_WARN"},
    {"dimension": "footnote_readability", "status": "PASS|FAIL|PASS_WITH_WARN"},
    {"dimension": "visual_similarity", "status": "PASS|FAIL|PASS_WITH_WARN"}
  ],
  "next_state": "S9_VerifyProcessContract|Lx_RepairLoop|S_FAIL_QUALITY"
}
```

Current blocking automation coverage:

| Gate | Automated as blocking | Notes |
|---|---:|---|
| `page_count` | yes | PyMuPDF page count |
| `page_geometry` | yes | page rectangle equality |
| `text_residue` | yes | extractable ASCII token residue |
| `backfill_generation` | yes if generation evidence is supplied | candidate must not be source-copy only |
| `translation_authenticity` | yes if generation evidence is supplied | deterministic placeholder providers fail |
| `semantic_coverage` | partial/yes if generation evidence is supplied | placeholder translations fail product-quality success |
| `semantic_translation_preflight` | yes | `validate_semantic_translations.py` must pass before semantic candidate generation |
| `text_fit` | partial/yes if generation evidence is supplied | `fit_warning_count > 0` blocks acceptance; bbox/PNG repair still needs richer validation |
| `line_fragmentation` | partial/no | must be reviewed from source-vs-output PNGs until promoted into a deterministic validator |
| `paragraph_rhythm` | partial/no | page metrics help, but human/model visual adjudication must record the finding |
| `paragraph_density` | partial/no | page metrics and body-flow evidence help, but source-vs-output crop adjudication must record the finding |
| `internal_paragraph_gap` | partial/no | must distinguish gaps between active paragraphs from blank space after the final paragraph |
| `font_hierarchy_ratio` | partial/yes if adjudication evidence is supplied | `evaluate_pdf_quality.py` records per-page font hierarchy metrics and blocks if D7 returns `FAIL` |
| `sidebar_orientation` | partial/no | requires source-vs-output crop adjudication for rotated side navigation |
| `sidebar_orientation_group_consistency` | partial/yes if adjudication evidence is supplied | `evaluate_pdf_quality.py` blocks if D7 returns `FAIL` |
| `sidebar_glyph_orientation` | partial/yes if adjudication evidence is supplied | use source-vs-output crop plus back-rotated output crop; blocks if D7 returns `FAIL` |
| `clipping` | no | requires bbox-plus-PNG validator |
| `collision` | no | requires region intersection validator |
| `visual_similarity` | partial/yes if adjudication evidence is supplied | blocks success unless source-vs-output PNG adjudication is recorded as PASS |
| `table_integrity` | no | requires grid/cell-specific validator |
| `chart_integrity` | no | requires chart-region-specific validator |
| `footnote_readability` | partial/no | metrics recorded; readability review still required |

Therefore an automated `PASS` from `evaluate_pdf_quality.py` is only a partial gate pass. A delivery report must add either deterministic evidence for the missing gates or a clearly labelled model/human visual adjudication record.

## Region-Reflow Product Rule

For paragraph-like text, table notes, footnotes, and multi-line headings, product quality requires region-level Chinese reflow:

```text
source English lines -> one semantic Chinese region, unless the region is a compact label, legend, vertical navigation, or chart/table tick.
```

For long aligned body paragraphs on the same article column, product quality may require `body_flow`:

```text
multiple source paragraph regions -> one flowing Chinese article region, with paragraph separators, only when current-run x/width statistics prove a continuous body column.
```

For wide `Note:` / `Notes:` blocks near tables, product quality requires `table_note` instead of `body_flow`; the note/body font hierarchy must remain close to the source.

Failure signal:

```json
{
  "failure_class": "line_fragmentation",
  "symptom": "Chinese paragraph inherits original English line bboxes and wraps after only a few Chinese words",
  "required_repair_atom": "region_reflow",
  "next_state": "Lx_RepairLoop"
}
```

Passing evidence must include:

```json
{
  "strategy": "redact_extractable_ascii_lines_and_insert_semantic_chinese_regions",
  "layout_policy_json": "docs/reports/.../layout_policy.json",
  "layout_policy_sha256": "...",
  "redacted_line_count": 209,
  "inserted_unit_count": 209,
  "inserted_region_count": 127,
  "fit_warning_count": 0,
  "visual_adjudication_dimensions": [
    "line_fragmentation",
    "paragraph_rhythm",
    "paragraph_density",
    "internal_paragraph_gap",
    "font_hierarchy_ratio",
    "sidebar_orientation",
    "footnote_readability",
    "text_area_ratio",
    "blank_area_delta"
  ]
}
```

## Required Quality JSON

```json
{
  "gate_id": "visual_similarity",
  "scope": "page_24.table.header",
  "region_type": "table_header",
  "source_value": 123.4,
  "output_value": 80.0,
  "ratio_or_delta": 0.648,
  "threshold": ">=0.80 for table_header",
  "status": "fail",
  "blocking": true,
  "evidence_artifacts": ["source.png", "output.png"],
  "repair_atom_candidates": ["cell_reflow", "font_size_adjust", "background_fill_resample"],
  "next_state": "Lx_RepairLoop"
}
```
