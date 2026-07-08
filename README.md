# PDF Semantic Translation Workflow Workspace

本工作区用于验证 PDF 语义翻译回填流程：从 PDF 结构提取、语义翻译、布局回填、视觉质量门禁、修复循环，到独立 spike 验证和回归测试。

根目录的职责是说明边界，不承载具体流程细节。完整流程设计继续放在 `docs/业务流程/`。

## 目录职责

| 目录 | 职责 | 是否是稳定执行入口 | 关键边界 |
|---|---|---:|---|
| `pdf_translation_workflow_core/` | 稳定主框架：契约、状态机、提示词模板、工具、可复用布局和质量门禁逻辑 | 是 | 只放通过实验、spike 和回归验证后可复用的能力 |
| `pdf_translation_workflow_lab/` | 实验区：按 round 孵化 core 尚不能处理的新场景或大改动 | 否 | round 可以失败；不能把样本页码、样本文字、样本数值迁入 core |
| `pdf_translation_workflow_regression/` | 回归区：固定 case、baseline、run 结果和验收报告 | 否 | 不存实验源码；不使用人工对照 PDF 作为运行时输入 |
| `spikes/` | 独立验证区：用新的会话验证 core 或 lab promotion 是否可复现 | 否 | 每个 spike 根目录是硬边界；只认包内输入、报告和输出证据 |
| `docs/业务流程/` | 人工维护的主流程设计、状态机、活动流、迁移方案和历史决策 | 否 | 是设计文档入口，不是运行输出目录 |
| `docs/input/` | 历史或人工整理的输入材料 | 否 | 新的稳定输入应进入 regression case；实验输入应进入 lab round |
| `docs/output/` | 历史候选 PDF 和人工查看输出 | 否 | 不再作为新 round 的主目录；新实验进入 lab，新回归进入 regression |
| `docs/reports/` | 历史报告或人工汇总报告 | 否 | 新的运行证据应跟随 lab/regression/spike 所属目录 |
| `docs/测试提示词/` | 面向人工或新会话的测试提示词 | 否 | 稳定运行提示词模板应进入 core；一次性验证提示词应进入对应 spike |
| `样本/` | 原始样本 PDF 和人工对照样本 | 否 | 人工对照只用于结果性评估，不进入运行时判断链路 |
| `测试数据/` | 临时抽页和测试数据 | 否 | 不作为长期契约来源 |
| `tmp/` | 临时文件 | 否 | 可清理，不作为证据源 |

## 推荐工作流

1. `pdf_translation_workflow_lab/rounds/<round_id>/` 中实验新能力。
2. round 产出可迁移能力清单、失败边界、反过拟合检查和报告。
3. 候选能力进入 `pdf_translation_workflow_regression/` 做固定 case 回归。
4. 通过后构建 `spikes/<spike_id>/`，用独立会话验证文档、工具和契约是否完整。
5. spike 和 regression 都能复现后，再把能力合入 `pdf_translation_workflow_core/`。
6. 合入 core 后同步更新 `docs/业务流程/` 中的标准流程设计。

## 不能混用的边界

- `lab` 的 round 输出不能直接视为 core 能力。
- `spikes` 的结果不能反向作为运行时裁决依据；spike 只验证当前包是否自洽。
- `regression` 只验证是否倒退，不孵化新工具。
- 人工对照 PDF 只能用于结果性人工评估，不能作为运行时输入。
- 产品质量 verdict 和流程契约 verdict 必须分开记录，不能互相覆盖。

## 核心文档入口

- 主流程设计：`docs/业务流程/PDF_语义翻译回填_标准流程设计.md`
- 新目录迁移方案：`docs/业务流程/PDF_语义翻译工作流_目录职责与迁移方案.md`
- core 文档副本：`pdf_translation_workflow_core/docs/process/`
- lab 规则：`pdf_translation_workflow_lab/README.md`
- regression 规则：`pdf_translation_workflow_regression/README.md`
- spike 规则：`spikes/README.md`
