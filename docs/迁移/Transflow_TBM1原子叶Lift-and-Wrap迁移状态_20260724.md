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
| 4 | `body.flow_text.multi` | `LIFTED_AND_WRAPPED` | `READY` | `BLOCKED` | `NOT_EVALUATED` | `KEEP_DISABLED` |
| 5 | `body.table` | `LIFTED_AND_WRAPPED` | `READY` | `BLOCKED` | `NOT_EVALUATED` | `KEEP_DISABLED` |
| 6 | `body.anchored_blocks` | `LIFTED_AND_WRAPPED` | `READY` | `BLOCKED` | `NOT_EVALUATED` | `KEEP_DISABLED` |
| 7 | `body.flow_text.visual_anchored` | `LIFTED_AND_WRAPPED` | `READY` | `BLOCKED` | `NOT_EVALUATED` | `KEEP_DISABLED` |

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
`body.table`。

## 7. `body.flow_text.multi` 核心迁移完成、合同阻塞

生产模块：

- `src/transflow/toolboxes/leaves/body_flow_text_multi/models.py`
- `src/transflow/toolboxes/leaves/body_flow_text_multi/template.py`
- `src/transflow/toolboxes/leaves/body_flow_text_multi/layout.py`
- `src/transflow/toolboxes/leaves/body_flow_text_multi/toolbox.py`

纠正后的来源冻结清单共 55 项，当前映射达到 `55/55`。此前一次工具输出截断把
`layout_planner.py`、`models.py` 以及 `orchestrator/`、`probes/`、`repairs/` 下
21 个文件折叠为一个坏路径；本轮只前向修正 TBM1 当前清单，重新核对文件、字节和 SHA-256，
没有修改 TBM0 历史证据。

本轮保留并工程化适配了两至三栏聚类、`ColumnBand`、`ColumnAssignment`、跨栏
`span`、栏优先阅读顺序、同栏段落碎片合并和有界字号 ladder。Provider、Prompt、叶内
重试、Qwen/httpx 裁决、整本枚举、线程池、直接 PDF 产物写入和 artifact 编排没有进入
生产叶；依赖物化候选的 spacing、anchor、density 和 typography 规则明确交给共享
Judge/Repair，不形成 Spike 运行时依赖。

真实分类页 `S2P0986` 当前证明：

1. 从源几何建立两个栏带，生成 14 个生产翻译单元和 14 个源对象绑定操作；
2. 短固定 bundle 的候选可打开、两栏分离且无相互覆盖；
3. 超长 bundle 产生 `MULTI_TEXT_OVERFLOW` 和明确标记的红框失败诊断；
4. 段落中的正文 span 进入翻译，已预授权的数字/缩写通过
   `inline_keep_source_object_ids` 保留，Patch 仍绑定完整段落；
5. run-private Catalog、默认停用 fallback、共享并发 1/2 等价、protected target、
   Patch binding、Ruff、Mypy 和默认 Catalog 指纹均通过。

短固定 bundle 只用于证明结构与合同接线，不是语义翻译或产品质量证据。本轮没有调用真实
模型。

当前有两个合同 blocker：

- `MULTI-SHARED-MARGIN-OWNER-GAP`：运行页眉在 `SemanticUnitMap` 中已归属
  `shared.margin.header`，但当前单 owner `PagePatch` 仍由 multi 叶生成这些操作。
  不能用叶内 owner 回退或静默 `KEEP_SOURCE` 掩盖，需在 TBM3 接通共享 margin 执行边界；
- `MULTI-INVENTORY-HASH-PROJECTION-GAP`：扩展真实分类池探测时，`S2P0987` 在 Provider
  前因合并容器投影中的原生文字内容哈希变化而被完整性合同拒绝。后续必须按结构最小化并修复
  通用 block/span 投影，禁止页号、文件名、原文或固定坐标特判。

另有 `MULTI-MATERIALIZED-DENSITY-JUDGE-GAP`：比例较密的固定 bundle 曾证明预物化
`fit` 不能替代真实候选 Judge；混合 CJK/Latin 回放仍可能产生基线拥挤。该项阻止密集排版
和产品质量结论，TBM3 必须使用记录的真实 bundle 接通物化 Judge/Repair 后再验收。

因此当前状态为 `CoreMigration=LIFTED_AND_WRAPPED`、
`EngineeringConformance=READY`、`ContractReadiness=BLOCKED`，默认 Catalog 继续
`KEEP_DISABLED`。候选、诊断、PNG 和机器报告仅保存在被 Git 忽略的 `runs/`。下一叶为
`body.table`。

## 8. `body.table` 核心迁移完成、合同阻塞

生产模块：

- `src/transflow/toolboxes/leaves/body_table/models.py`
- `src/transflow/toolboxes/leaves/body_table/template.py`
- `src/transflow/toolboxes/leaves/body_table/layout.py`
- `src/transflow/toolboxes/leaves/body_table/toolbox.py`

来源冻结清单共 14 项，当前映射达到 `14/14`。生产叶保留并适配了 Spike 的逻辑格
所有权、横向守列、纵向 fit、源对齐、保护数字/代码和有限字号/行距 ladder；事实只来自
当前 Kernel 的 `table_objects`、`cell_bboxes`、`text_spans` 和事前冻结
`PageTextInventory`。Provider、Prompt、叶内重试、整本 runner、线程池、直接产物编排
没有迁入叶，候选仍由共享 `PagePatchInterpreter` 物化。

首次候选虽然未越过 Kernel 原始大格，但把左侧标签、注号和多个业务行聚合在一起。视觉复核
据此否决该实现，并按 Spike“PDF 绘制格不等于逻辑单元格”的经验前向修正：用跨列重复行界
和窄数值/注号锚点恢复逻辑行列，再按逻辑格建立 unit。修正完全依赖当前页结构事实，不含
样本 ID、文件名、页码、标题、业务数字或绝对坐标分支；被否决的历史 run 保留，不覆盖。

最终真实代表页 `00356_DT CAPITAL_英文_2025_p095_body_table.pdf` 当前证明：

1. 一个 Kernel 表恢复为 61 个逻辑单元，其中 22 个可译单元生成 22 个声明式操作；
2. 标签、注号和两期数值列保持各自逻辑格，纯数字格不进入 TranslationBatch；
3. 短固定 bundle 候选可打开，22 个操作均完整物化，表格线和保护数值保留；
4. 超长 bundle 产生 `CELL_TEXT_OVERFLOW`，并以诊断模式在原 owner 内缩小物化，保留
   红框、译后文字和明确“非产品候选”标记；
5. run-private Catalog、默认停用 fallback、共享并发 1/2 等价、Patch binding、Ruff、
   Mypy 和默认 Catalog 指纹均通过。

短固定 bundle 只证明行列、owner、Patch 和物化接线，不是语义翻译或完整视觉产品证据。
本轮没有调用真实模型。

当前 blocker：

- `TABLE-KERNEL-DIRECT-EVIDENCE-GAP`：已分类无边框代表页
  `00235_CSC HOLDINGS_英文_2025_p080_body_table.pdf` 的生产 Kernel
  `table_objects` 为空。叶不读取文件身份重建第二套抽取器；应在 TBM3 分层页池定界后，
  由 Kernel/FAMILY 层补充线条、填色和重复数值锚点的通用直接证据；
- `TABLE-SHARED-MARGIN-OWNER-GAP`：文字页脚已被 `SemanticUnitMap` 归属
  `shared.margin.footer`，但当前单 owner Patch 仍由 `body.table` 承载。页眉页脚语义没有
  写入表格私有规则，但共享执行边界尚未接通；
- `TABLE-MATERIALIZED-GLYPH-LINE-JUDGE-GAP`：当前薄门禁验证了格内 bbox、实际物化和
  人工栅格复核，但 Spike 的“最终 glyph 与表格线相交”机器复裁尚未通过共享 Judge 在
  分层页池统一接通，不能据此宣称密集表格产品通过。

因此当前状态为 `CoreMigration=LIFTED_AND_WRAPPED`、
`EngineeringConformance=READY`、`ContractReadiness=BLOCKED`，默认 Catalog 继续
`KEEP_DISABLED`。候选、诊断、PNG 和机器报告仅保存在被 Git 忽略的 `runs/`。下一叶为
`body.anchored_blocks`。

## 9. `body.anchored_blocks` 核心迁移完成、合同阻塞

生产模块：

- `src/transflow/toolboxes/leaves/body_anchored_blocks/models.py`
- `src/transflow/toolboxes/leaves/body_anchored_blocks/template.py`
- `src/transflow/toolboxes/leaves/body_anchored_blocks/layout.py`
- `src/transflow/toolboxes/leaves/body_anchored_blocks/toolbox.py`

来源冻结清单共 16 项，当前映射达到 `16/16`。生产叶保留并工程化适配了 Spike 的独立
block owner、视觉背景边界、安全 slot、源对齐、保护对象、有限字号/行距 ladder 和同源
样式一致性；结构事实只来自当前 Kernel 的 `text_spans`、`image_objects`、
`drawing_objects` 和事前冻结 `PageTextInventory`。Prompt、Provider、必备字面量重试、
整页 runner、线程池、直接 PDF 写入和 artifact 编排没有迁入叶，候选仍由共享
`PagePatchInterpreter` 物化。

页眉页脚没有写入 anchored 正文私有分类规则：模板显式标为
`shared.margin.header/footer`，语义映射也继续归属共享 margin owner。由于当前生产合同
仍是一页一个 `PagePatch.owner`，实际操作暂由 `body.anchored_blocks` 承载；这被保留为
合同 blocker，而不是在叶内静默 `KEEP_SOURCE` 或复制一套全局翻译逻辑。

第一次物化 run 发现同构卡片会交替形成视觉 owner 和派生 owner：生产 Kernel 的卡片
drawing bbox 从标题条中部开始，顶部标题与卡片垂直覆盖约 44%，刚好低于 Spike 的
45% 门槛，造成部分标题 owner 与卡片 owner 重叠。该候选被视觉复核否决并保留。随后
做了通用前向校准：只对“文字中心仍在视觉区内”的候选把覆盖门槛调整为 35%，不含样本
ID、文件名、页码、标题、业务数字或绝对坐标分支。

最终真实代表页 `AB_EN_12_01978_p068.pdf` 当前证明：

1. 18 个可译容器归入 10 个互不重叠 owner，其中 7 个卡片 owner 有 Kernel 视觉背景
   直接证据，另有 1 个派生正文 owner 和 2 个共享 margin owner；
2. 18 个声明式 Patch 操作均绑定原生 source object，写入框不越过各自安全边界；
3. 固定短 bundle 候选可打开，卡片标题/正文保持槽位，图标、边框和背景保留，同源样式
   采用同一缩放级别；
4. 纯数字和卡片标签按事前 Inventory 保护；混合数字、日期和缩写通过
   `inline_keep_source_object_ids` 进入必备字面量合同；
5. 超长 bundle 产生 `ANCHORED_BLOCK_TEXT_OVERFLOW`，诊断模式在原 owner 内缩小物化
   译文，并保留红框与明确“非产品候选”标记；
6. run-private Catalog、默认停用 fallback、共享并发 1/2 等价、Ruff、Mypy 和默认
   Catalog 指纹均通过。

固定短 bundle 只证明 owner、slot、样式、Patch 和物化接线，不是语义翻译或盲测产品
证据。本轮没有调用真实模型；Spike 原始盲测仅 `4/6`，失败引导后的修复证据属于非盲
回归，本轮没有把它升级为当前产品通过。

当前 blocker：

- `ANCHORED-SHARED-MARGIN-OWNER-GAP`：页眉页脚已被模板和 `SemanticUnitMap` 识别为
  共享 owner，但单 owner Patch 执行边界尚未接通。正文叶不得承担全局 margin 决策；
- `ANCHORED-KERNEL-SEPARATOR-SEGMENT-GAP`：Spike 可通过重新打开源 PDF 读取线段级
  separator 并收窄 cell slot；生产 Kernel 当前只暴露 drawing bbox/content hash。
  叶没有私自重开 PDF 或建立第二套抽取器，依赖内部线段的页面需由 Kernel/FAMILY 层
  补充通用直接事实；
- `ANCHORED-MATERIALIZED-GLYPH-COLLISION-JUDGE-GAP`：2× 栅格复核已确认当前固定
  bundle 的 owner、对齐和保护背景，但最终 painted glyph 碰撞、不可读窄行和跨 owner
  机器复裁尚未通过共享 Judge 在分层页池统一接通。

因此当前状态为 `CoreMigration=LIFTED_AND_WRAPPED`、
`EngineeringConformance=READY`、`ContractReadiness=BLOCKED`，默认 Catalog 继续
`KEEP_DISABLED`。候选、诊断、PNG 和机器报告仅保存在被 Git 忽略的 `runs/`。下一叶为
`body.flow_text.visual_anchored`。

## 10. `body.flow_text.visual_anchored` 核心迁移完成、合同阻塞

生产模块：

- `src/transflow/toolboxes/leaves/body_flow_text_visual_anchored/models.py`
- `src/transflow/toolboxes/leaves/body_flow_text_visual_anchored/template.py`
- `src/transflow/toolboxes/leaves/body_flow_text_visual_anchored/layout.py`
- `src/transflow/toolboxes/leaves/body_flow_text_visual_anchored/toolbox.py`

来源冻结清单共 17 项，最终映射达到 `17/17`。生产叶保留并工程化适配了 Spike 的
`VisualTextSlot`、视觉背景/锚点对象绑定、源 `LEFT/RIGHT/CENTER` 对齐、安全
layout search region、hard boundary、有限字号/行距 ladder 和真实目标字形测量。
Template 只读取当前 Kernel 的 `text_spans`、`image_objects`、`drawing_objects` 与事前
冻结的 `PageTextInventory`；Prompt、Provider、Provider 重试、样本准备、整页 runner、
线程池和直接 PDF 写入均未进入叶运行时。候选仍只由共享 `PagePatchInterpreter` 物化。

本轮没有照搬两项已被 P12 冻结 holdout 否定的策略：

1. Spike 曾按脚本、字号和几何距离自动把疑似目标语言伴随容器设为
   `render_text=False`。历史 holdout 暴露了跨区双语正文、姓名/职务和普通大写词误判。
   生产迁移只建立结构候选，并输出
   `VISUAL_BILINGUAL_SEMANTIC_DECISION_REQUIRED`；在共享语义身份合同完成前，不按几何
   静默删除任何源文字，也不把疑似伴随关系当成已确认同义关系。
2. Spike Template 通过重新打开源 PDF 栅格采样背景色。生产 Kernel 当前只暴露视觉对象
   bbox 与内容哈希，分类 PNG 又明确是分类专用事实。生产叶既没有重开 PDF，也没有越权
   消费分类 PNG；视觉背景槽位记录 `KERNEL_GEOMETRY_ONLY`，并输出
   `VISUAL_BACKGROUND_EVIDENCE_MISSING`，而不是假定白底或伪造对比度 PASS。

页眉页脚继续按全局语义处理。代表页中的自然语言页脚在 Template 和
`SemanticUnitMap` 中归属 `shared.margin.footer`，页码仍由事前 Inventory 机械保护。
由于当前 `PagePatch` 只允许一个根 owner，页脚提议操作暂时仍由
`body.flow_text.visual_anchored` Patch 承载；该差异保留为共享 owner blocker，不在叶内
再造一套页眉页脚翻译流程。

真实代表页 `EN_00468_p0010.pdf` 当前证明：

1. 16 个原生文字 span 被 5 个容器和事前保护对象完整覆盖，容器间无重复归属；
2. 5 个容器保持源对齐，其中标题为左锚、签名姓名为右锚、职务和正文为左锚；
3. 3 个槽位绑定 Kernel 视觉几何证据，2 个槽位明确位于默认页面画布，固定照片和绘图
   对象全部保持锁定；
4. 5 个声明式提议 Patch 操作均绑定真实 source object、源擦除矩形、字号、行距、颜色
   与对齐方式，并由共享解释器完整物化；
5. 2× 源/提议候选复核确认照片未变、标题锚点保持、签名与正文仍在各自槽位，未见新增
   painted text overlap；该 PDF 元数据明确标记为合同阻塞的结构提议，不是产品候选；
6. 超长固定 bundle 形成 `VISUAL_SLOT_OVERFLOW`，诊断模式仍在原 hard boundary 内完整
   物化译文、红色槽位框和“非产品候选”元数据；
7. run-private Catalog、默认停用 fallback、共享并发 1/2 等价、Ruff、Mypy、8 项专用
   测试和默认 Catalog 指纹均通过。

固定 bundle 只证明槽位、锚点、Patch 和物化接线，不证明语义翻译质量或盲测产品效果。
本轮没有调用真实模型。P12 历史结论仍为 `FAIL`：冻结 holdout 自动结果 `7/8`，人工只
接受 `3/8`；低对比度页和四个双语重复页仍是历史失败证据，本轮没有将其升级。

当前 blocker：

- `VISUAL-BACKGROUND-DIRECT-FACT-GAP`：Kernel 缺少槽位级背景颜色/对比度的类型化直接
  事实。只有在 Kernel/FAMILY 层补齐后，叶才能区分“忠实保持低可见性”和“经授权增强
  可读性”，不得静默替产品负责人选择；
- `VISUAL-BILINGUAL-SEMANTIC-IDENTITY-GAP`：`PageTextInventory` 能事前识别
  `ALREADY_TARGET_LANGUAGE`，但还不能证明分区、姓名/职务或跨距离段落是否表达同一个
  语义单元。结构候选因此只触发 fallback，不执行几何去重；
- `VISUAL-SHARED-MARGIN-OWNER-GAP`：页脚已归属共享 margin owner，但单 owner Patch
  的执行边界尚未接通；
- `VISUAL-MATERIALIZED-GLYPH-JUDGE-GAP`：叶内目标字体 probe 与本轮 2× 人工复核已
  通过代表页结构，但共享的最终 painted-glyph 对比度、碰撞和锚点漂移复裁尚未接通。

因此当前状态为 `CoreMigration=LIFTED_AND_WRAPPED`、
`EngineeringConformance=READY`、`ContractReadiness=BLOCKED`，默认 Catalog 继续
`KEEP_DISABLED`。候选、失败 PDF、PNG 和机器报告只保存在被 Git 忽略的
`runs/toolbox_leaf_migration/TBM1/11-visual-anchored-thin-gate-20260724-150600/`。

## 11. TBM1 批量原子叶闭环

计划 §4.2 的完成条件已经满足，闭环状态为
`COMPLETE_WITH_CONTRACT_BLOCKERS`：

- 7 个原子叶均完成 `Lift-and-Wrap`，来源映射合计 `144/144`；
- 7 个原子叶均为 `EngineeringConformance=READY`；
- `cover`、`contents` 为 `CONTRACT_READY`；
- `end`、`body.flow_text.multi`、`body.table`、`body.anchored_blocks`、
  `body.flow_text.visual_anchored` 均有逐 Route 明确 blocker，状态为
  `ContractReadiness=BLOCKED`；
- 格式化后的 7 叶联合回归为 `43 passed in 363.54s (0:06:03)`，Ruff、Mypy 通过；
- 默认 Catalog 仍为 17 条 Route，仅 `visual_only` 与 `body.flow_text.single`
  启用；文件 SHA-256 仍为
  `a43dccd10447943a8b3701265c9e85638a65a874d6876b8d1493d6f6886a2f8a`；
- 本阶段未调用真实模型，未运行双语整本 PDF，7 个原子叶的产品质量均为
  `NOT_EVALUATED`，晋级状态均为 `KEEP_DISABLED`。

提交追踪：

| 范围 | commit |
|---|---|
| TBM0 基线 | `c1ca25d2` |
| `cover` | `d46b82a2` |
| `contents` | `90a7662a` |
| `end` | `6a23180f` |
| `body.flow_text.multi` | `8ddc482f` |
| `body.table` | `5dc80a15` |
| `body.anchored_blocks` | `5b17a00c` |
| `body.flow_text.visual_anchored` | `c9323cea` |

批次机器闭环证据保存在被 Git 忽略的
`runs/toolbox_leaf_migration/TBM1/12-atomic-batch-closure-20260724-152406/`。
各叶候选/失败 PDF、PNG、JSON 和测试日志均未进入 Git。

这里的“TBM1 完成”只表示所有计划内原子叶分别达到工程符合且合同就绪或明确阻塞，
不表示跨类别集成、整本产品质量、Owner 接受或默认启用。按阶段边界，本轮不自动开始
TBM2。

## 12. 当前阶段真实模型直连接线复验

### 12.1 阶段决策

当前阶段以“真实翻译与 PDF 排版主链可用”为验收目标，采用已经可用的
`tests.migration.p9_qwen_translation_adapter.MigrationQwenTranslationAdapter`
直连 OpenAI-compatible 模型接口。该 Adapter 仍由共享 `TranslationPort` 注入
`ToolboxPageCoordinator`，不进入叶实现，不改变 Toolbox 生命周期、默认 Catalog
或最终 Patch 回放规则。

阶段状态固定为：

- `CurrentStageTranslationWiring=PASS`；
- `CurrentStageAdapter=MigrationQwenTranslationAdapter`；
- `ProductionAiCapabilityServiceWiring=DEFERRED_NON_BLOCKING`；
- `LiteLLMProviderEncapsulation=DEFERRED`。

这里的 `DEFERRED_NON_BLOCKING` 只表示生产 `HttpAiCapabilityAdapter` 与未来
LiteLLM/AI Capability Service 的最终接线不再阻断 TBM1；不表示删除或降级生产
Port 合同，也不把迁移 Adapter 晋级为最终生产实现。

### 12.2 前向真实链证据

本轮唯一结论权威为被 Git 忽略的前向运行：

`runs/toolbox_leaf_migration/TBM1/17-stage-direct-qwen-20260724-160426/`

该运行没有复用上一轮 Checkpoint，完整执行：

```text
source PDF
→ Preservation preflight
→ PageFacts
→ 真实模型分类主判/复核
→ run-private Catalog
→ ToolboxPageCoordinator
→ PageTextInventory / SemanticUnitMap
→ 真实模型 TranslationPort
→ TranslationCompleteness
→ Cover Layout / Judge / Repair
→ PagePatchInterpreter
→ page Checkpoint
→ DocumentFinalizer 串行回放
→ Preservation
→ immutable final PDF
```

机器结果为：

- 总结论 `PASS`；
- 真实 HTTP 共 4 次：分类 2 次、翻译 2 次；
- 严格分类链 Route 为 `cover`，已知 Route 对照链独立执行；
- 两条链均为 5 个翻译单元 `FULL`、5 个批准 Patch、`fallback=NONE`；
- 两条链均生成不同于源文件的最终 PDF，Preservation 均通过；
- 默认 Catalog 前后 SHA-256 均为
  `a43dccd10447943a8b3701265c9e85638a65a874d6876b8d1493d6f6886a2f8a`；
- 两份最终 PDF 独立重渲染结果逐像素一致，声明操作矩形之外像素变化率为 `0.0`；
- 人工栅格复核未见裁切、文字互压或不可读字形；
- 凭据仅经进程环境注入，运行证据中的密钥匹配数为 `0`。

准备阶段的 `14-*`、`15-*` 目录均在真实 HTTP 前因一次性验证脚本目录前置条件停止；
`16-*` 已完成严格业务链及 3 次真实 HTTP，但在链后汇总复制阶段停止。三者均不作为
最终结论证据，也未被删除或改写。

### 12.3 验收边界

本轮证明的是当前阶段真实翻译、Toolbox 排版、Patch 回放和最终化链路已经打通，
不升级为整本产品质量或最终生产接线完成。仍保留以下问题：

- 必保留字面量恢复会把缺失的 `RMB/HKD` 机械追加到译文，合同完整但措辞可能重复；
- 最终 PDF 文本抽取仍可能把 `年` 映射为兼容字形 `U+F98E`；
- 受控 CJK 字体重复嵌入使该单页最终 PDF 约为 16.7 MB；
- 当前环境没有 Poppler，本轮最终 PDF 独立重渲染使用 PyMuPDF，未形成跨渲染器证据。

这些问题继续进入后续共享翻译质量、字体与产品验收阶段，不阻断
`CurrentStageTranslationWiring=PASS`。PDF、PNG、JSON、日志和模型结果仍只保存在
被 Git 忽略的 `runs/`，不得上传 GitHub。
