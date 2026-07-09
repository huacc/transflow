# Round25 Execution Document

## Scope

Round25 executes the design in:

```text
docs/设计/PDF_语义翻译回填_状态机与工具编排设计.md
```

The goal is not to force product PASS. The goal is to verify that layered judgement, dispatch, repair binding, repair execution, remeasurement, and rollback are auditable across AIA zh/en first-20-page inputs and the round24 regression input.

## Commands

Batch:

```powershell
python run_round25_batch.py
```

Single case:

```powershell
python run_round25_layered_case.py --source-pdf <relative pdf> --translations-json AUTO --source-language zh --target-language en --case-id <id>
```

## State To Tool Mapping

| State | Purpose | Tool or prompt | Required output |
|---|---|---|---|
| `S0_Request` | Confirm source PDF, translation mode, language pair, and non-goals | `run_round25_layered_case.py` | `reports/run_request.json` |
| `S1_ContractLoad` | Load contracts, design, prompt templates, tools, and write-boundary evidence | runner | `reports/contract_load_record.json`, `reports/workspace_boundary_preflight.json` |
| `S2_ToolProbe` | Probe runtime | `tools/probes/probe_runtime.py` | `reports/tool_probe.json` |
| `S3_SourceExtract` | Extract source text, bbox, font, color, and page stats | `tools/probes/extract_source_structure.py` | `reports/source_structure.json` |
| `S4_PageStrategy` | Materialize page strategy from current source evidence | runner | `reports/page_strategy.json` |
| `S5_TranslationPlan` | Materialize or validate semantic translation | `tools/translators/materialize_google_gtx_translations.py`, `S5_materialize_translation.prompt.json`, validation function | `reports/semantic_translations.json` or supplied translations, `reports/semantic_translation_validation.json` |
| `S6_LayoutPlan` | Build role and layout plans | `tools/planners/plan_roles.py`, `tools/planners/plan_layout.py` | `reports/role_plan.json`, `reports/layout_plan.json` |
| `S7_GenerateCandidate` | Generate initial candidate | `tools/generators/generate_candidate.py` | initial PDF, `reports/generation_evidence.json` |
| `S8A_NormalizeQualitySignals` | Compare candidate to source and normalize quality signals | `S8A_quality_signal_normalization.prompt.json`, `tools/judges/compare_source_candidate.py` | `reports/quality_signals.json` |
| `S8B_TriageQualitySignals` | Select failure class by causal priority, not tool choice | `S8B_quality_triage.prompt.json`, `tools/judges/compare_source_candidate.py` | `reports/visual_adjudication.json` |
| `S8C_DispatchAndBindRepairPatch` | Resolve failure class through static dispatch and bind current-run parameters | `contracts/failure_dispatch_table.json`, `S8C_repair_patch_binding.prompt.json`, `tools/repairs/build_repair_patch.py` | `reports/repair_patch_0001.json` |
| `Lx_RepairLoop` | Apply patch, regenerate, remeasure, accept or roll back | `Lx_repair_loop_execution.prompt.json`, `tools/repairs/apply_repair_patch.py` | `reports/layout_plan.repair0001.json`, `reports/repair_patch_application_0001.json`, `reports/repair_loop_0001.json` |
| `S9_VerifyProcessContract` | Validate process artifacts and anti-overfit boundary | `tools/validators/validate_process.py` | `reports/process_audit.json`, `reports/round25_final_verdict.json` |

## Batch Case Preservation

`run_round25_batch.py` runs one case at a time. After each case it copies `reports/`, `output/`, and `previews/` to:

```text
case_runs/<case_id>/
```

The root `reports/` contains only the final batch summary after the batch completes.

## Acceptance Rule

Repair execution is not repair acceptance. A repaired candidate is accepted only if:

1. the selected failure class improves;
2. total blocking failure count decreases;
3. non-selected hard failure classes do not regress.

Otherwise the repaired candidate is retained as evidence but rejected, and the accepted candidate rolls back to the pre-repair PDF.
