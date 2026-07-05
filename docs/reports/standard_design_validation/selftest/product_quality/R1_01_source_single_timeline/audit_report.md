# Workflow Selftest Audit - R1_01_source_single_timeline

generated_at_local: 2026-07-05 21:44:27
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

- visual_similarity: {'visual_quality_adjudication': None, 'visual_adjudication': None, 'reason': 'automated structural checks are not enough for product-quality acceptance; source-vs-output PNG adjudication must be recorded', 'generation_evidence': 'docs/reports/standard_design_validation/selftest/product_quality/R1_01_source_single_timeline/candidate_generation_evidence.json'}
