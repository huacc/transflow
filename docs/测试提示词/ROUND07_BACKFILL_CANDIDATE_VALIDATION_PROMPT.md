# Round07 Backfill Candidate Validation Prompt

工作目录固定为：

```text
D:\项目\开源项目\MerqFin\spikes\独立测试\spikes\round07
```

所有命令都从这个目录执行。所有输入、输出、报告和审计文件都使用相对路径。

## 1. 责任模型

| 角色 | 责任 |
|---|---|
| Codex 执行会话 | 阅读本提示词、阅读契约、执行命令、审计输出、写报告、给最终 verdict |
| `run_state_machine_selftest.py` | 生成状态机、自测、候选 PDF、日志和质量 gate 等机器证据 |
| 生成器/验证器/渲染器 | 被状态机脚本调用，完成局部工具动作 |

不要把“脚本执行成功”直接等同于“任务完成”。Codex 必须审计脚本输出。

## 2. 本轮目标

Round07 验证的是 `backfill_candidate_validation` 模式：

1. 能否读取回归输入；
2. 能否抽取 PDF 结构；
3. 能否删除可抽取英文并插入中文占位候选；
4. 能否发布候选 PDF 和预览图到 `docs\output`；
5. 能否生成 `translations.json`、`layout_plan.json`、`candidate_generation_evidence.json`；
6. 能否生成根级和每个回归样本级的 `operation_log.jsonl`、`decision_log.jsonl`；
7. 能否明确把占位译文阻断在产品质量之外。

Round07 不验证最终中文排版已经达到交付质量，也不要求 `product_quality_verdict: PASS`。

## 3. 必读文件

先阅读：

```text
README.md
PACKAGE_MANIFEST.md
docs\业务流程\01_source_pdf_中文回填_详细流程记录.md
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
```

如果必读文件缺失：

1. 不补造缺失文件；
2. 在 `docs\reports\round07_execution_audit.md` 记录缺失路径；
3. 最终判定 `package_completeness: FAIL`。

## 4. 执行命令

从 round07 根目录执行：

```powershell
python pdf_translation_workflow_core\tools\run_state_machine_selftest.py --modes backfill_candidate_validation
```

不要执行：

```powershell
python pdf_translation_workflow_core\tools\run_state_machine_selftest.py --modes product_quality --generator backfill_placeholder
python pdf_translation_workflow_core\tools\run_state_machine_selftest.py --generator smoke_copy
```

不要手工复制 PDF 到 `docs\output` 伪造候选文件。

不要修改 `pdf_translation_workflow_core` 下的工具代码。Round07 是执行验证，不是修复轮。

## 5. 必须产生的输出

本轮应产生：

```text
docs\reports\pdf_translation_workflow_core\selftest_*\selftest_summary.json
docs\reports\pdf_translation_workflow_core\selftest_*\operation_log.jsonl
docs\reports\pdf_translation_workflow_core\selftest_*\decision_log.jsonl
docs\reports\pdf_translation_workflow_core\selftest_*\backfill_candidate_validation\*\operation_log.jsonl
docs\reports\pdf_translation_workflow_core\selftest_*\backfill_candidate_validation\*\decision_log.jsonl
docs\reports\pdf_translation_workflow_core\selftest_*\backfill_candidate_validation\*\candidate_generation_evidence.json
docs\reports\pdf_translation_workflow_core\selftest_*\backfill_candidate_validation\*\translations.json
docs\reports\pdf_translation_workflow_core\selftest_*\backfill_candidate_validation\*\layout_plan.json
docs\reports\pdf_translation_workflow_core\selftest_*\backfill_candidate_validation\*\product_quality_gates.json
docs\reports\pdf_translation_workflow_core\selftest_*\backfill_candidate_validation\*\outputs\candidate.pdf
docs\output\*_backfill_placeholder_candidate.pdf
docs\output\previews\*_backfill_placeholder_page_*.png
docs\reports\round07_execution_audit.md
```

审计报告只引用本轮 `selftest_*` 目录和本轮 `docs\output`。

## 6. 生成证据检查

对每个 `backfill_candidate_validation\*` 回归样本读取 `candidate_generation_evidence.json`。

必须逐项记录：

```json
{
  "tool": "generate_backfill_candidate",
  "real_backfill_pdf": true,
  "translation_provider": "deterministic_placeholder",
  "redacted_line_count": ">0",
  "inserted_line_count": ">0",
  "translations_json": "present",
  "layout_plan_json": "present",
  "semantic_coverage": "placeholder_not_semantic"
}
```

## 7. 质量 gate 检查

对每个 `product_quality_gates.json` 记录：

```text
text_residue
backfill_generation
translation_authenticity
semantic_coverage
product_quality_verdict
terminal_state
blocking_failures
```

Round07 的默认预期：

```text
text_residue: pass
backfill_generation: pass
translation_authenticity: fail
semantic_coverage: fail
product_quality_verdict: FAIL
terminal_state: S_FAIL_QUALITY
```

如果 `translation_authenticity` 或 `semantic_coverage` 通过，记录为裁决逻辑缺陷，因为占位译文不能被判定为真实语义翻译。

## 8. 状态迁移审计

从 `state_trace.json`、`operation_log.jsonl` 和 `decision_log.jsonl` 审计：

1. 是否进入 `S7_GenerateCandidate`；
2. 是否进入 `S8_VerifyProductQuality`；
3. 是否进入明确终态；
4. D1-D9 是否都有输入摘要、判断维度、输出字段和工具绑定；
5. 根级聚合日志是否存在；
6. 每个回归样本级日志是否存在。

如果日志中没有完整状态迁移，不要根据最终 PDF 猜测流程正确。

## 9. 审计报告

写入：

```text
docs\reports\round07_execution_audit.md
```

报告必须包含：

1. `Workspace Boundary`：工作目录、输入文件、输出目录。
2. `Command Log`：实际运行的命令；如果失败，记录关键错误。
3. `Generated Evidence Inventory`：逐项列出要求输出是否存在。
4. `State Transition Audit`：逐项列出关键状态与终态是否出现。
5. `Decision Audit`：逐项列出 D1-D9 的输入、维度、输出、工具绑定。
6. `Backfill Generation Audit`：逐项列出 `candidate_generation_evidence.json` 的字段和值。
7. `Quality Gate Audit`：逐项列出 `product_quality_gates.json` 的 gate 结果。
8. `Deviation List`：列出任何偏离契约的行为。
9. `Final Verdict`：给出以下字段：

```json
{
  "round": "round07",
  "run_mode": "backfill_candidate_validation",
  "workspace_boundary": "PASS|FAIL",
  "package_completeness": "PASS|FAIL",
  "process_contract_verdict": "PASS|FAIL",
  "generation_verdict": "PASS|FAIL",
  "root_log_aggregation": "PASS|FAIL",
  "product_quality_verdict": "FAIL",
  "expected_quality_failure_observed": "PASS|FAIL",
  "terminal_state": "S_FAIL_QUALITY|S_FAIL_PROCESS|S_FAIL_PACKAGE|OTHER",
  "requires_design_revision": true
}
```

## 10. 最终回复

最终只汇报：

1. 是否产生候选 PDF；
2. 候选 PDF 路径；
3. 审计报告路径；
4. 最终 verdict JSON；
5. 如果失败，失败发生在哪个契约、状态、工具或输出边界。

不要声称“高质量翻译完成”。Round07 验证的是占位候选生成闭环，不验证最终美观度达标。
