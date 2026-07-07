# R15 semantic product-quality execution report

- process_contract_verdict: `PASS`
- product_quality_verdict: `FAIL`
- terminal_state: `S_FAIL_QUALITY`

| case_id | target | product | blocking repairs | failed gates | candidate |
|---|---|---|---:|---|---|
| `R15_00005_2025_annual_report_zh` | `en` | `FAIL` | 9 | text_residue, text_fit, source_anchor_order, visual_similarity, body_paragraph_readability, event_card_readability, footnote_readability, short_label_legibility, sidebar_navigation_legibility, table_text_legibility, title_readability, background_residue_artifact, matrix_diagram_integrity | `docs/output/round15/R15_00005_2025_annual_report_zh_candidate.pdf` |

## Repair Loop Evidence

A visual repair plan is not counted as loop execution. A loop is counted only when `repair_loop_<n>.json` exists.
- `R15_00005_2025_annual_report_zh`: ['docs/reports/round15/R15_00005_2025_annual_report_zh/repair_loop_0001.json']

## Notes

- Prior round quality artifacts were not used as evidence.
- Semantic translation JSON files were copied as current round inputs to isolate product-quality and loop behavior.
- If product quality fails, candidate PDFs are evidence artifacts, not accepted final translations.
