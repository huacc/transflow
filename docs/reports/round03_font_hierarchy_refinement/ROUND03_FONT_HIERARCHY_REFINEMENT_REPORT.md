# Round03 Font Hierarchy Refinement Report

## Scope

This is a root-workspace validation run, not a `spikes\round10` package.

Inputs:

```text
01_source.pdf
测试数据\AIA_2020_Annual_Report_en_pages_08_09_24_25.pdf
spikes\round09\docs\input\semantic_translations\R1_01_source_single_timeline.translations.json
spikes\round09\docs\input\semantic_translations\R2_AIA_pages_08_09_24_25.translations.json
```

Outputs:

```text
docs\output\round03\R1_01_source_single_timeline_round03_font_hierarchy_candidate.pdf
docs\output\round03\R2_AIA_pages_08_09_24_25_round03_font_hierarchy_candidate.pdf
```

## Toolchain Changes

All reusable tooling remains under:

```text
pdf_translation_workflow_core\tools
```

Changed or added tools:

| Tool | Change |
|---|---|
| `tools\planners\build_layout_policy.py` | Added `table_note`, `body_flow`, and `vertical_nav` policy fields plus separate font profiles |
| `tools\generators\generate_semantic_backfill.py` | Consumes `table_note`, `body_flow`, and `rotated_text` policy fields |
| `tools\renderers\render_source_output_crop.py` | New generic source-vs-output crop evidence tool |
| `tools\validators\evaluate_pdf_quality.py` | Adds font hierarchy metrics and a `font_hierarchy_ratio` visual gate hook |

## Evidence

Source-vs-output crop evidence:

```text
docs\reports\round03_font_hierarchy_refinement\compare\R2_page03_note_body_font_source_vs_round03.png
docs\reports\round03_font_hierarchy_refinement\compare\R2_page04_body_source_vs_round03.png
docs\reports\round03_font_hierarchy_refinement\compare\R2_page04_sidebar_source_vs_round03.png
docs\reports\round03_font_hierarchy_refinement\compare\R1_page01_source_vs_round03.png
```

Quality gate JSON:

```text
docs\reports\round03_font_hierarchy_refinement\R1\product_quality_gates.json
docs\reports\round03_font_hierarchy_refinement\R2\product_quality_gates.json
```

## Result

| Regression | Units | Regions | Fit warnings | Product verdict | Blocking gate |
|---|---:|---:|---:|---|---|
| R1 | 103 | 82 | 0 | FAIL | `visual_similarity` |
| R2 | 209 | 117 | 0 | FAIL | `visual_similarity` |

Round03 fixed or improved:

- `table_note` is no longer merged into `body_flow`;
- note/body font hierarchy is restored enough for `PASS_WITH_WARN`;
- body-flow grouping still avoids large active paragraph gaps;
- active sidebar labels use rotated text rather than stacked Chinese characters.

Round03 does not claim final product acceptance:

- `visual_similarity` remains failed;
- R2 translated body y-coverage is still shorter than source;
- R1 headline/timeline rhythm still needs repair.
