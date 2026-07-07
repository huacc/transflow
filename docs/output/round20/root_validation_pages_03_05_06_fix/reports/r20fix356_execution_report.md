# r20fix356 semantic product-quality execution report

- process_contract_verdict: `FAIL`
- product_quality_verdict: `FAIL`
- terminal_state: `S_FAIL_PROCESS_CONTRACT`

| case_id | target | product | blocking repairs | failed gates | candidate |
|---|---|---|---:|---|---|
| `R20FIX356_00005_2025_annual_report_zh_pages_003_005_006` | `en` | `FAIL` | 0 | visual_similarity, insertion_collision | `docs/output/round20/root_validation_pages_03_05_06_fix/output/R20FIX356_00005_2025_annual_report_zh_pages_003_005_006_candidate.pdf` |

## Repair Loop Evidence

A visual repair plan is not counted as loop execution. A loop is counted only when `repair_loop_<n>.json` exists.
- `R20FIX356_00005_2025_annual_report_zh_pages_003_005_006`: no blocking repair loop

## Notes

- Prior round quality artifacts were not used as evidence.
- Semantic translation JSON files were copied as current round inputs to isolate product-quality and loop behavior.
- If product quality fails, candidate PDFs are evidence artifacts, not accepted final translations.
