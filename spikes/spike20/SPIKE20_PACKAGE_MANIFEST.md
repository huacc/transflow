# SPIKE20 Package Manifest

## Scope

Workspace boundary:

```text
spikes\spike20
```

This package replaces the mistaken `spike19` AIA random-page inputs with the intended `round13` source PDF input set.

## Included Runtime Inputs

Copied from:

```text
docs\input\round13\source_pdfs
```

Included as top-level PDF-only inputs:

```text
input\00005_2025_interim_report_zh.pdf
input\00388_2026_annual_report_en.pdf
input\00992_2023_annual_report_en.pdf
input\建業新生活有限公司_c.pdf
input\建業新生活有限公司_e.pdf
```

No `round13` semantic translation JSON, semantic translation pool, historical candidate PDF, or historical report is included. The executing Codex must materialize D2 semantic translations inside this spike.

## Included Framework

```text
pdf_translation_workflow_core\
docs\业务流程\PDF_语义翻译回填_标准流程设计.md
docs\测试提示词\SPIKE20_ROUND13_SOURCE_PUNCTURE_PROMPT.md
run_request.json
SPIKE20_PACKAGE_MANIFEST.md
```

The copied framework represents the current parent project design at package-build time. It is frozen by default during execution.

## Writable Runtime Evidence

```text
docs\input\source_pdfs\
docs\input\semantic_translation_pool\
docs\input\semantic_translations\
docs\reports\
docs\output\
```

Every runtime write set must first have a passing `validate_workspace_boundary.py` report.

## Required Final Evidence

```text
docs\reports\spike20_execution_audit.md
docs\reports\spike20_final_verdict.json
```

Candidate PDFs are evidence artifacts. They are accepted products only when `product_quality_verdict=PASS`.
