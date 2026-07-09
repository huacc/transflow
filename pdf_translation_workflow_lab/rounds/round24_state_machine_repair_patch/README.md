# Round24 State-Machine RepairPatch Package

This is an isolated lab package for validating a layered PDF translation-backfill harness.

It is derived from `round23_state_machine_flow`, but unlike round23 it must execute one real repair loop:

```text
S8 source/candidate judgement -> RepairPatch binding -> Lx apply patch -> S7 regenerate -> S8 rejudge
```

## Boundary

- Runtime inputs are only under `input/`.
- Runtime outputs are only under `reports/`, `output/`, and `previews/`.
- This package must not import from `pdf_translation_workflow_core`.
- Runtime tools must not read human reference PDFs or `offline_reference_compare`.
- Repair decisions must use current-run source/candidate evidence only: bbox, font size, fit status, local overlap baseline, page statistics, and generated evidence.

## Main Command

```powershell
python run_round24_state_machine_repair_patch.py
```

Default input:

- `input/source_pdfs/00005_2025_annual_report_zh_pages_001_020.pdf`
- `input/semantic_translations/R22_GEN_ZH_TO_EN_00005_pages_001_020.translations.json`

Default outputs:

- `output/R24_GEN_ZH_TO_EN_00005_pages_001_020_initial_candidate.pdf`
- `output/R24_GEN_ZH_TO_EN_00005_pages_001_020_repair0001_candidate.pdf`
- `reports/round24_state_machine_repair_patch_report.md`
- `reports/round24_final_verdict.json`

## Layered Judgement

Round24 does not put all judgement into one prompt. It separates the workflow:

| Layer | State | Prompt template | Local tool |
|---|---|---|---|
| Signal normalization | `S8A` | `S8A_quality_signal_normalization.prompt.json` | `tools/judges/compare_source_candidate.py` |
| Triage | `S8B` | `S8B_quality_triage.prompt.json` | `tools/judges/compare_source_candidate.py` |
| Static dispatch | `S8C-Dispatch` | `contracts/failure_dispatch_table.json` | deterministic table lookup |
| Patch binding | `S8C-Binding` | `S8C_repair_patch_binding.prompt.json` | `tools/repairs/build_repair_patch.py` |
| Patch execution | `Lx` | `Lx_repair_loop_execution.prompt.json` | `tools/repairs/apply_repair_patch.py` |

The prompt templates define the model-facing contract. This round uses deterministic local tools and records `model_backend=not_invoked` in `reports/model_interactions.jsonl`.

Triage outputs failure classes only. Repair families and tools are resolved through `contracts/failure_dispatch_table.json`; binding may only fill current-run RepairPatch parameters.

## Success Criteria

This package succeeds as a harness experiment when:

1. the full state trace is recorded;
2. source/candidate comparison produces human-readable judgement;
3. a RepairPatch is built from current-run evidence;
4. the patch is applied to a layout plan;
5. a repaired candidate is regenerated;
6. repaired quality is judged again and the repaired candidate is either accepted or rejected with rollback;
7. the final report honestly separates process verdict and product verdict.

Product quality may still fail. A failed product verdict with complete evidence is acceptable for this experiment.
