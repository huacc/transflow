# Regression Matrix

## Purpose

Regression inputs validate generic workflow behavior. They are not templates to copy.

## Inputs

| Regression ID | Path | Coverage | Must prove |
|---|---|---|---|
| `R1_01_source_single_timeline` | `01_source.pdf` | single-page timeline with image/badge and narrow columns | process and candidate-generation mechanics remain stable |
| `R2_AIA_pages_08_09_24_25` | `测试数据\AIA_2020_Annual_Report_en_pages_08_09_24_25.pdf` | charts, pies, tables, body paragraphs, notes, side navigation | workflow handles multiple page types without hardcoding sample facts |

## Fixture Boundary

Regression fixtures may contain sample-specific replay scripts, thresholds, and historical evidence. They are not reusable workflow contracts.

Current fixture:

```text
pdf_translation_workflow_core\regression\fixtures\R1_01_source_single_timeline\replay_contract
```

Allowed use:

- reproduce the historical `01_source.pdf` output;
- protect the R1 single-page timeline regression;
- provide negative evidence if generic workflow changes degrade R1.

Forbidden use:

- copying its coordinates, strings, font sizes, thresholds, or layout choices into generic tools;
- treating its replay script as the general PDF translation engine.

## Required Regression Verdicts

For each regression input:

```json
{
  "regression_id": "R2_AIA_pages_08_09_24_25",
  "run_mode": "process_validation|backfill_candidate_validation|product_quality",
  "process_contract_verdict": "PASS|FAIL",
  "product_quality_verdict": "PASS|FAIL|NOT_ATTEMPTED",
  "anti_overfit_verdict": "PASS|FAIL",
  "evidence_artifacts": ["..."]
}
```

## Anti-Overfit Checks

The run fails anti-overfit if implementation logic branches on:

- filename contains `AIA`;
- filename contains `01_source`;
- source page number equals a known page;
- exact text string such as a known chart title;
- exact bbox coordinates from the sample;
- exact sampled colors used as constants outside a region sample record.

Allowed:

- using extracted text as evidence for the current page;
- using observed bbox as input to current layout slot;
- using sampled background color as fill provenance for the current region;
- citing sample facts in reports.
