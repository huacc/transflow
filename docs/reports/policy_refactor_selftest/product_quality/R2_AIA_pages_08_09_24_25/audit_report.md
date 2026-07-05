# Workflow Selftest Audit - R2_AIA_pages_08_09_24_25

generated_at_local: 2026-07-05 20:08:37
run_mode: product_quality
process_contract_verdict: PASS
product_quality_verdict: FAIL
terminal_state: S_FAIL_QUALITY

## Evidence

- state_trace.json
- operation_log.jsonl
- decision_log.jsonl
- source_extraction.json
- tool_probe.json
- process_validation.json
- product_quality_gates.json
- candidate.pdf

## Blocking Product Failures

- visual_similarity: {'visual_quality_adjudication': None, 'visual_adjudication': None, 'reason': 'automated structural checks are not enough for product-quality acceptance; source-vs-output PNG adjudication must be recorded', 'generation_evidence': 'docs/reports/policy_refactor_selftest/product_quality/R2_AIA_pages_08_09_24_25/candidate_generation_evidence.json'}
