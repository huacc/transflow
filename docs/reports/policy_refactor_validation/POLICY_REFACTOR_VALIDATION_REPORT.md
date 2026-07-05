# Policy Refactor Validation Report

generated_at_local: 2026-07-05

## Scope

This report validates the refactor from hidden generator constants to explicit run-local `layout_policy.json`.

## Outputs

| Regression | Candidate PDF | Process result |
|---|---|---|
| R1 | `docs\output\R1_01_source_single_timeline_policy_refactor_candidate.pdf` | generated |
| R2 | `docs\output\R2_AIA_pages_08_09_24_25_policy_refactor_candidate.pdf` | generated |

## Evidence Summary

| Regression | Policy source | Units | Regions | Fit warnings | Product gate |
|---|---|---:|---:|---:|---|
| R1 | `auto_from_current_extraction_statistics` | 103 | 82 | 0 | FAIL: `visual_similarity` |
| R2 | `auto_from_current_extraction_statistics` | 209 | 127 | 0 | FAIL: `visual_similarity` |

## Anti-Overfit Checks

- `generate_semantic_backfill.py` now requires `--layout-policy`.
- Role thresholds, font profiles, shrink scales, fallback lengths, and text variant fields are read from `layout_policy.json`.
- Compact text is no longer invented by the generator. If needed, it must come from `semantic_translations.json.units[*].layout_variants`.
- Source text color is inherited from current extraction evidence, not hardcoded.
- Token preservation validation uses typed tokens and does not require English currency strings such as `US$4,154 million` to be copied verbatim when the Chinese translation preserves the numeric value.

## Selftest

Command:

```powershell
python pdf_translation_workflow_core\tools\run_state_machine_selftest.py `
  --out-dir docs\reports\policy_refactor_selftest `
  --modes product_quality `
  --generator semantic_backfill `
  --semantic-translations-dir spikes\round09\docs\input\semantic_translations
```

Result:

```json
{
  "overall_process_contract_verdict": "PASS",
  "R1": {"process_contract_verdict": "PASS", "product_quality_verdict": "FAIL", "terminal_state": "S_FAIL_QUALITY"},
  "R2": {"process_contract_verdict": "PASS", "product_quality_verdict": "FAIL", "terminal_state": "S_FAIL_QUALITY"}
}
```

## Honest Boundary

The refactor fixes process generality and removes hidden sample-specific layout choices from the product-quality generator. It does not claim final visual acceptance. Both candidates still fail `visual_similarity` by explicit visual adjudication.
