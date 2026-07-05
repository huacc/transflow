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
docs\input\semantic_translations\<regression_id>.translations.json
```

Required D2 output dimensions:

| Dimension | Meaning |
|---|---|
| `translation_provider` | real model/API/human-reviewed provider; placeholder providers forbidden |
| `translation_quality` | must be `semantic_translation` |
| `semantic_coverage` | must be `full_semantic_translation` |
| `unit_coverage` | source unit count, translated unit count, missing ids |
| `units` | one translation per extracted English unit id |
| `preserve_tokens` | numbers, years, percentages, currency, footnote markers preserved |
| `term_decisions` | terminology choices for financial/table/chart labels |
| `layout_risk` | compactness risk for later layout planning |
| `prompt_artifacts` | prompt_instance, slot_values, model_output, decision_record refs |

D2 fails if any required English unit lacks a real Chinese translation or if the output contains placeholder text such as `中文回填`, `中文标题`, or `中文标签`.

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
| `region_classification` | role thresholds and affected region kinds, including `body`, `body_flow`, `table_note`, `footnote`, `heading`, `vertical_nav`, `compact_label`, and `legend` |
| `body_flow_grouping` | whether aligned body paragraphs should be merged into a flowing article region; cite current-run x/width statistics |
| `table_note_detection` | whether wide `Note:` / `Notes:` blocks remain `table_note` and are excluded from `body_flow` |
| `font_hierarchy_ratio` | source-relative font profile choices for note/body/table/title roles |
| `sidebar_draw_mode` | whether narrow navigation uses `rotated_text` or line preservation |
| `anti_overfit_evidence` | confirm no filename, fixed page, literal text, or fixed coordinate branch was used |

## D7 Visual Gate Required Dimensions

D7 must judge these dimensions from source-vs-output renders and metrics:

| Dimension | Failure signal |
|---|---|
| `paragraph_density` | active body paragraphs leave large internal holes compared with the source |
| `internal_paragraph_gap` | gaps between active paragraphs are visually excessive |
| `end_blank_allowed` | blank space appears only after the final active paragraph and is recorded as non-blocking or warning |
| `font_hierarchy_ratio` | notes/footnotes/table notes/body/headings lose source-relative scale |
| `sidebar_orientation` | rotated source navigation becomes stacked Chinese characters |
| `visual_similarity` | overall source-vs-output layout remains visibly mismatched |

## Hidden Reasoning Boundary

Record conclusions, evidence signals, and repair choices. Do not claim to export hidden chain-of-thought or system prompts.
