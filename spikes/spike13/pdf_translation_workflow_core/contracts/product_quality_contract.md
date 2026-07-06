# Product Quality Contract

## Purpose

This contract is mandatory in `product_quality` mode.

A run cannot claim product success merely because source-language residue appears to be gone. Visual quality and layout integrity must also pass.

## Product Gates

| Gate | Tool evidence | Model evidence | Blocks success |
|---|---|---|---:|
| page_count | PyMuPDF page count | none | yes |
| page_geometry | PyMuPDF page rects | none | yes |
| text_residue | PyMuPDF text + regex | D5 | yes |
| text_fit | insertion return codes, bbox checks | D5 | yes |
| source_anchor_order | generation evidence proves a target-language region does not cross visible untranslated source separators | D5/D7 | yes |
| clipping | output PNG and bbox checks | D5 | yes |
| collision | bbox intersections and PNG review | D5/D7 | yes |
| visual_similarity | metrics + PNG review | D7 | yes |
| source_relative_visual_baseline | `collect_visual_region_metrics.py` source-extraction coverage gate | D7 | yes |
| hero_banner_text_readability | `collect_visual_region_metrics.py` role gate | D7 | yes when hero/banner title exists |
| title_readability | `collect_visual_region_metrics.py` role gate | D7 | yes when title/heading exists |
| body_paragraph_readability | `collect_visual_region_metrics.py` role gate | D7 | yes when body/body_flow exists |
| table_text_legibility | `collect_visual_region_metrics.py` role gate | D7 | yes when table cells/headers exist |
| legend_label_alignment | `collect_visual_region_metrics.py` role gate | D7 | yes when legends exist |
| image_color_integrity | `collect_visual_region_metrics.py` page gate | D7 | yes |
| line_fragmentation | target-language line length and region width usage compared with source role | D7 | yes for paragraph/footnote pages |
| paragraph_density | active paragraph text area and line density compared with source | D7 | yes for body-text pages |
| internal_paragraph_gap | gap between active paragraphs, separated from end-of-article blank space | D7 | yes for body-text pages |
| single_dense_paragraph | source paragraph gaps collapse into one uninterrupted paragraph | D7 | yes for body-text pages |
| body_flow_fallback_truncation | body_flow cannot fit and falls back to clipped point insertion | generation evidence + D7 | yes |
| target_composition_used_for_fluid_body | expanding-language body copy uses target visual composition instead of hard source-bbox fitting | generation evidence + D7 | yes for expanding-language body pages |
| failed_probe_residue | failed textbox fit attempts leave visible residue or overlapped text | PNG review + generation evidence | yes |
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
| `role_gate_status` | title/body/table/footnote/sidebar/image role gate status | fail if a critical role is `fail`; warn only for non-critical fit risks |
| `fragmentation_ratio` | output median line width / source median line width in the same region role | fail if target-language paragraphs are broken into visibly short lines without a source-layout reason |
| `region_reflow_ratio` | inserted region count / inserted unit count | warn if paragraph pages are near 1.0, because that usually means line-by-line copy layout |
| `source_line_index_gap` | line-index jump inside one inserted region for one source block | fail if gap > 1 and no explicit ignorable separator evidence exists |

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

If `docs\input\semantic_translations\<case_id>.translations.json` is missing or fails validation, the correct terminal state is `S_FAIL_CAPABILITY` before candidate generation.

Semantic preflight must fail on pseudo translations that describe the source line instead of translating it. Forbidden examples include `This line reports...`, `This line describes...`, `本行说明...`, `本行列示...`, `当前页的财务报告、治理或业务信息`, `保留数值与标记...`, and equivalent metadata/instruction leakage. These failures are `S_FAIL_CAPABILITY`, not layout repair candidates.

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
    {"dimension": "source_anchor_order", "status": "PASS|FAIL|PASS_WITH_WARN"},
    {"dimension": "region_crosses_untranslated_separator", "status": "PASS|FAIL|PASS_WITH_WARN"},
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
| `text_residue` | yes | source-language residue: ASCII tokens for zh targets, CJK characters for en targets |
| `backfill_generation` | yes if generation evidence is supplied | candidate must not be source-copy only |
| `source_anchor_order` | yes if generation evidence is supplied | blocks when one inserted target-language region crosses skipped source lines inside the same block |
| `translation_authenticity` | yes if generation evidence is supplied | deterministic placeholder providers fail |
| `semantic_coverage` | partial/yes if generation evidence is supplied | placeholder translations fail product-quality success |
| `semantic_translation_preflight` | yes | `validate_semantic_translations.py` must pass before semantic candidate generation; it blocks placeholders and line-category pseudo translations |
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
| `source_relative_visual_baseline` | yes if `visual_region_metrics` is supplied | blocks when source extraction is missing or generated regions cannot be compared to source font/line evidence |
| `hero_banner_text_readability` | yes if `visual_region_metrics` is supplied | blocks when a banner/title region falls back to tiny point text or below role font floor |
| `title_readability` | yes if `visual_region_metrics` is supplied | blocks when heading/title text is unreadable, too small, or fallback-rendered |
| `body_paragraph_readability` | yes if `visual_region_metrics` is supplied | blocks body/body_flow fallback, unreadable font floor, or severe background mismatch |
| `table_text_legibility` | yes if `visual_region_metrics` is supplied | blocks table cell/header fallback or unreadable compact text |
| `image_color_integrity` | yes if `visual_region_metrics` is supplied | blocks missing source images or excessive page color delta |
| `table_integrity` | no | requires grid/cell-specific validator |
| `chart_integrity` | no | requires chart-region-specific validator |
| `footnote_readability` | partial/no | metrics recorded; readability review still required |

Therefore an automated `PASS` from `evaluate_pdf_quality.py` is only a partial gate pass. A delivery report must add either deterministic evidence for the missing gates or a clearly labelled model/human visual adjudication record.

## Region-Reflow Product Rule

For paragraph-like text, table notes, footnotes, and multi-line headings, product quality requires region-level target-language reflow:

```text
source-language lines -> one semantic target-language region, unless the region is a compact label, legend, vertical navigation, or chart/table tick.
```

For long aligned body paragraphs on the same article column, product quality may require `body_flow`:

```text
multiple source paragraph regions -> one flowing target-language article region, with paragraph separators, only when current-run x/width statistics prove a continuous body column.
```

Within `body_flow`, adjacent source regions are joined as same-paragraph continuations when their y-gap is below `flow_grouping.body.paragraph_gap_pt`; only larger gaps use `paragraph_separator`. This prevents artificial blank lines between every source-wrapped line.

Short same-column continuation lines may join an active `body_flow` when `allow_short_continuation_lines=true`, x/y geometry matches the current flow, and width exceeds `min_continuation_width_page_ratio`. This prevents a source-wrapped final word or phrase from breaking the article into multiple regions.

On dense table/chart pages, compact labels and cell text should be `table_cell` and excluded from `body_flow`. A lower-page body copy band may re-enter `body_flow` only when `allow_dense_page_body_below_y_ratio` is set and current-run geometry proves same-column article text below the dense table/chart area.

On mixed image/text timeline or milestone pages, narrow event descriptions should be `event_card`, not `body_flow`. Product quality for event cards is local: each card must remain tied to its year/image anchor, avoid overlap with adjacent cards, and use a readable constrained-slot font or compact event variant.

When target-language expansion is enabled, `target_language_reflow` may expand only declared region kinds and must apply `overlap_guard` against the next same-column region.

When `target_composition` is enabled, `body_flow` source bboxes become anchors rather than hard containers. The output frame must be computed from the current page's body band, margins, lower safe boundary, region kind, and overlap guard. This rule is required for language directions where target prose usually expands. It is forbidden for constrained slots such as table cells, legends, chart labels, and side navigation unless a future contract defines a specific constrained-slot composition mode.

If source text already appears in the target language and would remain under a recomposed body frame, the generator must redact/redraw it as `preserve_already_target_language_span` or D7 must fail `failed_probe_residue` / `collision`. This is preservation, not semantic translation.

Generation must preflight textbox fit on a temporary page. Failed font-size attempts must not render into the real candidate PDF; otherwise `failed_probe_residue` blocks quality.

For wide `Note:` / `Notes:` blocks near tables, product quality requires `table_note` instead of `body_flow`; the note/body font hierarchy must remain close to the source.

Region-level reflow must preserve visible source anchors. If the source block contains a visible line that is not a translation unit, such as a year, numeric heading, bullet-only label, or separator label, that line is a hard boundary. Target-language text before the boundary must remain before it, and target-language text after the boundary must remain after it.

Failure signal:

```json
{
  "failure_class": "line_fragmentation",
  "symptom": "target-language paragraph inherits original source line bboxes and wraps after only a few words",
  "required_repair_atom": "region_reflow",
  "next_state": "Lx_RepairLoop"
}
```

Passing evidence must include:

```json
{
  "strategy": "redact_extractable_<source_language>_lines_and_insert_semantic_<target_language>_regions",
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
    "target_composition_used_for_fluid_body",
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
