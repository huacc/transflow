# round18 semantic product-quality execution report

- process_contract_verdict: `PASS`
- product_quality_verdict: `FAIL`
- terminal_state: `S_FAIL_QUALITY`

| case_id | target | product | blocking repairs | failed gates | candidate |
|---|---|---|---:|---|---|
| `R18_00005_2025_annual_report_zh_pages_001_050` | `en` | `FAIL` | 4 | visual_similarity, short_label_legibility, table_text_legibility, background_residue_artifact, matrix_diagram_integrity | `docs/output/round18/R18_00005_2025_annual_report_zh_pages_001_050_repair02_candidate.pdf` |

## Repair Loop Evidence

A visual repair plan is not counted as loop execution. A loop is counted only when `repair_loop_<n>.json` exists.
- `R18_00005_2025_annual_report_zh_pages_001_050`: ['docs/reports/round18/R18_00005_2025_annual_report_zh_pages_001_050/repair_loop_0001.json', 'docs/reports/round18/R18_00005_2025_annual_report_zh_pages_001_050/repair_loop_0002.json']

## Notes

- Prior round quality artifacts were not used as evidence.
- Semantic translation JSON files were copied as current round inputs to isolate product-quality and loop behavior.
- If product quality fails, candidate PDFs are evidence artifacts, not accepted final translations.
