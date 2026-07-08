# Round22 Table Layout Promotion Notes

This note tracks which ideas from `pdf_translation_workflow_lab/rounds/round22_table_layout` may later enter `pdf_translation_workflow_core`.

Round22 is not a product-quality accepted baseline. It is an experiment snapshot used to identify missing core capabilities.

## Candidate Capabilities

- Table cell splitting from local geometry.
- Header and label binding from neighboring text, style, and bbox evidence.
- Obstacle-aware text-region expansion when target-language text grows.
- Source-relative font floors and hierarchy checks.

## Required Core Rewrite

Before promotion, each capability must be represented in stable core as:

- a tool or reusable module,
- an input/output contract,
- a prompt or non-model decision boundary when model judgment is required,
- a quality gate,
- a repair action,
- a process-document state transition.

## Validation Required

- Regression: `pdf_translation_workflow_regression/cases/hsbc_00005_zh_to_en_pages_001_020`.
- Independent spike package with no framework edits.
- Anti-overfit scan against sample-specific logic.

## Source Of Truth

The human-readable process source remains:

- `docs/业务流程/PDF_语义翻译回填_标准流程设计.md`

The core-local copy is:

- `pdf_translation_workflow_core/docs/process/PDF_语义翻译回填_标准流程设计.md`
