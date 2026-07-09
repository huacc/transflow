# Round24 Execution Document

## Scope

Round24 verifies whether the new layered state-machine design can drive a real repair loop.

Runtime design reference:

```text
docs/设计/PDF_语义翻译回填_状态机与工具编排设计.md
```

Round24 does not modify or import `pdf_translation_workflow_core`.

## Command

Run from this directory:

```powershell
python run_round24_state_machine_repair_patch.py
```

Optional arguments:

```powershell
python run_round24_state_machine_repair_patch.py `
  --source-pdf input/source_pdfs/00005_2025_annual_report_zh_pages_001_020.pdf `
  --translations-json input/semantic_translations/R22_GEN_ZH_TO_EN_00005_pages_001_020.translations.json `
  --case-id R24_GEN_ZH_TO_EN_00005_pages_001_020
```

## State To Tool Mapping

| State | Purpose | Tool or prompt | Required output |
|---|---|---|---|
| `S0_Request` | Confirm inputs and non-goals | runner | `reports/run_request.json` |
| `S1_ContractLoad` | Load contracts, design, prompt templates, and write-boundary evidence | runner | `reports/contract_load_record.json`, `reports/workspace_boundary_preflight.json` |
| `S2_ToolProbe` | Probe runtime | `tools/probes/probe_runtime.py` | `reports/tool_probe.json` |
| `S3_SourceExtract` | Extract source text, bbox, font, color, and page stats | `tools/probes/extract_source_structure.py` | `reports/source_structure.json` |
| `S4_PageStrategy` | Materialize page strategy from current source evidence | runner | `reports/page_strategy.json` |
| `S5_TranslationPlan` | Validate pre-supplied semantic translation | runner | `reports/semantic_translation_validation.json` |
| `S6_LayoutPlan` | Build role and layout plans | `tools/planners/plan_roles.py`, `tools/planners/plan_layout.py` | `reports/role_plan.json`, `reports/layout_plan.json` |
| `S7_GenerateCandidate` | Generate initial candidate | `tools/generators/generate_candidate.py` | initial PDF, `reports/generation_evidence.json` |
| `S8A_NormalizeQualitySignals` | Compare candidate to source and normalize quality signals | `S8A_quality_signal_normalization.prompt.json`, `tools/judges/compare_source_candidate.py` | `reports/quality_signals.json` |
| `S8B_TriageQualitySignals` | Decide product verdict, failure classes, and evidence sufficiency. It must not choose tools. | `S8B_quality_triage.prompt.json`, `tools/judges/compare_source_candidate.py` | `reports/visual_adjudication.json` |
| `S8C_DispatchAndBindRepairPatch` | Resolve failure class through `contracts/failure_dispatch_table.json`, then bind executable RepairPatch parameters. | `S8C_repair_patch_binding.prompt.json`, `tools/repairs/build_repair_patch.py` | `reports/repair_patch_0001.json` |
| `Lx_RepairLoop` | Apply patch and record loop evidence. The repaired candidate is accepted only if remeasurement improves; otherwise it is rejected and the accepted candidate rolls back. | `Lx_repair_loop_execution.prompt.json`, `tools/repairs/apply_repair_patch.py` | `reports/layout_plan.repair0001.json`, `reports/repair_patch_application_0001.json` |
| `S7_GenerateCandidate` | Regenerate repaired candidate | `tools/generators/generate_candidate.py` | repaired PDF, `reports/generation_evidence.repair0001.json` |
| `S8_VerifyProductQuality` | Rejudge repaired candidate against source | `tools/validators/validate_quality.py`, `tools/judges/compare_source_candidate.py` | `reports/quality_gates.repair0001.json`, `reports/quality_signals.repair0001.json`, `reports/visual_adjudication.repair0001.json`, `reports/repair_loop_0001.json` |
| `S9_VerifyProcessContract` | Validate process artifacts and anti-overfit boundary | `tools/validators/validate_process.py` | `reports/process_audit.json`, `reports/round24_final_verdict.json` |

## Required Evidence

The runner writes:

- `reports/state_trace.json`
- `reports/operation_log.jsonl`
- `reports/decision_log.jsonl`
- `reports/model_interactions.jsonl`
- `reports/quality_signals.json`
- `reports/visual_adjudication.json`
- `reports/repair_patch_0001.json`
- `reports/repair_patch_application_0001.json`
- `reports/quality_signals.repair0001.json`
- `reports/visual_adjudication.repair0001.json`
- `reports/repair_loop_0001.json`
- `reports/round24_state_machine_repair_patch_report.md`

## Model Boundary

Round24 defines model-facing prompt templates but does not invoke a backend model. Each model interaction record must state:

- prompt template id;
- input slots;
- expected output schema;
- `model_backend=not_invoked`;
- local tool that executed the same contract.

## Product Boundary

The repaired candidate is still a candidate until repaired S8 gates pass. The final report must preserve:

- `process_contract_verdict`;
- `product_quality_verdict`;
- `terminal_state`;
- before/after failure counts.
