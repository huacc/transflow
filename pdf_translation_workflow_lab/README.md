# PDF Translation Workflow Lab

本目录是 PDF 语义翻译回填工作流的实验区。

## 职责

- 承接 `pdf_translation_workflow_core` 暂时不满足的新场景。
- 允许在独立 round 中大幅修改工具、契约、提示词和裁决逻辑。
- 保留实验输入、候选 PDF、预览图、报告和失败证据。
- 输出可合入 core 的能力清单，而不是直接成为稳定主框架。

## 禁止事项

- 不作为稳定运行入口。
- 不要求每个 round 都产品质量通过。
- 不把 round 中的样本页码、样本文字、样本数值迁移到 core。
- 不让 spikes 直接依赖 lab 的历史输出作为裁决依据。

## 标准流程

1. 在 `rounds/roundXX_name/` 中做实验。
2. 在 round 内记录真实运行日志、质量门禁、反过拟合扫描和失败边界。
3. 在 round 的 `promotion/` 或说明文档中列出可迁移能力。
4. 由独立 `spikes/spikeXX` 验证契约和工具是否完整。
5. 验证稳定后，再把能力合入 `pdf_translation_workflow_core`。

## Round 目录结构

每个 round 应尽量保持独立：

- `README.md`：本轮目标、边界、当前状态。
- `EXECUTION.md`：可执行流程、状态到工具的映射。
- `contracts/`：本轮契约和门禁。
- `tools/`：本轮实验工具。
- `prompts/`：本轮提示词模板。
- `input/`：本轮运行输入。
- `reports/`：本轮运行证据。
- `output/`：本轮候选 PDF。
- `previews/`：本轮渲染预览。
- `promotion/`：可迁移到 core 的能力清单和合入检查表。

round 是实验载体，不是稳定主框架。只有经过 spike 验证和 regression 回归的能力才能进入 `pdf_translation_workflow_core`。

## 当前迁移

- `rounds/round22_table_layout/` 来自旧位置 `docs/output/round22`。
- 旧位置暂时保留，确认新结构无误后再单独清理。
