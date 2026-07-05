# Round04 Standalone Process And Quality-Gate Validation Prompt

Work only inside this standalone directory:

```text
D:\项目\开源项目\MerqFin\spikes\独立测试\spikes\round04
```

Do not read or depend on parent-directory files unless this prompt explicitly says to compare package integrity. The directory is expected to contain its own copy of workflow core, process docs, prompt docs, and regression inputs.

Path rule: the copied process document may contain historical absolute paths from earlier runs. Treat those as audit evidence only. For execution, resolve all relative workflow paths from the round04 root.

## Goal

Validate whether the packaged workflow can drive:

1. complete process evidence;
2. tool-to-state execution;
3. backend-model prompt slot and output contracts;
4. product-quality failure detection;
5. repair-loop or `S_FAIL_QUALITY` terminal behavior.

Do not optimize for visual quality in this round. The point is contract reproducibility and truthful state transitions.

## Required Reading

```text
pdf_translation_workflow_core\README.md
pdf_translation_workflow_core\contracts\run_modes.md
pdf_translation_workflow_core\contracts\state_machine.md
pdf_translation_workflow_core\contracts\tool_contracts.md
pdf_translation_workflow_core\contracts\decision_contracts.md
pdf_translation_workflow_core\contracts\product_quality_contract.md
pdf_translation_workflow_core\contracts\page_type_repair_matrix.md
pdf_translation_workflow_core\prompts\README.md
pdf_translation_workflow_core\prompts\prompt_manifest.json
pdf_translation_workflow_core\prompts\prompt_tool_bindings.json
pdf_translation_workflow_core\prompts\model_tool_orchestration_contract.md
pdf_translation_workflow_core\prompts\templates\*.prompt.json
pdf_translation_workflow_core\regression\regression_manifest.json
pdf_translation_workflow_core\regression\regression_matrix.md
docs\业务流程\01_source_pdf_中文回填_详细流程记录.md
```

You must treat `docs\业务流程\01_source_pdf_中文回填_详细流程记录.md` as the top-level execution map. The files under `pdf_translation_workflow_core` are the executable contract details.

## Run Mode

Use:

```json
{"run_mode": "product_quality"}
```

This means product-quality failures block success.

If product quality fails because the packaged generic generator is only a smoke-test generator, that is not a round04 failure. It is the expected truthful outcome:

```text
process_contract_verdict: PASS
product_quality_verdict: FAIL
terminal_state: S_FAIL_QUALITY
```

## Regression Inputs

Run at least:

```text
01_source.pdf
测试数据\AIA_2020_Annual_Report_en_pages_08_09_24_25.pdf
```

`01_source.pdf` is a regression anchor. The AIA PDF is a generalization anchor. Do not hardcode either one.

## Required Execution

First run the packaged selftest from the round04 root:

```powershell
python pdf_translation_workflow_core\tools\run_state_machine_selftest.py
```

Then inspect and summarize the generated report under:

```text
docs\reports\pdf_translation_workflow_core\selftest_*
```

If you perform any additional manual or scripted validation, it must follow:

```text
pdf_translation_workflow_core\prompts\prompt_tool_bindings.json
```

The execution pattern is:

```text
tool outputs -> slot normalization -> prompt instance -> model JSON -> decision record -> next state
```

## Required Recorded Artifacts

At minimum, round04 must create or preserve:

```text
docs\reports\pdf_translation_workflow_core\selftest_*\selftest_summary.json
docs\reports\pdf_translation_workflow_core\selftest_*\**\state_trace.json
docs\reports\pdf_translation_workflow_core\selftest_*\**\operation_log.jsonl
docs\reports\pdf_translation_workflow_core\selftest_*\**\decision_log.jsonl
docs\reports\pdf_translation_workflow_core\selftest_*\**\process_validation.json
docs\reports\round04_process_audit.md
```

If you create prompt instances manually, also record:

```text
prompt_instance.json
slot_values.json
model_output.json
decision_record.json
```

If a PDF is generated:

```text
docs\output\
docs\output\previews\
```

Do not copy historical reports from the parent workspace. Round04 must produce its own evidence.

## Final Verdict

The final report must contain:

```text
process_contract_verdict: PASS|FAIL
product_quality_verdict: PASS|FAIL|NOT_ATTEMPTED
anti_overfit_verdict: PASS|FAIL
terminal_state: S_DONE_PROCESS_VALIDATED|S_DONE_PRODUCT_ACCEPTED|S_FAIL_PROCESS_CONTRACT|S_FAIL_QUALITY|S_FAIL_TOOLING
```

In product-quality mode, a run cannot pass if product-quality gates fail.

## Mandatory Failure Behavior

If quality gates fail and cannot be repaired:

```text
process_contract_verdict: PASS
product_quality_verdict: FAIL
terminal_state: S_FAIL_QUALITY
```

Do not hide this behind a generic success statement.

## Mandatory Audit Questions

Your `docs\reports\round04_process_audit.md` must answer:

1. Did every executed tool appear in `operation_log.jsonl`?
2. Did every model/prompt judgement appear in `decision_log.jsonl`?
3. Did each model judgement identify its input artifacts, required output dimensions, verdict, confidence or equivalent, and next state?
4. Did the run use `prompt_tool_bindings.json` to connect states, tools, prompt slots, and next states?
5. Were sample-specific facts confined to manifests, fixtures, tests, or reports?
6. Did `product_quality` failure route to repair loop or `S_FAIL_QUALITY`?
7. Did the final answer split process success from product-quality success?

## Expected Round04 Interpretation

Round04 succeeds if it proves the methodology is executable and honest, even when product quality fails.

Round04 fails if it:

- claims product success when quality gates fail;
- skips operation logs, decision logs, or state traces;
- uses parent reports instead of generating its own evidence;
- treats `01_source` fixture logic as the generic engine;
- hardcodes AIA or `01_source` page facts into workflow logic;
- ignores the prompt-to-tool binding contract.
