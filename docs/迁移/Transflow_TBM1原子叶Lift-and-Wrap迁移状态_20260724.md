# Transflow TBM1 原子叶 Lift-and-Wrap 迁移状态

日期：2026-07-24
依据：`Transflow_Toolbox批量核心迁移与分层集成验收计划_v0.2.md` §4.2

## 1. 状态口径

本文只记录 TBM1 的当前前向迁移事实，不升级历史 Gate，也不代表 Owner 产品接受。
历史 `p9_*_migration.json`、Spike `stage_gate.json` 和既有 run 继续只读。

TBM1 采用以下固定边界：

- Spike 的 Template、Layout、Judge、Repair 等类别私有核心采用 Lift-and-Wrap；
- Provider、叶内重试、整本枚举、线程池、直接 PDF 产物编排不迁入叶；
- 翻译由 `ToolboxPageCoordinator + TranslationPort + TranslationCompleteness` 统一调度；
- 叶只生成绑定 `source_hash + page_no + geometry_hash + owner` 的声明式 `PagePatch`；
- 候选由共享 `PagePatchInterpreter` 物化，最终文档仍由 Finalizer 串行回放批准 Patch；
- 迁移验收只使用 run-private Catalog overlay，默认 Catalog 保持不变；
- TBM1 不宣称双语整本 PDF、全面视觉质量、Owner 接受或生产晋级。

## 2. 当前状态

| 顺序 | Route | 核心迁移 | 工程符合性 | 合同就绪 | 产品质量 | 默认 Catalog |
|---:|---|---|---|---|---|---|
| 1 | `cover` | `LIFTED_AND_WRAPPED` | `READY` | `CONTRACT_READY` | `NOT_EVALUATED` | `KEEP_DISABLED` |
| 2 | `contents` | `NOT_STARTED` | `NOT_EVALUATED` | `NOT_EVALUATED` | `NOT_EVALUATED` | `KEEP_DISABLED` |
| 3 | `end` | `NOT_STARTED` | `NOT_EVALUATED` | `NOT_EVALUATED` | `NOT_EVALUATED` | `KEEP_DISABLED` |
| 4 | `body.flow_text.multi` | `NOT_STARTED` | `NOT_EVALUATED` | `NOT_EVALUATED` | `NOT_EVALUATED` | `KEEP_DISABLED` |
| 5 | `body.table` | `NOT_STARTED` | `NOT_EVALUATED` | `NOT_EVALUATED` | `NOT_EVALUATED` | `KEEP_DISABLED` |
| 6 | `body.anchored_blocks` | `NOT_STARTED` | `NOT_EVALUATED` | `NOT_EVALUATED` | `NOT_EVALUATED` | `KEEP_DISABLED` |
| 7 | `body.flow_text.visual_anchored` | `NOT_STARTED` | `NOT_EVALUATED` | `NOT_EVALUATED` | `NOT_EVALUATED` | `KEEP_DISABLED` |

默认 Catalog SHA-256 仍为：
`a43dccd10447943a8b3701265c9e85638a65a874d6876b8d1493d6f6886a2f8a`。

## 3. `cover` 已完成范围

生产模块：

- `src/transflow/toolboxes/leaves/cover/models.py`
- `src/transflow/toolboxes/leaves/cover/template.py`
- `src/transflow/toolboxes/leaves/cover/layout.py`
- `src/transflow/toolboxes/leaves/cover/toolbox.py`
- `src/transflow/toolboxes/leaves/lifted_contracts.py`

来源映射共 14 项，已达到 `14/14`。其中：

- `models.py`、`template_builder.py`、`layout_planner.py` 的类别主体迁入生产包；
- Spike `engine.py` 中的叶内 Provider、重试、runner 和 Artifact 编排由生产共享链替换；
- Spike `renderer.py` 的直接 PDF 写入由 `PagePatch + PagePatchInterpreter` 替换；
- Spike Prompt 不进入叶运行时，翻译合同由共享 TranslationPort 和完整性门禁负责；
- 历史文档、manifest 和 Gate 仅作为来源证据，不成为运行时依赖。

薄门禁测试位于 `tests/test_toolbox_leaf_migration_tbm1.py`，当前覆盖：

1. 真实分类页生成可回放候选 PDF；
2. 完整译文无法容纳时保留 `COVER_TEXT_OVERFLOW`、proposed Patch 和明确失败诊断；
3. run-private Catalog 启用时唯一解析，默认停用时确定性 fallback；
4. 共享 `execute_many()` 下 `page_concurrency=1` 与 `2` 的页面身份、unit、Patch owner 和结果一致；
5. owner、protected target、Patch binding、默认 Catalog 指纹和非零 CropBox 回归。

本轮使用固定、可复现译文，没有调用真实模型。候选 PDF、失败诊断、PNG 和机器报告只保存在
被 Git 忽略的 `runs/toolbox_leaf_migration/TBM1/`，不进入 GitHub。

## 4. 已知问题与处理边界

以下问题不通过叶内特判掩盖：

- `SHARED-ENGINEERING-CONCURRENCY`：`run_classified()` 的端到端并发接线留到 TBM3；
- `SHARED-KERNEL-OFFSET-CROPBOX`：非零 CropBox 原点页面的 Patch 坐标校验属于共享 Kernel；
- `SHARED-TRANSLATION-ACRONYM-COMPATIBILITY`：历史译文会翻译 `RMB/HKD`，当前生产完整性合同要求原样保留；
- `SHARED-FONT-EMBEDDING-SIZE`：多操作候选重复嵌入受控 CJK 字体导致文件偏大，TBM1 只记录，不做无关优化；
- `COVER_DEDUPLICATION_PATCH_UNSUPPORTED`：现有 `replace_text` Patch 不能诚实表达“只删除重复双语伴随文字”，当前返回硬 Finding 和整页 fallback。

这些限制不妨碍 `cover` 达到薄门禁的 `CONTRACT_READY`，但足以阻止默认 Catalog 晋级和产品质量
结论。后续继续按 `contents → end → body.flow_text.multi → body.table →
body.anchored_blocks → body.flow_text.visual_anchored` 执行。
