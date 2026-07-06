# S20 semantic product-quality execution report

- process_contract_verdict: `PASS`
- product_quality_verdict: `FAIL`
- terminal_state: `S_FAIL_QUALITY`

| case_id | target | product | blocking repairs | failed gates | candidate |
|---|---|---|---:|---|---|
| `S20_00005_2025_interim_report_zh` | `en` | `FAIL` | 4 | text_residue, visual_similarity, sidebar_navigation_legibility, table_text_legibility, background_residue_artifact, matrix_diagram_integrity | `docs/output/S20_00005_2025_interim_report_zh_candidate.pdf` |
| `S20_00388_2026_annual_report_en` | `zh` | `PASS` | 0 | - | `docs/output/S20_00388_2026_annual_report_en_candidate.pdf` |
| `S20_00992_2023_annual_report_en` | `zh` | `PASS` | 0 | - | `docs/output/S20_00992_2023_annual_report_en_candidate.pdf` |
| `S20_建業新生活有限公司_c` | `en` | `PASS` | 0 | - | `docs/output/S20_建業新生活有限公司_c_candidate.pdf` |
| `S20_建業新生活有限公司_e` | `zh` | `PASS` | 0 | - | `docs/output/S20_建業新生活有限公司_e_candidate.pdf` |

## Repair Loop Evidence

A visual repair plan is not counted as loop execution. A loop is counted only when `repair_loop_<n>.json` exists.
- `S20_00005_2025_interim_report_zh`: ['docs/reports/S20_00005_2025_interim_report_zh/repair_loop_0001.json']
- `S20_00388_2026_annual_report_en`: no blocking repair loop
- `S20_00992_2023_annual_report_en`: no blocking repair loop
- `S20_建業新生活有限公司_c`: no blocking repair loop
- `S20_建業新生活有限公司_e`: no blocking repair loop

## Notes

- Prior round quality artifacts were not used as evidence.
- Semantic translation JSON files were copied as current round inputs to isolate product-quality and loop behavior.
- If product quality fails, candidate PDFs are evidence artifacts, not accepted final translations.
