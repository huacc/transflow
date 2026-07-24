# Transflow Toolbox 批量迁移与集成验收：会话接手背景

| 项 | 内容 |
|---|---|
| 文档用途 | 新会话独立接手剩余 Toolbox 迁移、集中调试与最终整本验收 |
| 编制时间 | 2026-07-24 09:39:10（Asia/Shanghai） |
| 仓库根目录 | `D:\项目\开源项目\translation\transflow` |
| 当前执行计划 | `docs/计划/Transflow_Toolbox批量核心迁移与分层集成验收计划_v0.2.md` |
| 历史执行计划 | `docs/计划/Transflow_Toolbox逐类别核心逻辑迁移与全流程验收计划_v0.1.md` |
| 新运行产物根目录 | `runs/toolbox_leaf_migration` |
| 建议 Python | `.venv\Scripts\python.exe` |

> 本文是新会话的入口，不是运行 Gate，也不覆盖历史 run。
> 当前工作树未找到早先路径中提到的两份
> `Transflow_*会话接手背景_20260721/20260723.md`；
> 本文根据当前工作树、代码、计划、Manifest 和 runs 重新建立接手事实。

---

## 1. 一句话接手结论

Transflow 的分类器、PageToolbox 合同、翻译完整性、布局/裁决/修复、Patch 回填、
DocumentFinalizer 和 Preservation 主框架已经形成；当前任务 **不是从零再造 PDF 引擎**，
而是把 Spike 中已经穿刺过的剩余分类专用 Toolbox 核心 Lift-and-Wrap 到这条生产主链，
随后集中解决跨类别视觉和产品化问题，最后再运行两份完整中英文年报。

2026-07-24 起，不再按“迁移一个叶 → 调很多轮 → 跑两份几百页 PDF → 人工停点”
的节奏推进。新的执行方式是：

```text
批量迁移剩余核心
→ 每叶薄契约门禁
→ 全叶 run-private Catalog 集成
→ GLOBAL / FAMILY / LEAF 集中调试
→ 双语整本集成基线
→ 定向回归
→ 双语整本发布候选
→ Owner 统一处置和 Catalog 冻结
```

完整规则见 v0.2 计划。本文只记录为什么改变、已经做了什么、当前真实状态、
新会话先做什么以及哪些事情不能误判。

---

## 2. 为什么要从 v0.1 切换到 v0.2

v0.1 的串行方式最初是为了隔离分类、翻译、Toolbox、Layout、Judge、Repair 和 Finalizer
等变量。在主链还不稳定时，这个选择合理。

实际执行 TM3 和 TM4 后，成本结构已经发生变化：

- TM3 `body.chart` 累计 24 个轮次；
- TM4 `body.diagram` 累计 27 个轮次；
- 很多轮次不是新增叶能力，而是在重复处理锚点、文字宽度、字号、行距、空白利用、
  Judge 边界、连接线碰撞、失败 PDF 和 Provider 异常；
- 这些问题具有跨类别或类别族属性，如果每迁一个叶才重新发现一次，会造成重复劳动；
- 为当前叶局部调共享规则，还容易产生过拟合、过度泛化或门禁比 Spike 真实合同更严。

负责人提出的核心判断是：

> 代码可以先把已有穿刺主体迁过来，再整体调试；否则一个类别反复调完再迁下一个类别，
> 同样的全局问题会被遗忘和重犯。

本次讨论接受这个方向，但保留一个必要边界：

> 不是“全部零测试搬完”，而是“每个叶只过薄契约门禁；重视觉、跨类别和整本测试后移”。

因此 v0.2 改变执行节奏，不改变引擎架构和安全边界。

---

## 3. 当前主框架到底完成到什么程度

### 3.1 已经存在的主链

当前 `src/transflow` 已有下列真实模块关系：

```text
完整源 PDF
→ DocumentCoordinator 枚举页面
→ SharedPdfKernel / PageFacts
→ ClassificationEngine / ClassificationRoute
→ ToolboxCatalog / PageToolbox
→ PageTextInventory / SemanticUnitMap
→ TranslationCompletenessDecision
→ Layout / Judge / Repair
→ PageOutcome / approved PagePatch
→ DocumentFinalizer 串行回放
→ Preservation / Final Artifact
```

这条链能够：

- 保持页数、页序和页面身份；
- 对每页形成确定性终态；
- 在翻译、布局或能力失败时回退；
- 保存诊断候选和最终 PDF；
- 从源 PDF 副本回放批准 Patch，而不是拼接页级候选 PDF。

所以后续不能再把 Toolbox 迁移写成“重新开发主框架”。

### 3.2 分类器

ClassificationEngine 和分类结果资产已经存在：

```text
src/transflow/classification
spikes/page_classification_engine_puncture_v1
spikes/page_classification_engine_puncture_v1/分类结果
```

分类结果目录还提供了正确类别和对应单页 PDF，和 Toolbox 穿刺目录一一对应。
后续迁移默认复用现有分类器，不为迁移某个叶修改分类树或强制 Route。

如果集成测试暴露真实分类缺陷，应单独登记为 Classification 问题；
不能通过迁移 runner 注入 Route 后把它写成产品证据。

### 3.3 多线程现状

不能简单写“多线程已经全部完成”：

- `DocumentCoordinator.classify_pages()` 有 `ThreadPoolExecutor` 分类并发入口；
- `ToolboxPageCoordinator.execute_many()` 有页级并发入口；
- 当前 `DocumentCoordinator.run_classified()` 使用流式分类扫描；
- 当前 `_execute_pages()` 仍按页串行执行；
- 最终 Patch 回放串行是设计要求，不应并发化。

因此，分类/页级并发原语已经有了，但完整生产入口尚未把所有页处理并发能力统一接通。
这是后续共享主链工程化事项，不应让每个 Toolbox 各自实现线程池。

### 3.4 P13/P14 与生产代码的关系

负责人已经明确质疑“为什么会和 P13/P14 有关系，代码不是应该只有一份吗”。
正确解释是：

- P13/P14 是 Spike 中 `body.chart`、`body.diagram` 的穿刺轮次、经验和行为证据；
- 它们是 Lift-and-Wrap 的来源，不是生产运行时的第二套代码；
- 最终生产代码只能落在 `src/transflow`；
- `src/transflow` 不得运行时导入 `spikes/`、`tests/` 或历史 `runs/`；
- 对 P13/P14 的引用只是说明“要保留什么已验证行为”和“Judge 不能比原合同更严”。

后续新会话如果再次把 P13/P14 当成并行运行链或重新实现目标，应立即纠正。

---

## 4. 当前 Catalog 和 Toolbox 状态

默认 Catalog：

```text
resources/catalogs/page_toolbox_catalog_v4.json
```

当前事实：

| 项 | 数量/状态 |
|---|---|
| Route 总数 | 17 |
| 默认 enabled | 2 |
| enabled Route | `visual_only`、`body.flow_text.single` |
| `body.chart` | 有专用生产实现，默认 disabled |
| `body.diagram` | 有专用生产实现，默认 disabled |
| 其他 Route | 普通骨架、轻量实现、pending 或 disabled fallback |

专用目录：

```text
src/transflow/toolboxes/leaves/body_flow_text_single
src/transflow/toolboxes/leaves/body_chart
src/transflow/toolboxes/leaves/body_diagram
```

普通叶骨架主要位于：

```text
src/transflow/toolboxes/leaves/ordinary.py
```

它为 cover、contents、end、multi、table、anchored_blocks 等提供结构所有权和回退骨架，
不能冒充 Spike 中专用 Template/Layout/Judge/Repair 已经全部迁移。

尚需处理的 13 条 Route：

```text
cover
contents
end
body.flow_text.multi
body.flow_text.visual_anchored
body.table
body.anchored_blocks
body.composite.flow_text_table
body.composite.chart_table
body.composite.flow_text_chart
body.composite.flow_text_diagram
body.composite.anchored_blocks_chart
body.freeform
```

`body.freeform` 没有可直接迁移的 Spike Toolbox，最后做有界新增。

---

## 5. 已完成工作的真实状态

### 5.1 RV/P9 系列

已有工程资产包括：

- 统一 PageToolbox 与 Catalog；
- Classification、Route/Capability；
- PageTextInventory、SemanticUnitMap、TranslationCompleteness；
- DocumentLayoutMemory；
- 页级 RepairMemory、RepairAtomCatalog 和有界修复；
- Finalizer、Patch 回放和 Preservation；
- 失败诊断候选与安全终态分离。

这些阶段证明主链和工程骨架存在，不证明所有分类专用 Toolbox 已经产品通过。

### 5.2 TM1 `visual_only`

- 已有生产透传实现；
- 默认 Catalog enabled；
- 不触发 TranslationPort、OCR、文本 Patch 或 Repair；
- 它是主链透传校准能力，不是 Spike 核心迁移案例。

### 5.3 TM2 `body.flow_text.single`

负责人正式处置：

```text
runs/toolbox_leaf_migration/TM2/20-owner-disposition-20260723-141316/
```

结论边界：

- TM2：`ACCEPTED`
- 来源 run：`15-final-sampled-acceptance-20260723-120749`
- 完整 PDF：240 页
- 目标 Route 页：23 页
- 技术状态：`PASS_WITH_OWNER_SCOPED_REUSE`
- `G-RV-12`、`G-TM-14`：`ACCEPTED`

该接受只覆盖已披露范围，不自动证明其他正文、chart、diagram 或 composite。

### 5.4 TM3 `body.chart`

已经完成：

- Spike 源资产冻结和 `body_chart` 专用生产目录；
- run-private Catalog 接入；
- 真实/记录译文、候选 PDF、失败 PDF、对比图和多轮回归；
- 对表格/图表行语义、数字前缀、长译文横向展开、公式与注释等做过多轮修复；
- 负责人要求“失败也必须产 PDF”后，失败页均保留诊断 PDF。

当前最新 30 页 replay：

```text
runs/toolbox_leaf_migration/TM3/
  24-body-chart-30-page-replay-regression-20260723-222429/
```

机器事实：

| 指标 | 结果 |
|---|---:|
| case | 30 |
| PASS | 18 |
| FAIL | 12 |
| 有输出 PDF | 30/30 |
| 有 review | 30/30 |
| translated diagnostic | 11 |
| translated gate rejected | 1 |
| Manifest 产品接受 | `false` |

这不是“30 页全部产品通过”。失败项中的译文质量不再要求过严，但布局、对象对应、
owner 和保护边界问题仍需在跨类别调试阶段重新判断。

TM3 没有独立的正式 Owner acceptance 文档；当前只有 TM4 授权中的
`OWNER_AUTHORIZED_TO_PROCEED`。不得把“允许继续 TM4”扩写成“TM3 所有产品质量已接受”。

### 5.5 TM4 `body.diagram`

已经完成：

- Spike 源资产冻结和 `body_diagram` 专用生产目录；
- 节点、连接线、局部标签、owner、文字 fit 和失败 Artifact 的多轮回归；
- 收回了比 P14 类别主体更严的 Judge；
- 把 `layout_search_region` 与 `hard_legal_boundary` 分开；
- 连接线采用“候选相对源页是否新增碰撞”的判断；
- 节点内文字不参与节点外局部标签的连接线门禁；
- 对字号上限、行距、正文 cohort 和真实 glyph 做过集中修复。

当前最新 30 页回归：

```text
runs/toolbox_leaf_migration/TM4/
  26-body-diagram-30-page-final-regression-20260724-054344/
```

机器事实：

| 指标 | 结果 |
|---|---:|
| case | 30 |
| PASS | 29 |
| FAIL | 1 |
| 有输出 PDF | 30/30 |
| 失败 case | `DG_ZH_00995_p307` |
| Promotion | `PASS_DISABLED_WITH_FALLBACK` |
| 默认 Catalog 修改 | `false` |
| Provider | recorded |

该结果仍不是默认启用或整本产品通过。

### 5.6 两份完整 01528 年报

负责人要求回归：

```text
样本/年报/01528_RS MACALLINE_英文_2025.pdf
样本/年报/01528_紅星美凱龍_中文_2025.pdf
```

当前只实际形成了英文完整年报 run：

```text
runs/toolbox_leaf_migration/TM4/
  27-01528-en-full-20260724-0812/
```

英文 run 的真实边界：

| 指标 | 结果 |
|---|---|
| 页数 | 322 |
| 最终 PDF 可打开 | 是 |
| 页数/页序/页面矩形 | 保持 |
| Preservation | PASS |
| 授权 Route 页 | 119 |
| 授权 Route PASS | 0 |
| `AI_RESPONSE_INVALID` | 116 |
| `AI_TIMEOUT` | 2 |
| `ROUTE_CAPABILITY_MISMATCH` | 1 |
| 页面 fallback | 322 页 `PAGE_PASSTHROUGH` |
| 整体状态 | `FAIL_WITH_FINAL_PDF` |

所以这个 PDF 证明的是：

- 322 页能完成分类、页面终态、最终化和 Preservation；
- 失败时仍能产生最终 PDF；
- 它没有证明 TM2/TM3/TM4 的译文在完整年报中成功写入。

当前工作树已将请求连接异常从 `AI_RESPONSE_INVALID` 中拆出为可重试 Provider 错误，
但 **尚未产生修改后的新整本 run**。下一会话不能把代码改动写成已经修复的产品证据。

中文完整年报尚未执行到同等级 Artifact；不能写“两份完整 PDF 已执行”。

---

## 6. 已经沉淀的跨类别经验

当前工程化经验：

```text
docs/经验/Transflow_跨类别文本锚点选择与保持经验_20260723.md
```

Spike 经验：

```text
spikes/page_toolbox_engine_puncture_v1/docs/经验
```

重要结论：

### 6.1 锚点不是全部锁左侧

- 正文通常保持所属阅读流左锚；
- 数值列通常保持右锚或 cell 对齐；
- 图、表、卡片、流程图标题可能保持中心轴，但必须有源页证据；
- 源左对齐标题仍保持左锚；
- 先确定 owner，再确定角色、源对齐和锚点；
- 源 glyph bbox 是证据，不是目标语言宽度牢笼。

### 6.2 中译英允许横向扩展

- 横向安全空间足够时，不受原中文窄 bbox 限制；
- 到达所属栏、cell、节点、图形或相邻 owner 边界后才换行；
- 不允许为了 fit 改变表格业务行、图表数据对象或 diagram 节点归属。

### 6.3 纵向可重排

- 左/右/中心锚点保持不等于纵向位置冻结；
- 同一阅读流有空间时可以调整换行、段距和行距；
- 纵向重排不能覆盖下一 owner 或改变阅读顺序；
- 同一正文 cohort 的字号和行距应一致。

### 6.4 美观度是结构化判断

- 标题、正文、注释的字体比例；
- 字体与空白空间比例；
- 纵向空白充足时的行距；
- 同 cohort 的字号一致性；
- 源页层级和对齐关系；
- 允许约 ±10% 或固定小步长搜索，禁止无界缩小。

### 6.5 Gate 不能制造假失败

必须区分：

```text
source_glyph_bbox
layout_search_region
hard_legal_boundary
```

共享 Judge 只执行叶声明的真实硬边界，不能把自己的保守安全框升级为类别事实。
对于连接线、障碍和既有碰撞，应比较“是否新增”，而不是要求译后绝对零相交。

### 6.6 失败必须可看

不可交付页也必须产出真实 PDF，负责人需要判断：

- 算法能力不足；
- 门禁过严；
- 翻译未物化；
- owner/对象绑定错误；
- 安全 fallback 是否合理。

不能只给 PASS/FAIL 数字而不提供问题 PDF。

---

## 7. 新计划必须持续记住的负责人决定

1. 这是迁移，不是从零开发；保持 Spike Toolbox 主体，做工程化适配。
2. 穿刺能力不足可以修改，但不能为了单页从头发明另一套算法。
3. 先批量迁移剩余叶，再统一调试和跑整本 PDF。
4. 每个叶仍需薄契约检查，不能完全零测试。
5. 输出统一放在 `runs`，每轮有明确轮号、input、output 和 process。
6. 失败页也必须生成 PDF。
7. 翻译另有服务治理；只要不是明显未翻译或合同无效，语言质量不做过严 Gate。
8. 所有用户反馈必须进入文件化问题账本和固定回归页，不依赖会话记忆。
9. GLOBAL 规则只处理真实跨类别不变量；类别特有规则留在 FAMILY/LEAF。
10. 防止单页过拟合，也防止把一个类别的规则过度泛化到所有 PDF。
11. 默认 Catalog 不提前启用 chart、diagram 或新迁叶。
12. 两份完整 01528 年报只在集成和发布里程碑运行，不在每个叶后重复运行。
13. 使用仓库 `.venv`：

    ```powershell
    & 'D:\项目\开源项目\translation\transflow\.venv\Scripts\python.exe' ...
    ```

14. 模型配置由进程环境读取；API Key 不得落盘。环境变量名：

    ```text
    TRANSFLOW_MIGRATION_QWEN_BASE_URL
    TRANSFLOW_MIGRATION_QWEN_API_KEY
    TRANSFLOW_MIGRATION_QWEN_MODEL
    ```

---

## 8. 新问题账本的首批种子

TBM0 必须把以下问题写入机器可读账本，并绑定正向页和负向哨兵：

| 建议 ID | 作用域 | 问题 |
|---|---|---|
| `GLOBAL-ANCHOR-001` | GLOBAL 合同、LEAF 决策 | 按 owner/源对齐选择 LEFT/RIGHT/CENTER，不能全部锁左或标题全部居中 |
| `GLOBAL-WIDTH-001` | GLOBAL | 目标语言可使用真实横向安全空间，源 bbox 不是最大宽度 |
| `GLOBAL-TYPO-001` | GLOBAL/FAMILY | 同正文 cohort 字号、行距、层级比例一致，允许有限步长调整 |
| `GLOBAL-VERTICAL-001` | GLOBAL/FAMILY | 纵向空间足够时允许同阅读流重排，不能造成文字碾压 |
| `GLOBAL-JUDGE-001` | GLOBAL 合同 | 区分 search region 与 hard boundary；比较新增违规 |
| `GLOBAL-ARTIFACT-001` | GLOBAL | 失败页必须生成诊断 PDF、Finding、Patch、Judge 和 fallback |
| `GLOBAL-TRANSLATION-001` | GLOBAL | Provider 连接错误、超时、响应非法必须正确分类和重试 |
| `FAMILY-TABLE-001` | 表格/图表 family | 保持业务行、列、系列和数值对象语义对应 |
| `FAMILY-DIAGRAM-001` | diagram family | 节点文字边界和节点外局部标签连接线增量门禁 |
| `LEAF-FORMULA-001` | chart | 注释/公式不得因翻译失败或对象拆分被静默丢失 |

已知回归入口：

```text
runs/toolbox_leaf_migration/TM3/
  24-body-chart-30-page-replay-regression-20260723-222429/

runs/toolbox_leaf_migration/TM4/
  26-body-diagram-30-page-final-regression-20260724-054344/
```

特别关注页：

```text
TM3 case 21: CH_ZH_02131_p123
TM3 case 22: CH_EN_02461-02562_p091
TM3 case 29: CH_EN_03700_p073
TM4 case 10: DG_ZH_00995_p052
TM4 case 20: DG_ZH_02400_p041
TM4 failure: DG_ZH_00995_p307
```

这些页是回归资产，不允许把它们的文件名、页码、文字或坐标写进生产规则。

---

## 9. 当前工作树安全警告

2026-07-24 09:39 核查时：

| Git 状态 | 数量 |
|---|---:|
| modified | 28 |
| deleted | 10 |
| untracked | 53 |
| 总条目 | 91 |

这些改动包含用户或前序会话资产：

- v0.1 计划和总体设计修改；
- TM3/TM4 代码、脚本、测试和 Manifest；
- `body_chart`、`body_diagram` 专用生产目录；
- Provider/TranslationCompleteness/Patch 等共享主链修改；
- 大量报告、经验、Gate 和运行清单；
- 若干样本数据库删除状态。

新会话必须：

1. 先运行 `git status --short`；
2. 不执行 `git reset --hard`、`git checkout --`、`git clean`；
3. 不恢复或删除与当前任务无关的文件；
4. 不把所有未提交改动当成自己本轮产生；
5. 新修改应保持手术式范围，并在报告列出实际 touched files。

---

## 10. 证据索引

### 10.1 计划与设计

```text
docs/计划/Transflow_Toolbox批量核心迁移与分层集成验收计划_v0.2.md
docs/计划/Transflow_Toolbox逐类别核心逻辑迁移与全流程验收计划_v0.1.md
docs/计划/Transflow_PDF翻译排版引擎_关键链路重新验收计划_v0.1.md
docs/设计/Transflow_PDF翻译排版引擎_总体设计_v0.1.md
docs/背景/PDF_翻译排版引擎_演进背景_既有资产与踩坑记录.md
```

### 10.2 Spike 来源

```text
spikes/page_classification_engine_puncture_v1
spikes/page_classification_engine_puncture_v1/分类结果
spikes/page_toolbox_engine_puncture_v1/toolboxes
spikes/page_toolbox_engine_puncture_v1/docs/经验
```

### 10.3 生产代码

```text
src/transflow
src/transflow/application/document_coordinator.py
src/transflow/application/toolbox_page_coordinator.py
src/transflow/application/translation_completeness.py
src/transflow/application/document_finalizer.py
src/transflow/toolboxes/catalog.py
src/transflow/toolboxes/leaves
src/transflow/pdf_kernel/patch.py
```

### 10.4 关键历史运行

```text
runs/toolbox_leaf_migration/TM2/20-owner-disposition-20260723-141316
runs/toolbox_leaf_migration/TM3/24-body-chart-30-page-replay-regression-20260723-222429
runs/toolbox_leaf_migration/TM4/26-body-diagram-30-page-final-regression-20260724-054344
runs/toolbox_leaf_migration/TM4/27-01528-en-full-20260724-0812
```

### 10.5 工程经验

```text
docs/经验/Transflow_跨类别文本锚点选择与保持经验_20260723.md
```

---

## 11. 新会话的固定第一轮

不要继续创建 `TM4/28-*`。第一轮应改为：

```text
runs/toolbox_leaf_migration/TBM0/
  01-baseline-and-migration-matrix-<timestamp>/
```

只做：

1. 读取本文和 v0.2 计划；
2. 保存当前 Git/Catalog/代码指纹，不修改历史证据；
3. 建立 17 Route 的 Spike→Transflow 迁移矩阵；
4. 建立 `GLOBAL/FAMILY/LEAF` 问题账本；
5. 建立跨类别分层页池计划；
6. 明确每个剩余叶的 Spike 源目录、经验文档、生产落点和薄门禁；
7. 输出 `report.md`，然后开始 TBM1 `cover`。

这一轮不做：

- 两份完整年报；
- 默认 Catalog 修改；
- 新一轮 TM4 视觉调参；
- 大范围共享代码重构；
- 分类树重写；
- Provider Secret 落盘。

---

## 12. 接手会话建议使用的技能

- `graphify`：先查询现有图谱，理解 Catalog、Coordinator、Toolbox、Patch 和 Finalizer
  的代码关系，避免把主框架重做。
- `pdf`：检查源 PDF、诊断候选、失败 PDF、整本最终 PDF 和真实视觉布局。
- `diagnose`：遇到 Provider、并发、Patch 回放或视觉回归时，按复现、最小化、假设、
  仪器化和回归测试处理。

使用技能不改变任务范围；任何代码修改仍须服从 v0.2 的作用域和 Gate。

---

## 13. 本交接文档的验证边界

本交接轮只完成了：

- 读取当前 v0.1 计划、演进背景、跨类别经验；
- 核对现有 Catalog、关键生产模块、TM2/TM3/TM4 Manifest 和完整年报摘要；
- 新建 v0.2 执行计划；
- 新建本文。

本交接轮没有：

- 修改生产代码；
- 运行新的 pytest；
- 调用真实模型；
- 运行新的单页或完整 PDF；
- 修改默认 Catalog；
- 对 TM3/TM4 追加产品接受结论。

因此新会话应把本文视为“当前证据索引和执行入口”，而不是新的技术 PASS。
