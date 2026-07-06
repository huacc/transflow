# Run Modes Contract

## Purpose

The workflow supports three modes. They must never be conflated.

## Modes

| Mode | Goal | PDF quality required for success | Required final verdict |
|---|---|---:|---|
| `process_validation` | Verify that state machine, tool contracts, and model decision records are complete | No | `process_contract_verdict` |
| `backfill_candidate_validation` | Verify that the generator can redact extractable English and insert Chinese placeholder text with evidence | No | `process_contract_verdict`, `generation_verdict`, and an expected product-quality failure |
| `product_quality` | Produce a semantically translated/backfilled PDF that visually follows the source | Yes | both `process_contract_verdict` and `product_quality_verdict` |

## Mode Selection Contract

Every run must declare:

```json
{
  "run_mode": "process_validation|backfill_candidate_validation|product_quality",
  "target_inputs": ["..."],
  "success_criteria": ["..."],
  "non_goals": ["..."],
  "adaptive_changes_allowed": true
}
```

`adaptive_changes_allowed` is not a separate run mode. It is an execution policy. If enabled, all edits must follow `change_control_contract.md`.

## Exit Rules

### process_validation

Allowed final states:

- `S_DONE_PROCESS_VALIDATED`
- `S_FAIL_PROCESS_CONTRACT`

Product-quality failures are recorded as observations with logical next states. They do not block success.

### backfill_candidate_validation

Allowed final states:

- `S_DONE_PROCESS_VALIDATED`
- `S_FAIL_PROCESS_CONTRACT`
- `S_FAIL_TOOLING`
- `S_FAIL_QUALITY`

This mode may use deterministic placeholder Chinese. It must not claim product-quality success. A semantic failure such as `placeholder_not_semantic` is expected evidence that the candidate is not a product deliverable.

### product_quality

Allowed final states:

- `S_DONE_PRODUCT_ACCEPTED`
- `S_FAIL_PROCESS_CONTRACT`
- `S_FAIL_QUALITY`
- `S_FAIL_TOOLING`
- `S_FAIL_CAPABILITY`

Any blocking quality failure must enter a repair loop. A run cannot claim product success with unresolved quality failures.

`product_quality` must not use `backfill_placeholder` or `smoke_copy`. It requires:

- `docs\input\semantic_translations\<case_id>.translations.json`;
- `translation_validation_verdict: PASS` from `validate_semantic_translations.py`;
- `layout_policy.json` from `build_layout_policy.py` and/or D4 model revision;
- `generate_semantic_backfill.py` as the candidate generator;
- `translation_provider` that is not placeholder;
- `semantic_coverage: full_semantic_translation`.

If semantic translations are missing or invalid, the correct result is `S_FAIL_CAPABILITY`, not a placeholder PDF.

## Required Separation

Do not write:

```text
final_verdict: PASS
```

Write:

```text
process_contract_verdict: PASS
product_quality_verdict: FAIL
```

or:

```text
process_contract_verdict: PASS
product_quality_verdict: NOT_ATTEMPTED
```

For `backfill_candidate_validation`, write:

```text
process_contract_verdict: PASS
generation_verdict: PASS
product_quality_verdict: FAIL
expected_quality_failure_observed: PASS
```
