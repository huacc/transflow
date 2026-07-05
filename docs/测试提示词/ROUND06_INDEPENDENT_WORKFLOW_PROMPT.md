# Round06 Independent Workflow Prompt

工作目录固定为：

```text
D:\项目\开源项目\MerqFin\spikes\独立测试\spikes\round06
```

所有命令都从这个目录执行。所有输入、输出、报告和审计文件都使用相对路径。

## 1. 本轮目标

Round06 验证的是流程、契约、状态机、工具绑定、候选生成机制和审计记录是否能形成闭环。

Round06 不验证最终中文排版已经达到交付质量，也不要求视觉质量 PASS。

Round06 使用的运行模式是 `backfill_candidate_validation`。不要把它解释成 `product_quality`。

本轮必须证明或否定以下事项：

1. 状态机能进入候选生成验证路径；
2. `S7_GenerateCandidate` 能产生真实候选 PDF；
3. 候选 PDF 能发布到 `docs\output`；
4. 生成过程能留下 `translations.json`、`layout_plan.json`、`candidate_generation_evidence.json`；
5. 质量 gate 能区分“已生成候选”和“语义/产品质量仍失败”；
6. 操作日志、状态迁移日志、裁决日志能够支撑审计；
7. 失败时能定位到明确的契约、状态、工具或输出边界。

## 2. 必读文件

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
2. 在 `docs\reports\round06_execution_audit.md` 记录缺失路径；
3. 最终判定 `package_completeness: FAIL`。

## 3. 允许的执行命令

从 round06 根目录执行：

```powershell
python pdf_translation_workflow_core\tools\run_state_machine_selftest.py --modes backfill_candidate_validation
```

不要使用：

```powershell
python pdf_translation_workflow_core\tools\run_state_machine_selftest.py --generator smoke_copy
python pdf_translation_workflow_core\tools\run_state_machine_selftest.py --modes product_quality --generator backfill_placeholder
```

不要手工复制 PDF 到 `docs\output` 伪造候选文件。

不要修改 `pdf_translation_workflow_core` 下的工具代码。Round06 是执行验证，不是修复轮。

## 4. 必须产生的输出

本轮应产生：

```text
docs\reports\pdf_translation_workflow_core\selftest_*\selftest_summary.json
docs\reports\pdf_translation_workflow_core\selftest_*\operation_log.jsonl
docs\reports\pdf_translation_workflow_core\selftest_*\decision_log.jsonl
docs\reports\pdf_translation_workflow_core\selftest_*\operation_log.jsonl
docs\reports\pdf_translation_workflow_core\selftest_*\decision_log.jsonl
docs\reports\pdf_translation_workflow_core\selftest_*\backfill_candidate_validation\*\candidate_generation_evidence.json
docs\reports\pdf_translation_workflow_core\selftest_*\backfill_candidate_validation\*\translations.json
docs\reports\pdf_translation_workflow_core\selftest_*\backfill_candidate_validation\*\layout_plan.json
docs\reports\pdf_translation_workflow_core\selftest_*\backfill_candidate_validation\*\product_quality_gates.json
docs\reports\pdf_translation_workflow_core\selftest_*\backfill_candidate_validation\*\outputs\candidate.pdf
docs\output\*_backfill_placeholder_candidate.pdf
docs\output\previews\*_backfill_placeholder_page_*.png
docs\reports\round06_execution_audit.md
```

审计报告只引用本轮 `selftest_*` 目录和本轮 `docs\output`。

## 5. 生成证据检查

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

判定规则：

| 字段 | 通过条件 | 失败含义 |
|---|---|---|
| `tool` | 等于 `generate_backfill_candidate` | 状态机没有使用指定生成器 |
| `real_backfill_pdf` | `true` | 候选不是有效回填候选 |
| `translation_provider` | 等于 `deterministic_placeholder` | 本轮不应伪装成真实语义翻译 |
| `redacted_line_count` | 大于 0 | 没有删除可抽取英文 |
| `inserted_line_count` | 大于 0 | 没有插入中文文本 |
| `translations_json` | 文件存在 | 没有输出翻译契约数据 |
| `layout_plan_json` | 文件存在 | 没有输出布局契约数据 |
| `semantic_coverage` | `placeholder_not_semantic` | 不能误判为真实语义翻译 |

## 6. 质量 gate 检查

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

Round06 的默认预期：

```text
text_residue: pass
backfill_generation: pass
translation_authenticity: fail
semantic_coverage: fail
product_quality_verdict: FAIL
terminal_state: S_FAIL_QUALITY
```

失败解释：

| gate | 异常情况 | 记录方式 |
|---|---|---|
| `text_residue` | 失败 | 记录为生成器或检测器缺陷 |
| `backfill_generation` | 失败 | 记录为状态机或生成器执行缺陷 |
| `translation_authenticity` | 通过 | 记录为裁决逻辑缺陷；占位译文不能判定为真实翻译提供方 |
| `semantic_coverage` | 通过 | 记录为裁决逻辑缺陷；占位译文不能判定为语义翻译达标 |

## 7. 状态迁移审计

从 `operation_log.jsonl` 和 `decision_log.jsonl` 审计：

1. `S0_Preflight` 是否出现；
2. `S1_LoadRegression` 是否出现；
3. `S2_ExtractSource` 是否出现；
4. `S3_ClassifyPage` 是否出现；
5. `S4_BuildLayoutContract` 是否出现；
6. `S5_ModelDecision` 是否出现；
7. `S6_RepairPlan` 是否出现；
8. `S7_GenerateCandidate` 是否出现；
9. `S8_QualityGate` 是否出现；
10. 是否进入明确终态；
11. D1-D9 是否都有输入摘要、判断维度、输出字段和工具绑定。

如果日志中没有完整状态迁移，不要根据最终 PDF 猜测流程正确。

## 8. 大模型裁决记录

本工具链可能使用确定性占位裁决模拟大模型判断。必须诚实区分：

| 情况 | 记录方式 |
|---|---|
| 调用了真实模型 | 记录模型名、系统提示词、用户提示词、输入数据摘要、输出 JSON 字段 |
| 未调用真实模型，只用了本地确定性裁决 | 明确写 `model_call_type: deterministic_local_surrogate` |
| 某个裁决字段来自工具脚本 | 明确写工具名和输出文件 |
| 某个裁决字段来自人工阅读 | 明确写 `human/codex_manual_audit`，不能伪装成工具结果 |

## 9. 审计报告

写入：

```text
docs\reports\round06_execution_audit.md
```

报告必须包含：

1. `Workspace Boundary`：工作目录、输入文件、输出目录。
2. `Command Log`：实际运行的命令；如果失败，记录关键错误。
3. `Generated Evidence Inventory`：逐项列出要求输出是否存在。
4. `State Transition Audit`：逐项列出 S0-S8 与终态是否出现。
5. `Decision Audit`：逐项列出 D1-D9 的输入、维度、输出、工具绑定。
6. `Backfill Generation Audit`：逐项列出 `candidate_generation_evidence.json` 的字段和值。
7. `Quality Gate Audit`：逐项列出 `product_quality_gates.json` 的 gate 结果。
8. `Deviation List`：列出任何偏离契约的行为，包括修改代码、新增工具、手工复制 PDF、缺文件、缺日志。
9. `Final Verdict`：给出以下字段：

```json
{
  "round": "round06",
  "run_mode": "backfill_candidate_validation",
  "workspace_boundary": "PASS|FAIL",
  "package_completeness": "PASS|FAIL",
  "process_contract_verdict": "PASS|FAIL",
  "generation_verdict": "PASS|FAIL",
  "product_quality_verdict": "PASS|FAIL",
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

不要声称“高质量翻译完成”。Round06 验证的是流程闭环，不验证最终美观度达标。
