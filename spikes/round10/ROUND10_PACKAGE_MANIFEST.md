# ROUND10 Package Manifest

## Purpose

This package validates whether the workflow design, state machine, tool contracts, prompt bindings, repair-loop logic, and anti-overfit controls can guide a fresh Codex session to execute the PDF translation/backfill workflow.

The goal is process and methodology validation. Product-quality success is allowed but not assumed.

## Workspace Root

```text
spikes\round10
```

All commands must run from this directory.

## Included Inputs

```text
01_source.pdf
测试数据\AIA_2020_Annual_Report_en_pages_08_09_24_25.pdf
docs\input\semantic_translations\R1_01_source_single_timeline.translations.json
docs\input\semantic_translations\R2_AIA_pages_08_09_24_25.translations.json
```

The semantic translation files are input seeds copied from round09 so round10 can focus on state-machine/tool/prompt validation rather than redoing translation preparation from scratch.

## Included Methodology

```text
docs\业务流程\PDF_中文回填_标准流程设计.md
pdf_translation_workflow_core\
```

`pdf_translation_workflow_core` is copied from the current root workflow core, not from the older round09 core.

## Required Execution Prompt

```text
docs\测试提示词\ROUND10_ENGINE_WORKFLOW_VALIDATION_PROMPT.md
```

## Output Locations

```text
docs\output\
docs\reports\
```

Round10 must create its own candidates, reports, state trace, operation log, quality gates, and final audit report.

## Independence Boundary

Round10 may read this package only. It must not use prior round output PDFs, prior round quality verdicts, or prior round reports as evidence for product acceptance.

If the fresh Codex makes small execution-continuity changes, every change must be recorded in the final report and in an adaptive change record.
