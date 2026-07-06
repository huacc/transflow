# Model Decision Contracts

## Required Record Schema

Every model adjudication must be recorded:

```json
{
  "decision_id": "D7_similarity_gate",
  "state": "S8_VerifyProductQuality",
  "loop_id": null,
  "purpose": "judge source-vs-output visual similarity gates",
  "input_artifacts": [{"path": "...", "kind": "..."}],
  "prompt_contract": "...",
  "required_output_dimensions": ["..."],
  "model_output": {
    "verdict": "pass|fail|warn|skipped",
    "summary": "...",
    "blocking": true
  },
  "tool_outputs": [{"path": "...", "kind": "..."}],
  "next_state": "L2_RepairLoop"
}
```

## Required Decisions

| Decision | Purpose | Blocks product-quality success? |
|---|---|---:|
| D1_role_classification | classify extracted text/regions into roles | yes |
| D2_translation | define translation and terminology policy | yes |
| D3_visual_only_text | handle visible but non-extractable text | yes |
| D4_layout_plan | map translations into layout slots and render patches | yes |
| D5_initial_verification | check residue, overflow, clipping, punctuation, collision | yes |
| D6_user_feedback_adjudication | incorporate user visual feedback or known negative examples | yes when relevant |
| D7_similarity_gate | judge visual similarity metrics and source-vs-output PNGs | yes |
| D8_minimal_repair_selection | choose one repair atom for current failure | yes when a failure exists |
| D9_final_acceptance | split process and product verdicts | yes |

## D2 Translation Decision Contract

In `product_quality`, D2 must produce or reference:

```text
docs\input\semantic_translations\<case_id>.translations.json
```

Required D2 output dimensions:

| Dimension | Meaning |
|---|---|
| `translation_provider` | real model/API/human-reviewed provider; placeholder providers forbidden |
| `translation_quality` | must be `semantic_translation` |
| `semantic_coverage` | must be `full_semantic_translation` |
| `unit_coverage` | source unit count, translated unit count, missing ids |
| `units` | one translation per extracted source-language unit id |
| `preserve_tokens` | numbers, years, percentages, currency, footnote markers preserved |
| `term_decisions` | terminology choices for financial/table/chart labels |
| `layout_risk` | compactness risk for later layout planning |
| `layout_variants` | optional compact target-language variants for constrained labels, table cells, legends, and side navigation |
| `forbidden_pattern_check` | evidence that target text and variants do not contain placeholder or line-category pseudo translation patterns |
| `prompt_artifacts` | prompt_instance, slot_values, model_output, decision_record refs |

D2 fails if any required source-language unit lacks a real target-language translation or if the output contains placeholder or metadata-style pseudo translation text such as `中文回填`, `中文标题`, `中文标签`, `待翻译`, `占位`, `placeholder`, `TBD`, `This line reports...`, `This line describes...`, `本行说明...`, `本行列示...`, `当前页的财务报告、治理或业务信息`, or `保留数值与标记...`.

## Prompt Requirements

Model prompts must include:

- task mode: `process_validation` or `product_quality`;
- input artifact list;
- allowed and forbidden assumptions;
- output dimensions;
- pass/fail/warn criteria;
- next-state rule;
- anti-overfit warning.

## D4 Layout Decision Required Dimensions

D4 must explicitly decide these layout dimensions when the corresponding source evidence exists:

| Dimension | Required output |
|---|---|
| `language_pair_profile` | selected generic profile path, profile sha256, source_language, target_language, target_text_field, and layout_strategy |
| `region_classification` | role thresholds and affected region kinds, including `body`, `body_flow`, `table_cell`, `table_note`, `footnote`, `heading`, `vertical_nav`, `compact_label`, and `legend` |
| `body_flow_grouping` | whether aligned body paragraphs should be merged into a flowing article region; cite current-run x/width/y-gap statistics |
| `body_flow_line_joining` | `paragraph_gap_pt`, target-language line joiners, and paragraph separator evidence |
| `short_continuation_policy` | whether narrow same-column continuation lines may join an active `body_flow`; include `allow_short_continuation_lines` and `min_continuation_width_page_ratio` |
| `dense_page_body_band_policy` | when dense table/chart page types may still allow lower-page article text into `body_flow`; include `allow_dense_page_body_below_y_ratio` evidence |
| `target_language_reflow` | region kinds eligible for target-language frame expansion plus `overlap_guard` settings |
| `table_note_detection` | whether wide `Note:` / `Notes:` blocks remain `table_note` and are excluded from `body_flow` |
| `table_cell_detection` | whether dense table/chart labels are preserved as `table_cell` and excluded from `body_flow` |
| `font_hierarchy_ratio` | source-relative font profile choices for note/body/table_cell/table_note/title roles |
| `textbox_probe_isolation` | confirm failed textbox fit attempts are probed off-page and cannot render residue into the candidate |
| `sidebar_draw_mode` | whether narrow navigation uses `rotated_horizontal_text_image` or line preservation |
| `anti_overfit_evidence` | confirm no filename, fixed page, literal text, or fixed coordinate branch was used |

## D7 Visual Gate Required Dimensions

D7 must judge these dimensions from source-vs-output renders and metrics:

| Dimension | Failure signal |
|---|---|
| `paragraph_density` | active body paragraphs leave large internal holes compared with the source |
| `internal_paragraph_gap` | gaps between active paragraphs are visually excessive |
| `single_dense_paragraph` | source paragraph gaps collapse into one continuous paragraph |
| `body_flow_fallback_truncation` | long body text falls back to a clipped point insertion instead of fitting as flowing text |
| `failed_probe_residue` | failed font-size attempts leave visible overlapped text in the output |
| `end_blank_allowed` | blank space appears only after the final active paragraph and is recorded as non-blocking or warning |
| `font_hierarchy_ratio` | notes/footnotes/table notes/body/headings lose source-relative scale |
| `sidebar_orientation` | rotated source navigation becomes stacked Chinese characters |
| `sidebar_orientation_group_consistency` | labels in one side-navigation group do not share one writing mode |
| `sidebar_glyph_orientation` | back-rotated output crop does not become readable horizontal target-language text |
| `visual_similarity` | overall source-vs-output layout remains visibly mismatched |

## Hidden Reasoning Boundary

Record conclusions, evidence signals, and repair choices. Do not claim to export hidden chain-of-thought or system prompts.
