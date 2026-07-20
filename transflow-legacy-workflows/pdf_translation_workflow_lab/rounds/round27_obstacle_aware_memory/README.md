# Round27 Obstacle-Aware Memory

Round27 is a lab round for layered PDF translation/backfill repair. It is not merged into `pdf_translation_workflow_core`.

## Goal

Validate this loop:

1. Generate an initial translated candidate.
2. Collect source-vs-candidate quality signals.
3. Triage one primary blocking failure.
4. Apply one bounded RepairPatch.
5. Rejudge the repaired candidate.
6. If a repair fixes the target but creates a hard regression, record it in `repair_memory_ledger.json` and promote the regressed domain to loop 2.

## Run

```powershell
python -B run_round27_batch.py
```

## Outputs

- Batch summary: `reports/round27_batch_summary.md`
- Audit report: `reports/round27_execution_audit.md`
- Per-case reports: `case_runs/<case_id>/reports/`
- Candidate PDFs: `case_runs/<case_id>/output/*_candidate.pdf`

## Product Verdict

This round separates process success from product quality:

- `process_contract_verdict = PASS` means the layered process, evidence files, memory ledger, and validators executed.
- `product_quality_verdict = PASS` means the PDF itself passed product gates.

Round27 currently reaches process PASS and decision-graph PASS, but product quality remains FAIL because residual layout failures remain after loop 2.
