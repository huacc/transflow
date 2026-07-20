# Round28 Column-Flow Elastic Layout

Round28 is a lab experiment for page-type-driven PDF translation backfill. It is not
merged into `pdf_translation_workflow_core`.

## Goal

Validate whether most layout failures can be reduced to engineering rules:

1. classify each page into a source-derived page/layout type;
2. keep text-column width stable;
3. allow normal text to grow or shrink vertically;
4. preserve tables, charts, visual pages, images, and background;
5. leave only residual aesthetic judgement to the LLM/human adjudication layer.

## Run

```powershell
python -B run_round28_batch.py
```

## Inputs

The batch uses the AIA 20-page Chinese and English source PDFs:

- `input/source_pdfs/AIA_2020_Annual_Report_zh_pages_001_020.pdf`
- `input/source_pdfs/AIA_2020_Annual_Report_en_pages_001_020.pdf`

## Outputs

- Batch summary: `reports/round28_batch_summary.md`
- Audit report: `reports/round28_execution_audit.md`
- Per-case reports: `case_runs/<case_id>/reports/`
- Candidate PDFs: `case_runs/<case_id>/output/*_candidate.pdf`

## Key Evidence

- `reports/page_profiles.json`: page role, layout flow, density, columns, and normal-flow eligibility.
- `reports/layout_plan.raw.json`: base layout plan before round28 postprocess.
- `reports/column_flow_elastic_evidence.json`: pages/groups changed by column-flow elastic layout.
- `reports/layout_plan.json`: final layout consumed by the PDF generator.

## Product Verdict

Process success and PDF quality stay separate:

- `process_contract_verdict = PASS` means round28 followed the required artifacts and state flow.
- `product_quality_verdict = PASS` means the produced PDF passed product gates.

Round28 is allowed to end with product quality FAIL. The point of the round is to expose whether
the page-type plus column-flow approach reduces failure classes without overfitting.
