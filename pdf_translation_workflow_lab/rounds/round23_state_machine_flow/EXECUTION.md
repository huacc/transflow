# Round23 Execution Document

## Scope

Round23 tests the new state-first workflow design against the isolated round22 table-layout package.

Runtime design reference:

```text
docs/设计/PDF_语义翻译回填_状态机与工具编排设计.md
```

Round23 does not modify or import `pdf_translation_workflow_core`.

## Command

Run from this directory:

```powershell
python run_round23_state_machine_flow.py
```

Optional arguments:

```powershell
python run_round23_state_machine_flow.py `
  --source-pdf input/source_pdfs/00005_2025_annual_report_zh_pages_001_020.pdf `
  --translations-json input/semantic_translations/R22_GEN_ZH_TO_EN_00005_pages_001_020.translations.json `
  --case-id R23_GEN_ZH_TO_EN_00005_pages_001_020
```

## State To Tool Mapping

| State | Purpose | Runtime action | Required output |
|---|---|---|---|
| `S0_Request` | Confirm inputs and non-goals | runner writes run request | `reports/run_request.json` |
| `S1_ContractLoad` | Load contracts, prompts, design docs, and check write boundary | inline runner checks files and path containment | `reports/contract_load_record.json`, `reports/workspace_boundary_preflight.json` |
| `S2_ToolProbe` | Probe runtime | `tools/probes/probe_runtime.py` | `reports/tool_probe.json` |
| `S3_SourceExtract` | Extract source text, bbox, font, color, and page stats | `tools/probes/extract_source_structure.py` | `reports/source_structure.json` |
| `S4_PageStrategy` | Materialize page-level strategy evidence | inline runner summarizes current-run source structure | `reports/page_strategy.json` |
| `S5_TranslationPlan` | Validate pre-supplied semantic translations | inline runner checks translation metadata, coverage, pseudo/empty units | `reports/semantic_translation_validation.json` |
| `S6_LayoutPlan` | Build role and layout plans | `tools/planners/plan_roles.py`, `tools/planners/plan_layout.py` | `reports/role_plan.json`, `reports/layout_plan.json` |
| `S7_GenerateCandidate` | Generate candidate PDF | `tools/generators/generate_candidate.py` | candidate PDF, `reports/generation_evidence.json`, `previews/*.png` |
| `S8_VerifyProductQuality` | Run quality gates and select repair family | `tools/validators/validate_quality.py`, `tools/repairs/plan_repairs.py`, inline visual adjudication summary | `reports/quality_gates.json`, `reports/repair_plan_0.json`, `reports/visual_adjudication.json` |
| `Lx_RepairLoop` | Record repair-loop boundary | inline runner writes honest non-executed loop record when no RepairPatch executor exists | `reports/repair_loop_0001.json` |
| `S9_VerifyProcessContract` | Validate process artifacts and anti-overfit process checks | `tools/validators/validate_process.py` | `reports/process_audit.json`, `reports/round23_final_verdict.json` |

## Required Evidence

The runner writes:

- `reports/run_request.json`
- `reports/contract_load_record.json`
- `reports/workspace_boundary_preflight.json`
- `reports/tool_probe.json`
- `reports/source_structure.json`
- `reports/page_strategy.json`
- `reports/semantic_translation_validation.json`
- `reports/role_plan.json`
- `reports/layout_plan.json`
- `reports/generation_evidence.json`
- `reports/quality_gates.json`
- `reports/repair_plan_0.json`
- `reports/visual_adjudication.json`
- `reports/repair_loop_0001.json` when product quality fails
- `reports/process_audit.json`
- `reports/round23_final_verdict.json`
- `reports/state_trace.json`
- `reports/decision_log.jsonl`
- `reports/operation_log.jsonl`
- `reports/model_interactions.jsonl`

## Model Boundary

No translation model or visual model is invoked by this round23 runner. It consumes pre-supplied semantic translation JSON from `input/semantic_translations` and records `model_backend=not_invoked` in `model_interactions.jsonl`.

## Expected Failure Boundary

If `quality_gates.json` reports product-quality failure, Round23 must:

1. enter `Lx_RepairLoop`;
2. write `repair_loop_0001.json`;
3. record that no generic RepairPatch executor exists in this inherited tool package;
4. terminate as `S_FAIL_QUALITY`, not as a process success or product acceptance.
