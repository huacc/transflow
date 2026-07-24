# Transflow Toolbox 批量核心迁移与分层集成验收计划 v0.2

| 项 | 内容 |
|---|---|
| 文档状态 | 新执行基线；用于接替 v0.1 尚未完成的 Toolbox 工作 |
| 编制时间 | 2026-07-24 09:39:10（Asia/Shanghai） |
| 仓库根目录 | `D:\项目\开源项目\translation\transflow` |
| 迁移来源 | `spikes/page_toolbox_engine_puncture_v1/toolboxes` |
| 分类样本来源 | `spikes/page_classification_engine_puncture_v1/分类结果` |
| 生产目标 | `src/transflow/toolboxes` 与现有 `DocumentCoordinator` 主链 |
| 运行产物根目录 | `runs/toolbox_leaf_migration` |
| 接手背景 | `docs/背景/Transflow_Toolbox批量迁移与集成验收_会话接手背景_20260724.md` |
| 被调整计划 | `docs/计划/Transflow_Toolbox逐类别核心逻辑迁移与全流程验收计划_v0.1.md` |

---

## 0. 本计划的权威边界

### 0.1 v0.2 改什么

本计划调整的是 **Toolbox 剩余迁移工作的执行节奏、测试分层和人工停点**：

- 从“每迁移一个叶，立即完成视觉调优、双语整本 PDF、完整 Gate 和人工验收”
  改为“先批量完成核心迁移和薄契约门禁，再进行跨类别集中调试，最后在里程碑运行双语整本 PDF”；
- 允许所有相互独立的叶先推进到 `CONTRACT_READY`，不再要求每个叶等待负责人看完完整 PDF 后才迁移下一叶；
- 把共享问题按 `GLOBAL / FAMILY / LEAF` 分层，固定进入问题账本与回归集，不再依赖会话记忆；
- 把完整中英文年报从“逐叶开发 Gate”后移为“集成基线和发布候选 Gate”。

### 0.2 v0.2 不改什么

以下设计不变量继续有效：

1. 生产代码只有一份，位于 `src/transflow`；Spike 是迁移来源和历史证据，不是第二套生产运行时。
2. 采用 Lift-and-Wrap：优先迁移既有 Toolbox 主体并适配统一合同，不从零重写已穿刺能力。
3. 完整 PDF 继续走既有生产主链：

   ```text
   完整 PDF
   → PageFacts
   → ClassificationEngine
   → 唯一 Route / 显式 Catalog
   → 唯一 PageToolbox
   → TranslationCompletenessDecision
   → Layout / Judge / Repair
   → PageOutcome
   → DocumentFinalizer 从源 PDF 副本串行回放批准 Patch
   → Preservation / 最终 Artifact
   ```

4. 默认 Catalog 未经最终证据和负责人处置不得启用新叶。
5. 图片、drawing、连接线、表格线、背景、链接等保护对象的边界不因提速而放宽。
6. 历史 run、报告和 Gate 保持只读；新验证只产生新的前向产物。
7. 技术运行通过、可打开 PDF、run-private Catalog 授权和产品人工接受是不同结论，不得相互替代。

### 0.3 与 v0.1 的关系

- v0.1 继续作为 TM1～TM4 历史执行过程、逐叶来源要求和原始 Gate 设计的证据；
- 从本计划开始，不再按 v0.1 的“单叶完整 PDF + 人工硬停”推进剩余叶；
- v0.1 下已经产生的失败、通过、授权和人工处置不重写；
- 若 v0.1 与本计划在后续执行节奏上冲突，以本计划为准；
- 若两者在生产主链、保护对象、Catalog 晋升纪律或历史证据只读上冲突，以总体设计和更严格的真实安全合同为准。

---

## 1. 为什么现在必须改变节奏

### 1.1 原串行策略的合理性

v0.1 采用逐叶完整闭环，是为了隔离以下变量：

- 分类是否正确；
- 译文是否真实、完整；
- 目标 Toolbox 是否真正命中；
- 差异来自迁移适配还是模型随机性；
- Layout、Judge、Repair 哪一层与 Spike 不一致；
- 最终 PDF 是真实译后结果还是安全透传；
- 某叶失败是否被其他 Route 掩盖。

在主链、分类、翻译完整性、PageToolbox 合同和 Finalizer 尚未稳定时，该策略有价值。

### 1.2 实际执行暴露的新问题

当前事实已经变化：

- `DocumentCoordinator`、`ClassificationEngine`、显式 Catalog、PageToolbox、
  TranslationCompleteness、DocumentLayoutMemory、Repair、Patch 回放和 Preservation
  已形成一条生产主链；
- 默认 Catalog 有 17 条 Route，但当前仍只有 `visual_only` 和
  `body.flow_text.single` 两条默认启用；
- `body.chart` 已累计 24 个 TM3 轮次；
- `body.diagram` 已累计 27 个 TM4 轮次；
- 大量轮次不是在“迁移新核心”，而是在反复修正跨类别问题：
  锚点、目标语言宽度、字号和行距、空白利用、Judge 边界、连接线新增碰撞、
  失败 PDF、翻译异常映射和跨页样式一致性。

这说明当前主要成本已经从“写叶子核心”转成“共享规则和产品效果调试”。
继续在每个叶后重复运行数百页双语 PDF，会让同一个全局问题在不同 TM 阶段被反复发现、
反复修复，也会诱发为了当前类别局部通过而修改共享逻辑。

### 1.3 新策略的直接目标

本计划要解决的不是“少测一些”，而是 **让不同测试在正确的时机发生**：

1. 迁移阶段快速证明来源、合同、运行可达和安全失败；
2. 集成阶段一次性暴露跨类别相互作用；
3. 调试阶段按影响范围选择回归集；
4. 完整年报只在系统里程碑验证产品主链；
5. 每个用户反馈进入持久问题账本和固定回归页，避免下一阶段重复犯错。

---

## 2. 当前基线

### 2.1 已经形成的生产能力

- 完整 PDF 枚举、PageFacts 提取和页面身份保持；
- ClassificationEngine 和显式 Route；
- ToolboxCatalog、PageToolbox 合同和确定性 fallback；
- PageTextInventory、SemanticUnitMap 和翻译完整性裁决；
- DocumentLayoutMemory 和页级 RepairMemory；
- Layout、Judge、Repair、PagePatch 与诊断候选；
- DocumentFinalizer 从源 PDF 副本回放批准 Patch；
- 页数、页序、页面矩形和 Preservation 校验；
- 分类并发帮助入口和 Toolbox 页级并发帮助入口。

这些能力是后续迁移必须复用的生产框架，不是每个 Toolbox 可以自行替代的参考实现：

| 工程化维度 | 现有统一所有者 | 迁移约束 |
|---|---|---|
| 完整文档编排 | `DocumentCoordinator` | Toolbox 只处理页级类别逻辑，不自行枚举整本 PDF、不另建文档主循环 |
| 分类与 Route | `ClassificationEngine`、显式 Catalog | 不在叶内重复分类；每页只有一个 Route 和一个确定的 Catalog 解析结果 |
| 翻译调度 | `ToolboxPageCoordinator`、`TranslationPort` | Toolbox 只构造/消费 TranslationBundle，不直接调用模型服务或自建重试循环 |
| 页级生命周期 | `PageToolbox` 合同、factory | 必须按统一 `prepare` / `build_translation_request` / `consume_translation_bundle` / `render` / `judge` / `repair` 六阶段接入，不复制 Spike runner 作为生产入口 |
| 并发与归并 | `DocumentCoordinator.classify_pages()`、`ToolboxPageCoordinator.execute_many()` | Toolbox 不创建线程池；只保证页级可重入、无跨页可变状态，并由共享入口按 `page_no` 确定归并 |
| 启停与失败隔离 | `ToolboxCatalog`、`CatalogResolution` | 启用时进入唯一叶；停用、版本不符或初始化失败时进入确定性 `PAGE_PASSTHROUGH` |
| Patch 与最终化 | `PagePatch`、`DocumentFinalizer` | 叶只产出受约束候选；最终批准 Patch 从源 PDF 副本串行回放 |
| 证据与保护 | 统一 Artifact、Finding、Preservation | 成功和失败都进入同一证据结构，不另造只供某一类别使用的结果格式 |

因此这里的“迁移”是“迁移类别专用核心，并用现有生产合同包裹”，不是把 Spike 的
runner、线程模型、模型调用、目录结构和 Gate 原样搬入 `src/transflow`。

并发现状也必须如实记录：虽然上述两个受控并发帮助入口已经存在，当前集成
`DocumentCoordinator.run_classified()` 仍通过 `scan_classified_pages()` 串行分类，
`_execute_pages()` 也仍串行处理页面；传入的 `page_concurrency` 尚未贯通这两段。
最终 Patch 回放串行则是有意设计。TBM 需要在共享主链中只接通一次有界页级并发，
并保持页面身份、顺序归并、内存上限和终态屏障；不得让各 Toolbox 分别实现并发。

### 2.2 已迁移或已建立专用生产目录的叶

| Route | 当前事实 | 默认 Catalog |
|---|---|---|
| `visual_only` | 已有生产透传实现，无独立 Spike 专用核心 | enabled |
| `body.flow_text.single` | 已有专用生产目录；TM2 已由负责人限定范围接受 | enabled |
| `body.chart` | 已有 `body_chart` 专用生产目录和大量回归产物 | disabled |
| `body.diagram` | 已有 `body_diagram` 专用生产目录和大量回归产物 | disabled |

### 2.3 尚未完成专用核心迁移的叶

以下 Route 仍以普通骨架、轻量实现、disabled fallback 或 pending 为主：

1. `cover`
2. `contents`
3. `end`
4. `body.flow_text.multi`
5. `body.flow_text.visual_anchored`
6. `body.table`
7. `body.anchored_blocks`
8. `body.composite.flow_text_table`
9. `body.composite.chart_table`
10. `body.composite.flow_text_chart`
11. `body.composite.flow_text_diagram`
12. `body.composite.anchored_blocks_chart`
13. `body.freeform`

其中 `body.freeform` 没有可直接 Lift-and-Wrap 的 Spike Toolbox，只能在已知叶迁移完成后做
有界新增，不得写成“Spike 核心迁移完成”。

### 2.4 当前运行证据边界

- TM2 的正式负责人处置：
  `runs/toolbox_leaf_migration/TM2/20-owner-disposition-20260723-141316/`。
- TM3 最新 30 页 replay：
  `runs/toolbox_leaf_migration/TM3/24-body-chart-30-page-replay-regression-20260723-222429/`。
  运行本身完成，30 页均有 PDF 和 review；18 页 PASS、12 页 FAIL，
  Manifest 的整体 `product_acceptance=false`。
- TM4 最新 30 页回归：
  `runs/toolbox_leaf_migration/TM4/26-body-diagram-30-page-final-regression-20260724-054344/`。
  29 页 PASS、1 页 FAIL，30 页均有 PDF；结论为
  `PASS_DISABLED_WITH_FALLBACK`，默认 Catalog 未修改。
- TM4 英文完整年报：
  `runs/toolbox_leaf_migration/TM4/27-01528-en-full-20260724-0812/`。
  322 页最终 PDF 可打开，页数、页序、页面矩形和 Preservation 通过；
  但授权叶 119 页中 0 页通过，116 次 `AI_RESPONSE_INVALID`、2 次 `AI_TIMEOUT`、
  1 次 `ROUTE_CAPABILITY_MISMATCH`，所有页面进入 `PAGE_PASSTHROUGH`。
  该产物只能证明结构收敛和失败留证，不能证明翻译排版产品通过。
- 对应中文完整年报尚未形成同等级运行产物。
- 当前工作树已修改 Provider 连接异常映射等逻辑，但没有新的整本 PDF 证明修改后的真实效果。

---

## 3. 新状态模型：迁移、工程接入、合同、产品和晋升分开

每个 Route 必须分别记录五个结论轴：

| 结论轴 | 可选值 | 说明 |
|---|---|---|
| `CoreMigration` | `COMPLETE / BLOCKED / NOT_APPLICABLE` | Spike 运行可达核心是否完成来源映射并进入生产代码 |
| `EngineeringConformance` | `READY / FAIL` | 是否复用统一主链、PageToolbox 生命周期、共享并发入口、Catalog 启停、fallback 和 Artifact；是否没有叶私有 runner/线程池/模型调用 |
| `ContractReadiness` | `READY / FAIL` | 能否导入、注册、执行、生成候选/失败 PDF，并遵守 owner/protected/fallback 合同 |
| `IntegratedProductQuality` | `PASS / FAIL / NOT_EVALUATED` | 放入跨类别运行后，翻译排版和相互作用是否可接受 |
| `PromotionEligibility` | `ENABLE / KEEP_DISABLED / BLOCKED` | 是否允许修改默认 Catalog |

状态机调整为：

```text
NOT_STARTED
  → SOURCE_MAPPED
  → CORE_MIGRATED
  → ENGINEERING_CONFORMANT
  → CONTRACT_READY
  → INTEGRATED
  → SYSTEM_REGRESSION_PASS
  → OWNER_REVIEW
  → ENABLED 或 DISABLED_WITH_EVIDENCE
```

关键变化：

- 所有相互独立的叶可以连续推进到 `CONTRACT_READY`；
- `CORE_MIGRATED` 只表示核心代码已进入生产目录；绕开统一 Coordinator、并发调度、
  Catalog 启停或 Artifact 合同的直接移植，必须停在 `EngineeringConformance=FAIL`；
- 主链工程化缺口只在共享模块修复一次，所有叶通过同一合同受益，禁止在叶内复制补丁；
- `CONTRACT_READY` 不是产品 PASS，也不允许默认启用；
- 某个独立叶薄门禁失败只阻塞该叶及依赖它的 composite，不阻塞其他独立叶迁移；
- 跨类别产品调试和负责人复核集中发生在后半段。

---

## 4. 执行阶段

新的阶段编号使用 `TBM`，避免把新节奏误写成 v0.1 的 TM5 延续。

| 阶段 | 工作 | 完成条件 |
|---|---|---|
| TBM0 | 冻结当前工作树事实、Catalog、源映射和问题账本 | 形成可追溯基线，不改默认 Catalog |
| TBM1 | 批量迁移剩余原子叶 | 原子叶分别达到 `ENGINEERING_CONFORMANT`、`CONTRACT_READY` 或明确 `BLOCKED` |
| TBM2 | 批量迁移 composite，并有界处理 freeform | composite 分别达到 `ENGINEERING_CONFORMANT`、`CONTRACT_READY` 或明确 `BLOCKED` |
| TBM3 | 建立全叶 run-private Catalog 和跨类别分层页池 | 所有 ready 叶进入同一主链，共享并发、启停、失败隔离和问题归属通过 |
| TBM4 | GLOBAL/FAMILY/LEAF 集中调试 | 固定问题回归集稳定，未关闭项有明确处置 |
| TBM5 | 双语整本集成基线 | 两份完整年报各生成一次真实基线及完整失败证据 |
| TBM6 | 发布候选回归、Owner 处置和默认 Catalog 冻结 | 两份完整年报发布候选、最终 Route 矩阵和 Catalog 决策 |

### 4.1 TBM0：只建立一次的基线

必须输出：

```text
runs/toolbox_leaf_migration/TBM0/<round>-baseline-<timestamp>/
├─ input/
│  ├─ current_git_status.txt
│  ├─ default_catalog.json
│  ├─ source_roots.json
│  └─ historical_evidence_index.json
├─ process/
│  ├─ route_migration_matrix.json
│  ├─ shared_issue_ledger.json
│  └─ regression_pool_plan.json
└─ report.md
```

要求：

1. 保留当前脏工作树，不 reset、不 clean、不删除历史 runs。
2. 逐 Route 建立 Spike→Transflow 来源矩阵。
3. 把 TM2/TM3/TM4 已知问题页登记为固定回归页。
4. 记录默认 Catalog 17 条 Route、当前 enabled 状态和 hash。
5. 不运行两份完整年报。

### 4.2 TBM1：批量迁移原子叶

建议顺序：

```text
cover
→ contents
→ end
→ body.flow_text.multi
→ body.table
→ body.anchored_blocks
→ body.flow_text.visual_anchored
```

顺序用于减少依赖混乱，不再附带逐叶人工硬停。每个叶仍独立保存来源、代码、测试和状态。

对每个叶只执行薄门禁：

1. Spike 运行可达核心资产映射覆盖率 `100%`，或每个未迁资产都有明确理由。
2. 类别专用核心经生产 `PageToolbox` 生命周期包裹；生产模块可导入，factory 可构建，
   Route 与 descriptor 唯一一致。
3. `src/transflow` 不导入 `spikes/`、`tests/` 或历史 `runs/`。
4. 叶不直接调用 Translation Provider、不自行重试、不自行枚举整本 PDF、不创建线程池，
   只通过共享 Coordinator 和 TranslationPort 工作。
5. run-private Catalog 中启用该 Route 时进入唯一 Toolbox；停用时进入确定性 fallback，
   且两种结果都有机器可读 trace。
6. 通过共享页级并发入口执行至少两个页面；`page_concurrency=1` 与 `>1` 的页面身份、
   Route、Patch owner、结果集合和按页归并一致，不发生跨页状态污染。
7. 至少一个结构代表页可生成实际候选 PDF。
8. 至少一个故障输入可生成可查看的失败/诊断 PDF 和机器可读 Finding。
9. owner、protected object、Patch binding 和 fallback 合同通过。
10. 默认 Catalog 不变，只使用 run-private overlay。
11. 不要求本阶段完成双语整本 PDF、全面视觉调优或 Owner 产品接受。

第 6 项验证的是叶对共享并发范式的兼容性，不代表完整文档主链已经并发化。
若 `run_classified()` 的并发参数尚未贯通，应登记为一个
`SHARED-ENGINEERING-CONCURRENCY` 主链问题，在 TBM3 集中接通；不得改成每个叶各自开线程。

常规布局回归优先复用经过记录的真实 TranslationBundle，避免模型随机性和网络开销。
真实模型只用于证明翻译合同接线和后续集成产品效果，不要求每个薄门禁重复调用。

### 4.3 TBM2：批量迁移 composite 和 freeform

依赖：

| Composite | 必须先 ready 的原子能力 |
|---|---|
| `body.composite.flow_text_table` | single、table |
| `body.composite.chart_table` | chart、table |
| `body.composite.flow_text_chart` | single、chart |
| `body.composite.flow_text_diagram` | single、diagram |
| `body.composite.anchored_blocks_chart` | anchored_blocks、chart |

Composite 必须迁移自己的根级 owner、执行顺序、Patch merge、Judge、Repair 和 fallback，
不得在运行时把两个原子 Toolbox 临时拼接成组合 Toolbox。

Composite 仍必须遵循与原子叶完全相同的工程范式：

- 通过统一 factory 和 run-private Catalog 构造，不提供第二套组合页 runner；
- 只由 `ToolboxPageCoordinator` 调度翻译，不由子叶或 composite 并发调用模型；
- 页间并发只由共享 `page_concurrency` 控制；组合内部执行顺序和 Patch merge 必须确定，
  不得依赖 future 完成顺序；
- 启用/停用测试必须覆盖整个 composite 的唯一解析和确定性 fallback；
- 任一原子能力失败时按 composite 根 owner 收敛并留证，不把部分 Patch 越权提交给 Finalizer。

`body.freeform` 最后处理：

- 只允许有界区域分解和固定 allow-list；
- 不递归分类、不让模型动态选工具、不借用未通过合同的叶；
- 没有自然真实样本时保持 `IntegratedProductQuality=NOT_EVALUATED`；
- 不得阻塞其他已知叶进入 TBM3。

执行记录（2026-07-24）：

- `body.composite.flow_text_chart`、`body.composite.flow_text_diagram` 和
  `body.freeform` 已完成保守迁移并达到 `EngineeringConformance=READY`、
  `ContractReadiness=READY`；
- 依赖 `body.table` 或 `body.anchored_blocks` 的三个 composite 保持明确阻塞，
  没有提前建立 factory；
- 默认 Catalog 未修改，产品质量均未升级，详见
  `docs/迁移/Transflow_TBM2组合叶与Freeform受控穿刺状态_20260724.md` 和
  `resources/manifests/toolbox_batch_migration/tbm2_gate.json`。

### 4.4 TBM3：全叶集成

建立一个 run-private Catalog：

- 纳入全部 `CONTRACT_READY` 叶；
- 对 `BLOCKED` 叶保留原确定性 fallback；
- 每条 Route 只允许一个 Toolbox；
- 每页 trace 必须记录实际 Route、toolbox key/version/fingerprint；
- 默认 Catalog 仍不修改。

这里的“启停”是按一次运行冻结的 Catalog/config snapshot 控制，不等同于进程内热切换。
TBM3 必须建立启停矩阵：每个 ready Route 在 enabled 时实际命中其唯一 Toolbox，在
disabled 时稳定返回该 Route 的 `PAGE_PASSTHROUGH`，且不影响同一运行中其他 enabled Route。

TBM3 同时完成一次共享工程主链验收：

1. `run_classified()` 的 `page_concurrency` 必须实际控制分类与页级 Toolbox 执行，而不只是
   存在未接通的 helper 或命令行参数。
2. 并发必须是有界的；不能为追求吞吐一次性长期持有整本页面 PNG 或绕过
   `DocumentLayoutMemory` 的只读冻结边界。
3. 同一分层页池分别以 `page_concurrency=1` 和 `>1` 运行，页面身份、页序、Route、
   toolbox 指纹、Translation unit、Patch binding、Finding 集合和最终 PDF 页序一致。
4. 单页分类、翻译或 Toolbox 失败只使该页进入受控 fallback；其他 future 继续收敛，
   终态屏障后再进入 Finalizer。
5. Finalizer 继续从源 PDF 副本串行按页回放批准 Patch；禁止并发写同一个最终 PDF。
6. 所有叶共用同一种 Artifact/trace schema，能够从最终页反查分类、Catalog 解析、
   翻译、Judge/Repair、Patch 或 fallback。

建立跨类别分层页池：

1. 从 `spikes/page_classification_engine_puncture_v1/分类结果` 获取正确类别和单页 PDF；
2. 从各 Spike Toolbox runs 选择结构代表页；
3. 纳入 TM2/TM3/TM4 已知问题页和负责人点名页；
4. 每个可翻译 Route 在资产允许时至少包含：
   - 一个中文源页；
   - 一个英文源页；
   - 一个长度扩张或复杂结构页；
   - 一个非目标/失败哨兵；
5. 不追求“每个类别都固定 30 页”；覆盖由结构、语言方向和已知问题决定。

TBM3 验证的是：

- 所有 ready 叶能在同一 Catalog、同一协调器和同一 Artifact 体系中共存；
- 所有 ready 叶遵循同一启停、共享并发、顺序归并和失败隔离范式；
- TM2、TM3、TM4 已有改动没有因新叶接入失效；
- 失败能定位到 GLOBAL、FAMILY 或 LEAF，不被汇总 Gate 掩盖。

### 4.5 TBM4：集中调试

所有问题必须进入 `shared_issue_ledger.json`，至少包含：

```text
issue_id
symptom
source_run
source_page
scope: GLOBAL | FAMILY | LEAF
owner_module
rule_or_code_location
positive_regressions
negative_sentinels
status
evidence
owner_disposition
```

作用域纪律：

- `GLOBAL`：真正跨类别且不包含 PageType 私有语义的合同；
- `FAMILY`：正文流、网格/表格、图表/流程图/锚定块、复合页等类别族共享；
- `LEAF`：owner 识别、局部几何、私有 Judge/Repair 和类别独有结构。

修改规则：

1. GLOBAL 修改必须跑跨类别分层页池。
2. FAMILY 修改必须跑该 family 正向集和其他 family 哨兵。
3. LEAF 修改只跑该叶正向集和必要非目标哨兵。
4. 不能为样本 ID、文件名、页码、公司名、已知标题或绝对坐标写分支。
5. 为一个叶修改共享规则前，必须证明问题确实跨类别。
6. 如果穿刺能力本身不足，可以修改叶实现；先保留主体，修改点必须写入来源差异表。

### 4.6 TBM5：双语整本集成基线

使用 TBM3 的同一 run-private Catalog 分别运行两份完整 01528 年报。
本阶段的目标是发现分层页池没有覆盖的跨页、长文档、Route 分布和 Provider 问题，
不是要求第一次整本运行即全部产品通过。

要求：

- 两份 PDF 均从完整源文件进入生产主链；
- 保存每页 Route、Toolbox、翻译、fallback、Patch 和最终化索引；
- 成功页和失败页均有可查看 Artifact；
- 所有新问题回灌 `shared_issue_ledger.json`；
- 不直接修改默认 Catalog；
- TBM5 结束后只对新增问题运行 L1/L2/L3 定向回归，不反复整本试错。

### 4.7 TBM6：发布候选与统一处置

先关闭或明确接受 TBM5 回灌的问题，再运行 L3 跨类别页池。
只有 L3 稳定后，才第二次运行两份完整年报作为发布候选。

TBM6 必须输出：

- 两份发布候选 PDF 及 Preservation 证据；
- 17 条 Route 的四轴状态矩阵；
- 未通过 Route 的诊断 PDF 和 disabled fallback；
- 默认 Catalog 变更候选及 hash；
- 负责人对每条 Route 的 `ENABLE / KEEP_DISABLED` 处置；
- 最终问题账本和证据索引。

---

## 5. 必须固化的共享经验

### 5.1 类型化语义锚点

锚点选择顺序固定为：

```text
owner
→ 语义角色
→ 源对齐证据
→ LEFT / RIGHT / CENTER / owner-relative
→ 安全展开区域
→ 真实 glyph 测量
→ 锚点门禁
```

- 正文通常保持所属阅读流左锚；
- 数值列通常保持右锚或 cell 对齐；
- 图片、表格、图表、卡片标题只有源页证据支持时才保持中心轴；
- 角色名是 `TITLE` 不等于无条件居中；
- 纵向位置可依据目标语言长度、同一阅读流和真实空白重排；
- 横向达到所属栏、cell、节点或 owner 边界后才自然换行；
- 源字形 bbox 是锚点和首轮槽位证据，不是目标语言宽度牢笼。

权威经验：
`docs/经验/Transflow_跨类别文本锚点选择与保持经验_20260723.md`。

### 5.2 字体、行距和空白

- 同一正文 cohort 的字号和行距应保持一致；
- 标题/正文/注释的字体层级应保持源页比例关系；
- 纵向空白充足时可适度增加行距或段距；
- 允许使用相对源值约 ±10% 或固定小步长搜索字号、行距、段距；
- 不允许某一段因局部 fit 被无界缩小，与同 cohort 正文明显不一致；
- 调整必须以真实 glyph、owner 空间和相邻障碍为依据。

### 5.3 中译英长度扩张

- 横向存在安全空间时，不受原字形窄 bbox 强制约束；
- `LEFT` 向右扩、`RIGHT` 向左扩、`CENTER` 围绕中心轴扩；
- 达到真实 owner/栏/cell/节点边界后再换行；
- 不能因为翻译变长就改变表格业务行、图表数据对象或 diagram 节点归属。

### 5.4 Judge 不得比类别真实合同更严

必须区分：

```text
source_glyph_bbox
layout_search_region
hard_legal_boundary
```

- 优选安全框不是自动的硬违法边界；
- diagram 节点内文字使用原始节点边界作为已验证硬合同；
- 连接线碰撞比较候选相对源页是否新增；
- 节点内文字不参与“节点外局部标签新增连接线碰撞”门禁；
- 新 Gate 若比 Spike 已验证合同更严，必须有跨样本结构证据，否则收回。

### 5.5 失败 PDF 必须保留

不可交付不等于不产出：

- 每个失败页都必须保留实际译后/排版候选 PDF；
- 同时保留 source、Finding、Patch、Judge、Repair 和最终 fallback；
- 安全交付可以回退到源页，但诊断候选不能被删除；
- 只有这样负责人才能判断是能力失败还是门禁过紧。

### 5.6 翻译质量边界

本迁移计划的重点是“翻译结果是否进入正确排版和回填链”，不是替代上游翻译服务做高强度语言审校。

翻译 Gate 应拒绝：

- 未翻译、空串、占位、身份集合漂移；
- 明显原文整段照抄；
- required literal 丢失；
- Provider/Schema/合同错误。

只要不是明显未翻译且合同有效，不因措辞风格或轻微语义质量问题阻塞 Toolbox 排版迁移。
语言质量由专门翻译服务另行治理。

---

## 6. 分层测试矩阵

| 层级 | 触发时机 | 输入 | 是否真实模型 | 是否整本 PDF | 目标 |
|---|---|---|---|---|
| L0 静态/合同 | 每个叶迁移后 | 代码、来源清单、构造合同 | 否 | 否 | 导入、注册、owner、protected、fallback |
| L1 叶冒烟 | 当前叶代码变化 | 少量代表页和失败页 | 记录回放为主 | 否 | 当前叶可执行并产出 PDF |
| L2 Family 回归 | family 规则变化 | 对应 family 页池 | 记录回放为主 | 否 | 同族一致、异族哨兵不退化 |
| L3 跨类别集成 | GLOBAL 规则变化或批次结束 | 全 Route 分层页池 | 记录回放 + 少量真实调用 | 否 | 全局规则和主链相互作用 |
| L4 里程碑整本 | TBM5、TBM6 或批准的关键主链变化 | 两份 01528 完整年报 | 是 | 是 | 产品主链、跨类别质量、Preservation |

不得把 L0/L1 结果写成产品 PASS；也不得用 L4 的安全透传掩盖没有真实译后页面。

工程化验收至少还要满足：

- L0：factory/Catalog 唯一注册、enabled/disabled 双分支和确定性 fallback；
- L1：同一小页集的 `page_concurrency=1`/`>1` 等价性和跨页状态隔离；
- L3：`run_classified()` 实际贯通受控并发、异常 future 隔离、按页归并和统一 Artifact；
- L4：并发页处理结束后仍由 Finalizer 串行回放，Preservation 不因调度顺序改变。

### 6.1 完整 PDF 的固定里程碑

完整年报固定为：

```text
样本/年报/01528_RS MACALLINE_英文_2025.pdf
样本/年报/01528_紅星美凱龍_中文_2025.pdf
```

只在以下时点强制各运行一次：

1. **TBM5 集成基线**：全部可迁叶达到 `CONTRACT_READY` 并接入 run-private Catalog 后；
2. **TBM6 发布候选**：GLOBAL/FAMILY 集中调试稳定、问题账本达到发布条件后。

额外整本回归必须有明确触发理由，例如：

- DocumentCoordinator、Catalog 解析、Finalizer、Patch Interpreter、Preservation 等关键主链变化；
- 默认 Catalog 候选集合变化；
- 负责人明确要求新的整本效果验证。

单个叶、单个门禁或单页视觉问题默认使用 L1/L2/L3，不立即重跑两份整本年报。

---

## 7. 运行目录与轮次

所有新产物继续写入：

```text
runs/toolbox_leaf_migration/<TBMx>/<round>-<purpose>-<timestamp>/
```

每个正式 run 至少具有：

```text
input/
output/
process/
review/
run_manifest.json
report.md
```

页池 run：

```text
cases/<index>-<sample_id>/
├─ input/source.pdf
├─ output/transflow.pdf
├─ process/case_manifest.json
└─ review/source_vs_transflow.png
```

要求：

- 轮号单调增加；
- 不覆盖、不重命名历史 run；
- 成功和失败都生成 Manifest；
- 失败 case 仍必须有 `output/transflow.pdf`，并通过 artifact mode 区分产品候选、
  译后诊断候选和源页 fallback；
- 报告不得写入 API Key、Authorization 或完整未脱敏 Provider 原始响应。

---

## 8. Catalog 与晋升

### 8.1 迁移期

- 只使用 run-private Catalog；
- 默认 Catalog 保持当前状态；
- 单叶 `CONTRACT_READY` 不产生默认启用资格；
- blocked 叶继续确定性 fallback，不切换其他叶。

### 8.2 集成期

形成统一 Route 决策矩阵：

| Route | CoreMigration | ContractReadiness | IntegratedProductQuality | PromotionEligibility | Evidence |
|---|---|---|---|---|---|

负责人不需要在每个叶迁移后停下，而是在 TBM5/TBM6 查看：

- 跨类别页池；
- 两份整本 PDF；
- 每个 Route 的通过/失败和诊断 PDF；
- 未关闭问题和 disabled fallback；
- Catalog 变更候选。

负责人可以按 Route 选择 `ENABLE` 或 `KEEP_DISABLED`。未明确处置的 Route 默认 disabled。

---

## 9. 停止条件

### 9.1 只阻塞当前叶

- 来源映射不完整；
- factory/Route/descriptor 漂移；
- owner 或 protected 合同失败；
- 当前叶无法生成实际候选或失败 PDF；
- 当前叶依赖尚未 ready；
- 当前 Spike 能力本身不足且没有批准的改动方案。

记录 `BLOCKED` 后，可继续迁移其他独立叶。

### 9.2 阻塞整个批次

- 生产代码开始运行时导入 Spike、tests 或历史 runs；
- 修改、删除或覆盖历史证据；
- 默认 Catalog 在未授权时被修改；
- Patch 可修改保护对象或越过明确硬边界；
- Finalizer 页数、页序、页面矩形或 Preservation 破坏；
- Secret 写入代码、配置、日志、报告或 Artifact；
- 共享规则出现样本 ID、文件名、页码、公司名或绝对坐标分支；
- 主链修改导致所有叶无法确定性失败和收敛。

### 9.3 不构成批次停止

- Provider 暂时不可达；
- 某个 TranslationBundle 语言质量一般但不是未翻译/无效；
- 单个叶产品视觉失败；
- 某个叶保持 disabled；
- 某个完整 PDF 以降级终态完成。

这些情况必须留证，但不阻止机械核心迁移继续推进。

---

## 10. 完成定义

只有同时满足以下条件，本计划才算完成：

1. 所有有 Spike 核心的 Route 都达到 `CoreMigration=COMPLETE`，或有负责人接受的明确阻塞记录；
2. `src/transflow` 对 Spike/tests/runs 的运行时依赖为 `0`；
3. 所有已迁叶均达到 `EngineeringConformance=READY`，没有叶私有整本 runner、线程池、
   Translation Provider 调用或 Finalizer；
4. 所有已迁叶都有来源映射、薄合同测试、候选 PDF 和失败 PDF；
5. 所有 `CONTRACT_READY` 叶进入同一个 run-private Catalog 和同一生产主链；
6. Catalog enabled/disabled 双分支、串并发等价、失败隔离、按页归并和串行最终化通过；
7. GLOBAL/FAMILY/LEAF 问题账本完整，用户指出的问题全部有固定回归页；
8. 跨类别分层页池通过，或失败 Route 有明确 disabled 处置；
9. 两份完整 01528 年报都产生 TBM6 发布候选运行、最终 PDF 和 Preservation 证据；
10. TM2/TM3/TM4 已有能力在最终集成回归中没有无解释退化；
11. 每条 Route 获得 `ENABLE` 或 `KEEP_DISABLED` 的负责人处置；
12. 默认 Catalog、实现指纹、问题账本和最终证据索引被冻结。

在上述条件满足前，只能说：

```text
核心迁移进行中
或
批量核心迁移完成、集成产品验收进行中
```

不能说“所有 Toolbox 已经产品通过”。

---

## 11. 新会话的起步顺序

新会话不得直接继续 TM4 第 28 轮，也不得先重跑两份完整年报。固定顺序为：

1. 阅读本计划和对应接手背景；
2. 查看当前 `git status`，保护 2026-07-24 的脏工作树；
3. 核对默认 Catalog、三条专用叶目录和 TM2/TM3/TM4 证据；
4. 建立 TBM0 第 01 轮：
   - Route 迁移矩阵；
   - 共享问题账本；
   - 分层回归页池计划；
5. 从 TBM1 的 `cover` 开始 Lift-and-Wrap；
6. 每叶只完成薄门禁并前进，不做整本 PDF 和逐叶 Owner 硬停；
7. 全部原子叶和 composite 达到 ready 后，再进入 TBM3 集成调试。

建议使用仓库虚拟环境：

```powershell
& 'D:\项目\开源项目\translation\transflow\.venv\Scripts\python.exe' ...
```

真实模型凭据只能通过进程环境注入：

```text
TRANSFLOW_MIGRATION_QWEN_BASE_URL
TRANSFLOW_MIGRATION_QWEN_API_KEY
TRANSFLOW_MIGRATION_QWEN_MODEL
```

不得把凭据复制进本计划、接手背景、Manifest、日志或代码。
