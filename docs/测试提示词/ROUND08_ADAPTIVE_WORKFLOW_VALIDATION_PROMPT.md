# Round08 Adaptive Workflow Validation Prompt

工作目录固定为：

```text
D:\项目\开源项目\MerqFin\spikes\独立测试\spikes\round08
```

所有命令都从这个目录执行。所有输入、输出、报告和审计文件都使用相对路径。

## 1. 本轮目标

Round08 验证的是：当核心文档、契约、工具或提示词不足时，执行 Codex 能否在当前 round 工作区内做最小必要修改，并完整记录修改原因、修改内容、验证结果和回灌建议。

Round08 不是要求一次性做到高质量 PDF 产品交付。它要求：

1. 先按当前包执行 baseline；
2. 识别 baseline 暴露的文档/工具/契约不足；
3. 如果需要修改，必须只改 round08 内文件；
4. 每个修改都要记录到变更日志；
5. 修改后重新运行相关验证；
6. 最终报告说明是否还有需要回灌到根核心目录的变更。

判断核心是否足够的标准：

```text
如果本轮 modification_count = 0，并且目标验证通过，说明当前核心文档/工具对本轮目标是充分的。
如果 modification_count > 0，本轮结果仍有价值，但说明根核心还不完整，后续需要把变更回灌。
```

## 2. 必读文件

先阅读：

```text
README.md
PACKAGE_MANIFEST.md
docs\业务流程\01_source_pdf_中文回填_详细流程记录.md
docs\测试提示词\ROUND08_ADAPTIVE_WORKFLOW_VALIDATION_PROMPT.md
pdf_translation_workflow_core\README.md
pdf_translation_workflow_core\contracts\run_modes.md
pdf_translation_workflow_core\contracts\state_machine.md
pdf_translation_workflow_core\contracts\tool_contracts.md
pdf_translation_workflow_core\contracts\decision_contracts.md
pdf_translation_workflow_core\contracts\product_quality_contract.md
pdf_translation_workflow_core\contracts\page_type_repair_matrix.md
pdf_translation_workflow_core\contracts\change_control_contract.md
pdf_translation_workflow_core\prompts\README.md
pdf_translation_workflow_core\prompts\prompt_manifest.json
pdf_translation_workflow_core\prompts\prompt_tool_bindings.json
pdf_translation_workflow_core\prompts\model_tool_orchestration_contract.md
pdf_translation_workflow_core\prompts\templates\*.prompt.json
pdf_translation_workflow_core\regression\regression_manifest.json
pdf_translation_workflow_core\regression\regression_matrix.md
```

如果必读文件缺失：

1. 不补造缺失文件；
2. 在 `docs\reports\round08_execution_audit.md` 记录缺失路径；
3. 如果缺失文件导致无法执行 baseline，最终判定 `package_completeness: FAIL`。

## 3. 修改权限

允许修改当前 round08 工作区内这些内容：

```text
docs\业务流程
docs\测试提示词
pdf_translation_workflow_core\contracts
pdf_translation_workflow_core\prompts
pdf_translation_workflow_core\tools
pdf_translation_workflow_core\regression
```

禁止修改：

```text
01_source.pdf
测试数据\AIA_2020_Annual_Report_en_pages_08_09_24_25.pdf
父目录或其他 round 目录
```

所有修改必须遵守：

```text
pdf_translation_workflow_core\contracts\change_control_contract.md
```

## 4. Baseline 快照

执行任何修改前，先生成修改前清单：

```powershell
python pdf_translation_workflow_core\tools\validators\collect_change_manifest.py --root . --out docs\reports\round08_change_manifest_before.json
```

如果该工具失败，必须在报告中记录失败原因，并用手工文件清单替代。

## 5. Baseline 执行

从 round08 根目录执行：

```powershell
python pdf_translation_workflow_core\tools\run_state_machine_selftest.py --modes backfill_candidate_validation
```

记录：

```text
stdout
exit_code
selftest_dir
candidate_pdf_paths
quality_gate_summary
```

## 6. 判断是否需要修改

以下任一情况出现，允许进入 `Ax_AdaptiveChange`：

1. 必需文件缺失；
2. 命令失败；
3. 根级或样本级日志缺失；
4. 状态迁移或 D1-D9 不完整；
5. 输出 PDF 与任务目标明显不一致，但现有文档没有说明这是预期；
6. 质量 gate 缺少能解释失败的维度；
7. 执行提示词和核心契约矛盾；
8. 需要新增报告字段才能让下一轮复盘。

如果不需要修改，仍然写报告，并声明：

```json
{
  "modification_count": 0,
  "core_sufficiency_observed": "PASS"
}
```

## 7. 修改记录要求

每次修改都必须追加到：

```text
docs\reports\round08_change_log.md
```

每个修改条目必须包含：

```text
change_id
trigger_failure
hypothesis
files_changed
change_type
before_evidence
after_evidence
verification_command
result
core_backport_recommendation
```

如果修改了工具代码，必须说明：

1. 为什么文档或提示词修改不足以解决；
2. 是否改变了状态机语义；
3. 是否新增了依赖；
4. 如何避免样本过拟合。

## 8. 修改后验证

完成修改后，至少运行与修改相关的最小验证。

如果修改影响状态机、生成器、验证器或契约，应再次运行：

```powershell
python pdf_translation_workflow_core\tools\run_state_machine_selftest.py --modes backfill_candidate_validation
```

不要隐藏失败。失败也要记录。

## 9. 修改后快照和差异

最终生成修改后清单和 delta：

```powershell
python pdf_translation_workflow_core\tools\validators\collect_change_manifest.py --root . --out docs\reports\round08_change_manifest_after.json --baseline docs\reports\round08_change_manifest_before.json --delta-out docs\reports\round08_change_manifest_delta.json
```

如果该工具失败，必须在 `round08_execution_audit.md` 中记录，并手工列出改动文件。

## 10. 审计报告

写入：

```text
docs\reports\round08_execution_audit.md
```

报告必须包含：

1. `Workspace Boundary`
2. `Baseline Command Log`
3. `Baseline Findings`
4. `Adaptive Change Decisions`
5. `Change Inventory`
6. `Post-change Verification`
7. `Generated Evidence Inventory`
8. `Quality Gate Audit`
9. `Backport Recommendations`
10. `Final Verdict`

最终 verdict JSON：

```json
{
  "round": "round08",
  "run_mode": "adaptive_backfill_candidate_validation",
  "workspace_boundary": "PASS|FAIL",
  "package_completeness": "PASS|FAIL",
  "baseline_process_contract_verdict": "PASS|FAIL|NOT_RUN",
  "post_change_process_contract_verdict": "PASS|FAIL|NOT_RUN",
  "generation_verdict": "PASS|FAIL|NOT_RUN",
  "root_log_aggregation": "PASS|FAIL|NOT_RUN",
  "product_quality_verdict": "PASS|FAIL|NOT_ATTEMPTED",
  "expected_quality_failure_observed": "PASS|FAIL|NOT_APPLICABLE",
  "modification_count": 0,
  "core_sufficiency_observed": "PASS|FAIL",
  "requires_core_backport": true,
  "requires_design_revision": true
}
```

Rules:

- If `modification_count > 0`, then `core_sufficiency_observed` must be `FAIL`.
- If `modification_count > 0`, then `requires_core_backport` must be `true`.
- If no modification was needed and all target checks passed, `core_sufficiency_observed` may be `PASS`.

## 11. 最终回复

最终只汇报：

1. 是否产生候选 PDF；
2. 候选 PDF 路径；
3. 审计报告路径；
4. 变更日志路径；
5. `modification_count`；
6. 最终 verdict JSON；
7. 如果修改了文件，列出需要回灌到根核心目录的文件。

不要声称“高质量翻译完成”，除非真实 `product_quality` 通过并有对应证据。
