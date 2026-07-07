# round20p356 semantic product-quality execution report

- process_contract_verdict: `FAIL`
- product_quality_verdict: `PASS`
- terminal_state: `S_FAIL_PROCESS_CONTRACT`

| case_id | target | product | blocking repairs | failed gates | candidate |
|---|---|---|---:|---|---|
| `ROUND20P356_00005_2025_annual_report_zh_pages_003_005_006` | `en` | `PASS` | 0 | - | `run_pages_03_05_06/output/ROUND20P356_00005_2025_annual_report_zh_pages_003_005_006_candidate.pdf` |

## Repair Loop Evidence

A visual repair plan is not counted as loop execution. A loop is counted only when `repair_loop_<n>.json` exists.
- `ROUND20P356_00005_2025_annual_report_zh_pages_003_005_006`: no blocking repair loop

## Notes

- Prior round quality artifacts were not used as evidence.
- Semantic translation JSON files were copied as current round inputs to isolate product-quality and loop behavior.
- If product quality fails, candidate PDFs are evidence artifacts, not accepted final translations.
