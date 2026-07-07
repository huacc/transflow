# Round22 Isolated Workflow Package

This directory is an isolated experiment package. It is not part of `pdf_translation_workflow_core`.

## Objective

Translate and re-layout the selected source PDF pages while preserving the visual structure of the source page. This round focuses on validating the workflow chain for pages 3, 5, and 6 of the HSBC sample.

## Boundary

- Runtime inputs are only under `input/`.
- Generated artifacts are only under `reports/`, `output/`, and `previews/`.
- Offline references under `offline_reference_compare/` are for human review only and must not be consumed by runtime tools.
- No tool in this package may import from `pdf_translation_workflow_core`.

## Directory Map

- `EXECUTION.md`: runnable procedure, state-to-tool mapping, required evidence, and current limitations.
- `docs/业务流程/PDF_语义翻译回填_Round22_设计增量与合入指南.md`: design delta, state/tool mapping, adjudication matrix, and merge guide for promoting round22 capabilities into the main workflow.
- `contracts/`: state machine, artifact schemas, tool contracts, gate-to-repair mapping.
- `prompts/templates/`: prompt templates for model adjudication and repair selection.
- `tools/probes/`: source extraction and visual sampling.
- `tools/planners/`: role classification and layout planning.
- `tools/generators/`: PDF candidate generation.
- `tools/validators/`: quality gates and anti-overfit checks.
- `tools/repairs/`: repair plan materialization.
- `reports/`: all stage outputs, traces, decisions, gates, and experiment reports.
- `output/`: candidate PDF files.
- `previews/`: rendered PNG previews.

## Current Status

The package is still experimental. The process audit is expected to pass on a clean run, but product quality may still fail because repair selection is recorded and not yet auto-applied. No logic should be merged back to the core framework until product quality also passes.
