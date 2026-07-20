# Round22 Page 16 Table Fix Record

This file records the page-16 fix for `R22_GEN_ZH_TO_EN_00005_pages_001_020_candidate.pdf`.

## Failure

- The source page contains wide financial tables.
- The previous role planner merged a table title and many table cells into one long text group.
- The generator rendered that group as one red paragraph, causing a red text band to cover body text.
- The quality gate surfaced the symptom as `local_text_overlap` and `all_groups_fit`, but the root cause was `table_block_merged_into_paragraph`.

## Added Generic Rules

| Rule | Tool | Source-Derived Inputs | Output |
|---|---|---|---|
| `table_cell_split` | `tools/generate_round22_layout_candidate.py`, `tools/planners/plan_roles.py` | block line count, block width, numeric-token ratio, short-line ratio, x-column count | split dense wide table-like blocks into per-line `table_cell` groups |
| `table_neighbor_header_binding` | `tools/generate_round22_layout_candidate.py`, `tools/planners/plan_roles.py` | small-font block y distance to table top, horizontal overlap with table, page font quantiles | bind adjacent table headers into `table_cell` treatment |
| `table_region_obstacle_pack` | `tools/planners/plan_layout.py` | table bands derived from `table_cell` source rectangles, same-column body source/target rects, available pre-table height | pack translated body flow above later table regions |
| `table_cell_font_floor` | `tools/validators/validate_quality.py` | `table_cell` source font size | use a table-specific source-relative font floor |

## Anti-Overfit Boundary

Runtime tools must not branch on:

- page number;
- file name;
- exact sample text such as company name or section title;
- exact numeric values;
- offline reference PDF coordinates.

The implemented rules use only current source-page geometry, current source-page text statistics, current source colors/fonts, generic numeric/unit token patterns, and translated text length.

## Latest Verification

- Command: `python run_round22_workflow.py --source-pdf input/source_pdfs/00005_2025_annual_report_zh_pages_001_020.pdf --translations-json input/semantic_translations/R22_GEN_ZH_TO_EN_00005_pages_001_020.translations.json --case-id R22_GEN_ZH_TO_EN_00005_pages_001_020`
- Candidate: `output/R22_GEN_ZH_TO_EN_00005_pages_001_020_candidate.pdf`
- Preview: `previews/candidate_page_016.png`
- Process audit: `process_contract_verdict=PASS`
- Product gate: `product_quality_verdict=FAIL`
- Latest blocking failures: `80`
- Split: `all_groups_fit=9`, `source_relative_font_floor=1`, `local_text_overlap=70`
- Page 16 remaining failures: one red-heading overflow and one small middle-column body overlap.

## Merge Guidance

Do not merge the candidate PDF as a product-quality pass. If these rules are promoted into `pdf_translation_workflow_core`, promote them as independent role/layout/gate capabilities:

- `table_cell_split`;
- `table_neighbor_header_binding`;
- `table_region_obstacle_pack`;
- `table_cell_font_floor`.

They should be wired into the repair loop as executable actions, not only as repair-family labels.
