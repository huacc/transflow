# PDF 语义翻译工作流目录职责与迁移方案

## 目标

本次目录调整的目标是把“稳定主框架、实验轮次、独立验证、回归测试”拆开，避免 `docs/output/roundXX` 同时承担源码、实验、报告和输出的多重职责。

## 新目录职责

| 目录 | 职责 | 是否可作为稳定执行入口 |
|---|---|---|
| `pdf_translation_workflow_core/` | 稳定主框架，保存通用契约、状态机、提示词、工具和可复用能力 | 是 |
| `pdf_translation_workflow_lab/` | 实验区，保存 round 级大改动、新能力孵化和候选证据 | 否 |
| `pdf_translation_workflow_regression/` | 回归区，保存固定测试用例、基线说明、运行结果和验收报告 | 否 |
| `spikes/` | 独立验证区，用新会话验证 core 或 lab promotion 是否可复现 | 否 |
| `docs/业务流程/` | 人工维护的业务流程设计和历史决策文档入口 | 否 |

## 迁移策略

当前阶段只复制迁移，不删除旧目录。

1. `docs/output/round22` 已复制到：
   `pdf_translation_workflow_lab/rounds/round22_table_layout`
2. `docs/业务流程/PDF_语义翻译回填_标准流程设计.md` 已复制到：
   `pdf_translation_workflow_core/docs/process/PDF_语义翻译回填_标准流程设计.md`
3. 原始 `docs/output/round22` 和 `docs/业务流程/PDF_语义翻译回填_标准流程设计.md` 暂时保留。

## README 职责

- `README.md` 保存整个工作区的目录职责、边界契约和推荐工作流。
- `pdf_translation_workflow_lab/README.md` 保存 lab 全局规则、round 标准结构、合入边界和禁止事项。
- `pdf_translation_workflow_lab/rounds/README.md` 只作为 round 实例目录的索引说明，不承载全局流程规则。
- `pdf_translation_workflow_core/docs/` 保存 core 相关流程副本、promotion 说明和后续设计资料。
- `spikes/README.md` 保存独立 spike 验证区的职责、包边界和验收口径。
- `spikes/SPIKE_PACKAGE_CONTRACT.md` 保存新建 spike 包必须满足的最小契约。
- `docs/业务流程/` 保留人工维护的主流程设计文档和迁移方案，不迁入实验产物。

## 后续合入原则

- round 只能证明能力候选，不能直接等于 core 能力。
- spike 只验证契约、工具、状态机和文档是否完整，不作为能力源码。
- regression 用于证明合入 core 后不破坏旧样本。
- core 中不得出现样本页码、样本文字、样本数值或人工对照文件路径作为运行时判断条件。

## round22 合入候选

round22 当前可作为合入候选的能力包括：

- `table_cell_split`
- `table_neighbor_header_binding`
- `table_region_obstacle_pack`
- `table_cell_font_floor`

这些能力需要作为独立 role/layout/gate/repair action 进入 core，而不是整体复制 round22 的实验脚本。

## 清理条件

只有满足以下条件后，才考虑删除旧位置的实验产物：

- 新目录中的 round22 快照完整。
- core 文档副本可被独立 spike 使用。
- 至少一次 spike 使用新目录结构完成验证。
- Git 中保留了迁移提交，能够追溯旧目录和新目录的对应关系。
