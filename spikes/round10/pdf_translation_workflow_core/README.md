# PDF Translation Workflow Core

This directory is the executable-methodology entrypoint for PDF translation/backfill work.

It is separate from `docs\业务流程`:

- `docs\业务流程\<process_record>.md` is the audit narrative and source-of-truth process record copied into a round.
- `pdf_translation_workflow_core` is the reusable workflow core: contracts, backend model prompt packages, tool taxonomy, and regression anchors.
- Runtime reports are not stored in this core directory. They belong under `docs\reports\pdf_translation_workflow_core`.

## Directory Map

| Path | Purpose |
|---|---|
| `contracts\run_modes.md` | Defines process-validation, backfill-candidate-validation, and product-quality modes |
| `contracts\state_machine.md` | Generic state machine and mandatory exit conditions |
| `contracts\tool_contracts.md` | Tool contract matrix and state-to-tool mapping |
| `contracts\decision_contracts.md` | D1-D9 model adjudication contracts |
| `contracts\product_quality_contract.md` | Product-quality gates and repair-loop rules |
| `contracts\semantic_translation_contract.md` | Required semantic translation input schema for product-quality mode |
| `contracts\page_type_repair_matrix.md` | Page-type-specific quality failures and repair atoms |
| `contracts\change_control_contract.md` | Rules for adaptive round-local edits and required change evidence |
| `prompts\README.md` | Backend model prompt package rules |
| `prompts\prompt_manifest.json` | Prompt package manifest |
| `prompts\prompt_tool_bindings.json` | State-to-tool-to-prompt orchestration binding |
| `prompts\model_tool_orchestration_contract.md` | How tool outputs fill prompt slots and drive next states |
| `prompts\templates\*.prompt.json` | System prompts, user prompt templates, slots, schemas, and failure policies |
| `regression\regression_manifest.json` | Regression input manifest; sample-specific facts remain evidence only |
| `regression\regression_matrix.md` | How to use regression inputs without overfitting |
| `tools\README.md` | Tool taxonomy and where new tools should live |

External prompts for another Codex/human validation round are not part of the core prompt package. They live under:

```text
docs\测试提示词
```

## Non-Negotiable Rule

No sample-specific fact may become a generic behavior rule.

Do not hardcode filenames, page numbers, coordinates, colors, strings, chart labels, or known document identities into the workflow logic. Regression samples are evidence only.

## Required Verdict Split

Every run must report two verdicts:

```text
process_contract_verdict: PASS|FAIL
product_quality_verdict: PASS|FAIL|NOT_ATTEMPTED
```

In `process_validation` mode, product quality may be observation-only.

In `backfill_candidate_validation` mode, placeholder Chinese may prove redaction/backfill mechanics, but product quality must remain failed.

In `product_quality` mode, product-quality failure blocks final success and must enter a repair loop, `S_FAIL_QUALITY`, or `S_FAIL_CAPABILITY`. Product-quality mode requires a real semantic translation provider; deterministic placeholder text is forbidden.

`product_quality` mode reads semantic translation input from:

```text
docs\input\semantic_translations\<regression_id>.translations.json
```

The file must pass `tools\validators\validate_semantic_translations.py` before `tools\generators\generate_semantic_backfill.py` may create a candidate PDF. Missing or invalid semantic translations are `S_FAIL_CAPABILITY`, not a reason to fall back to placeholder generation.

## Adaptive Rounds

Some validation rounds may allow the execution Codex to modify the copied round workspace when the package is incomplete. Those edits must follow `contracts\change_control_contract.md` and must write before/after change manifests plus a human-readable change log under `docs\reports`.
