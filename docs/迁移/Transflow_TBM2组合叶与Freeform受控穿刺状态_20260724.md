# Transflow TBM2 组合叶与 Freeform 受控穿刺状态

日期：2026-07-24

依据：`Transflow_Toolbox批量核心迁移与分层集成验收计划_v0.2.md` §4.3

## 1. 阶段结论与边界

TBM2 按“不改总体架构、先验证迁移边界”的原则完成当前可执行范围：

- `body.composite.flow_text_chart`、`body.composite.flow_text_diagram` 已迁为独立组合根；
- `body.freeform` 已迁为只在分类失败后启用的有界内容区域兜底；
- 依赖 `body.table` 或 `body.anchored_blocks` 的三个 composite 没有抢跑，保持明确阻塞；
- 默认 Catalog 未修改，全部新 Route 继续 disabled；
- 本阶段只证明生产主链、根 owner、翻译、排版、Judge、Repair、fallback 和 Patch
  物化可贯通，不代表整本产品质量或负责人接受。

这里没有修改既定六阶段生命周期、Catalog 解析、Coordinator、Finalizer 或 PDF Kernel
架构。Spike 只作为类别核心和踩坑证据，生产代码不依赖 `spikes/`、`tests/` 或 `runs/`。

## 2. 五轴状态

| Route | CoreMigration | EngineeringConformance | ContractReadiness | IntegratedProductQuality | PromotionEligibility |
|---|---|---|---|---|---|
| `body.composite.flow_text_chart` | `COMPLETE` | `READY` | `READY` | `NOT_EVALUATED` | `KEEP_DISABLED` |
| `body.composite.flow_text_diagram` | `COMPLETE` | `READY` | `READY` | `NOT_EVALUATED` | `KEEP_DISABLED` |
| `body.freeform` | `COMPLETE` | `READY` | `READY` | `NOT_EVALUATED` | `KEEP_DISABLED` |
| `body.composite.flow_text_table` | `BLOCKED` | `FAIL` | `FAIL` | `NOT_EVALUATED` | `BLOCKED` |
| `body.composite.chart_table` | `BLOCKED` | `FAIL` | `FAIL` | `NOT_EVALUATED` | `BLOCKED` |
| `body.composite.anchored_blocks_chart` | `BLOCKED` | `FAIL` | `FAIL` | `NOT_EVALUATED` | `BLOCKED` |

后三项的 `FAIL` 表示依赖未就绪时没有建立可执行 factory 和合同候选，不表示已有实现发生
回归。它们分别被 `body.table` 或 `body.anchored_blocks` 的 TBM1 合同阻塞。

默认 Catalog SHA-256 保持：
`a43dccd10447943a8b3701265c9e85638a65a874d6876b8d1493d6f6886a2f8a`。

## 3. 工程化实现

### 3.1 独立组合根

两个 ready composite 各自拥有一个 PageToolbox 根，不在运行时实例化或拼接原子
PageToolbox。组合根只复用已经进入生产包的纯类别核心：

- single：`build_containers()`、`plan_placements()`；
- chart：`build_chart_template()`、`plan_chart_layout()`、`judge_chart_plan()`；
- diagram：`build_diagram_template()`、`plan_diagram_layout()`、
  `judge_diagram_plan()`。

每页只有一个根 `TranslationBatch` 和一个根 owner。所有操作按冻结 reading order
生成，同一源对象只允许一个 owner。叶级硬 Finding 在组合根收敛；有限 Repair 后仍失败时，
整根返回 passthrough，不向 Finalizer 提交部分 Patch。

### 3.2 有界安全处理

本轮只增加三条从 PDF 栅格复核得到的通用安全边界：

1. 页眉页脚等 shared margin 由组合根登记为 `retained`，不跟随正文类别写入 Patch，
   留给后续全局层处理；
2. 无节点 `local_label` 若内部文本行之间出现大于半行高的明确断带，说明模板可能把
   多个独立图框合成一个容器；该容器整块留源并记 `REGION_FALLBACK`，不猜测拆分；
3. 仅在 Freeform 中，同一 flow 容器若包含处于同一水平带、间距超过两倍行高的独立
   文字片段，则整容器留源；这避免把多个图表标签译成一串，同时不改变已分类 composite。

双栏正文仍复用 single 核心，但按源页 x 轴 lane 独立规划；左 lane 的合法右边界止于下一
lane 前，不再把两列串成一个纵向流。

### 3.3 Freeform 的实际含义

`body.freeform` 只允许在上游给出 `CLASSIFICATION_FAILED` 时使用。它不是新的 PDF
页面类型，也不会再次分类整页；它只对页内内容做一次有界分解。

固定 allow-list 为：

```text
diagram
chart
flow
```

`table` 和 `anchored_blocks` 因合同未就绪不在 allow-list。Freeform 不递归、不让模型
选工具、不动态注册叶；无法安全归属的内容留源。当前只有构造页工程穿刺，没有自然真实
freeform 样本，因此产品质量必须保持 `NOT_EVALUATED`。

## 4. 自动化与真实链路证据

TBM2 专项门禁：

```text
python -m pytest -q tests/test_toolbox_batch_migration_tbm2.py
10 passed in 132.76s
```

覆盖范围包括：

- 两个 composite 的单根请求、唯一 owner、Patch 可回放；
- chart/diagram 既有 Judge 接入组合根；
- diagram 双栏 lane 保持；
- shared margin、不连续 local-label 与 Freeform 远距横向片段有界留源；
- 根失败不输出部分 Patch；
- Freeform 固定 allow-list；
- run-private Catalog 只注册三个 ready Route；
- `page_concurrency=1/2` 结果等价；
- 默认 Catalog 指纹不变。

受影响既有回归：

```text
python -m pytest -q \
  tests/test_toolbox_leaf_migration_tm2.py \
  tests/test_toolbox_leaf_migration_tm3.py \
  tests/test_toolbox_leaf_migration_tm4.py \
  tests/test_critical_chain_rv4.py
95 passed, 1 failed in 194.79s
```

唯一失败仍为 TM3 冻结完整 PDF 的 `TARGET_PAGE_HASH_MISMATCH`。同一失败在 TBM2
组合包接入前已经存在；失败测试不导入本阶段 composite，当前没有修改历史 PDF、
`chart_01717_full.json` 或冻结哈希。

全仓 `python scripts/verify_architecture.py` 仍报告 9 条既有违规，均位于本轮未修改的
`adapters/ai/fixed.py` 或既有原子叶；`src/transflow/toolboxes/composites/` 没有出现在
违规列表。本阶段不越界清理历史架构扫描问题。

真实模型通过 OpenAI-compatible `/chat/completions` 接入
`Qwen/Qwen3.6-35B-A3B`，密钥只由进程环境提供，没有写入代码、文档或运行摘要。

| Route | 本地 run | 单元数 | 模型 HTTP 批次 | 根判定 | Patch 物化 |
|---|---|---:|---:|---|---|
| `body.composite.flow_text_chart` | `runs/toolbox_batch_migration/TBM2/05-flow-chart-qwen-full-judge-20260724/` | 11 | 1 | `ACCEPT` + `REGION_FALLBACK` | `fits=True` |
| `body.composite.flow_text_diagram` | `runs/toolbox_batch_migration/TBM2/09-flow-diagram-qwen-parallel-final-20260724/` | 33 | 2 | `ACCEPT` + `REGION_FALLBACK` | `fits=True` |
| `body.freeform`（注入构造页） | `runs/toolbox_batch_migration/TBM2/11-freeform-qwen-bounded-final-20260724/` | 2 | 1 | `ACCEPT` + `REGION_FALLBACK` | `fits=True` |

diagram 的第二次视觉复核确认：双栏正文保持并行；图结构未被替换；安全标签进入翻译；
被误合并的两个独立标签框保持源文，重叠消失。chart 的正文和图表均成功物化，页眉页脚
保持源文。Freeform 构造页的正文以正常宽度翻译，两个远距图表标签保持各自源位置；
这只证明注入接线和有界 fallback，不替代自然样本。

最终候选的受保护视觉对象计数与源页一致：chart 为 228 个 image、39 个 drawing，
diagram 为 0 个 image、50 个 drawing。

以上 `runs/` 由 `.gitignore` 排除，只是本地前向证据，不上传 GitHub。机器状态清单位于
`resources/manifests/toolbox_batch_migration/tbm2_gate.json`。

## 5. 验收限制

- 两个 composite 各只有一页已知样本，不满足盲样、跨文档和跨类别产品阈值；
- shared margin 和不连续 diagram 标签存在显式留源，所以结果不是 FULL translation；
- 当前 `SemanticUnitMap` 完整性合同仍要求 `retained` 文字进入根 TranslationBatch；
  它们的译文不会写入 Patch。要消除该请求开销并改由全局 margin owner 独立翻译，
  需要在后续共享合同阶段一次处理，本阶段不改架构；
- 大写 required literal 可能使局部标题保留英文机械字面量，留待统一翻译合同治理；
- Freeform 没有自然真实样本；
- 三个依赖未就绪的 composite 没有实现，也没有伪造候选。

因此 TBM2 的“完成”只表示三个可执行 Route 已达到工程和合同就绪，其余 Route 有明确
依赖阻塞。所有 Route 均不获得默认启用资格。
