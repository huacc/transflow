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
| 2 | `contents` | `LIFTED_AND_WRAPPED` | `READY` | `CONTRACT_READY` | `NOT_EVALUATED` | `KEEP_DISABLED` |
| 3 | `end` | `LIFTED_AND_WRAPPED` | `READY` | `BLOCKED` | `NOT_EVALUATED` | `KEEP_DISABLED` |
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
结论。后续继续按 `end → body.flow_text.multi → body.table →
body.anchored_blocks → body.flow_text.visual_anchored` 执行。

## 5. `contents` 已完成范围

生产模块：

- `src/transflow/toolboxes/leaves/contents/models.py`
- `src/transflow/toolboxes/leaves/contents/template.py`
- `src/transflow/toolboxes/leaves/contents/layout.py`
- `src/transflow/toolboxes/leaves/contents/toolbox.py`
- `src/transflow/toolboxes/leaves/lifted_text_leaf.py`

来源映射共 14 项，已达到 `14/14`。其中：

- `models.py`、`template_builder.py`、`layout_planner.py` 的目录类别主体迁入生产包；
- 目录标题、重复页码锚点、列、层级、辅助文字和相邻行边界仍属于叶私有语义；
- Spike `engine.py` 中的 Provider、重试、runner、线程池和 artifact 编排由生产共享链替换；
- Spike `renderer.py` 的直接 PDF 写入由 `PagePatch + PagePatchInterpreter` 替换；
- Prompt、历史 manifest 和 Gate 只作为来源证据，不形成生产运行时依赖。

`cover` 与 `contents` 已实际共同使用的生命周期机械步骤收敛到
`LiftedAtomicTextToolbox`；类别 Template、Container、Layout、Placement 和 Finding
仍由各叶私有，未抽象单叶推测能力。

薄门禁使用真实分类页 `S2P0303.pdf`，当前证明：

1. 18 个可翻译容器生成 18 个带 owner 和源对象绑定的声明式操作；
2. 13 个页码锚点不进入 TranslationBatch、不成为 Patch target，候选中原位保留；
3. 目录标题、条目、层级和辅助说明可回放为可打开的单页候选；
4. 超长译文保留 `CONTENTS_TEXT_OVERFLOW`、proposed Patch 和明确失败诊断；
5. run-private Catalog、默认停用 fallback、共享并发等价性和默认 Catalog 指纹均通过。

首次栅格检查发现 `CONTENTS` 被共享 `PageTextInventory` 误判为
`CODE_OR_ACRONYM`。修复只调整共享机械判定：较长纯字母全大写标题进入翻译，
`EBITDA` 等短缩写继续 `KEEP_SOURCE`；没有添加目录样本特判。

本轮仍使用固定可复现译文，没有调用真实模型。候选、失败诊断、PNG 和机器报告只保存在
被 Git 忽略的 `runs/toolbox_leaf_migration/TBM1/`，不会上传 GitHub。

新增开放问题：

- `SHARED-FONT-TOUNICODE-COMPATIBILITY`：少数字形的文本抽取为兼容字符，但栅格视觉正常；
- `SHARED-FONT-EMBEDDING-SIZE`：18 个操作生成约 16.7 MB 单页候选，继续只记录；
- `SHARED-ENGINEERING-CONCURRENCY`：端到端接线仍按计划留到 TBM3。

这些限制阻止产品质量结论和默认 Catalog 晋级，但不阻止 `contents` 的当前
`CONTRACT_READY`。后续顺序为 `body.flow_text.multi → body.table →
body.anchored_blocks → body.flow_text.visual_anchored`。

## 6. `end` 核心迁移完成、合同阻塞

生产模块：

- `src/transflow/toolboxes/leaves/end/models.py`
- `src/transflow/toolboxes/leaves/end/template.py`
- `src/transflow/toolboxes/leaves/end/layout.py`
- `src/transflow/toolboxes/leaves/end/toolbox.py`

来源映射共 14 项，已达到 `14/14`。类别私有的联系块、免责声明、公司/品牌、链接、
对齐和局部安全区主体已经迁入；Provider、Prompt、叶内重试、整本 runner、线程池、
直接 PDF 写入和 artifact 编排均由共享生产链替换。

已通过的薄门禁事实：

1. 真实英文联系页 `S2P0120` 生成一个绑定两段源文字的声明式 Patch；
2. 候选可打开且译文可读，Logo 和 URL 保持不变；
3. 超长译文生成 `END_TEXT_OVERFLOW` 和明确标记的失败诊断；
4. 无原生文字页 `S2P0580` 不调用 Provider，显式透传收敛；
5. run-private Catalog、默认停用 fallback、共享并发等价和默认 Catalog 指纹通过。

当前 blocker 为 `END-PROTECTED-PREAUTHORIZATION-GAP`：真实双语联系页 `S2P0040`
中的 `Tel 電話` / `Fax 傳真` 按 Spike 语义应保护，但 Kernel 在 Toolbox 之前冻结的
PageTextInventory 会把其中拉丁 span 预授权为 `TRANSLATE`。叶不能在事后新增
`KEEP_SOURCE/PROTECTED`，因此主链正确返回 `ROUTE_CAPABILITY_MISMATCH`，不调用
Provider，也不生成伪翻译候选。

该问题不能用样本、公司名、文件名、页码或叶内特判处理。按计划 §9.1，
owner/protected 合同失败只阻塞当前叶：`CoreMigration=LIFTED_AND_WRAPPED`、
`EngineeringConformance=READY`，但 `ContractReadiness=BLOCKED`，继续
`KEEP_DISABLED`。后续在 TBM3 双语分层页池确认作用域，再在 TBM4 决定共享事前授权
合同；当前不把 `end` 纳入 ready 集。

本轮没有调用真实模型。候选、失败诊断、PNG、blocker 样本副本和机器报告只保存在
被 Git 忽略的 `runs/toolbox_leaf_migration/TBM1/`。下一叶为
`body.flow_text.multi`。
