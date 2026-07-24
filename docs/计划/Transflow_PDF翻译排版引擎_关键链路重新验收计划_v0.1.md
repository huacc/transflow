# Transflow PDF 翻译排版引擎关键链路重新验收计划 v0.1

| 项 | 内容 |
|---|---|
| 文档状态 | 已闭合：RV0～RV6 已按各轮记录闭合；RV7/TM2 已由负责人 `ACCEPTED`；TM3 阻断已解除 |
| 编制时间 | 2026-07-21（Asia/Shanghai） |
| 计划性质 | P4～P9B 当前有效性前向重新验收，不改写历史 Gate |
| 触发阶段 | Toolbox 逐类别迁移 TM2 |
| 当前停止点 | G-RV-11=`PASS_WITH_OWNER_SCOPED_REUSE`；G-RV-12=`ACCEPTED`；后续停止点转入 TM3 的 G-TM-14 |
| 上位设计 | docs/设计/Transflow_PDF翻译排版引擎_总体设计_v0.1.md |
| 原详细计划 | docs/计划/Transflow_PDF翻译排版引擎_详细开发计划_v0.1.md |
| 后续计划 | docs/计划/Transflow_Toolbox逐类别核心逻辑迁移与全流程验收计划_v0.1.md |
| 主要诊断来源 | page_classification_engine_puncture_v1、page_toolbox_engine_puncture_v1、TM2 正式运行证据 |

---

## 0. 2026-07-23 负责人处置覆盖

负责人查看 TM2 当前产物后明确回复“我看了，接受了”，并要求按逐叶计划执行 TM3。
该处置通过新轮次前向记录，不修改本计划下方的历史执行段落：

- owner-disposition：
  `runs/toolbox_leaf_migration/TM2/20-owner-disposition-20260723-141316/`；
- 阶段 Gate：
  `resources/manifests/toolbox_leaf_migration/tm2_gate.json`；
- 当前结论：`G-RV-12=ACCEPTED`、`G-TM-14=ACCEPTED`；
- TM3 源资产冻结：
  `runs/toolbox_leaf_migration/TM3/01-source-freeze-20260723-142215/`。

因此，本关键链路重新验收计划已经完成其“恢复可信上游并决定是否允许继续逐叶迁移”的职责。
TM3 的技术、产品与人工结论必须在 Toolbox 迁移计划下独立产生。

## 1. 决策与目的

### 1.1 当前决策

在继续 Toolbox 逐类别迁移前，对会直接影响“页面是否被完整识别、正确分类、完整翻译、正确排版并交付”的历史关键链路重新验收。

本计划不是把 P0～P9B 全部推倒重做，也不是把历史 PASS 改成 FAIL。它建立一层当前有效性结论：

- 历史 Gate 继续表示当时输入、当时阈值和当时实现下的结论；
- 当前重新验收表示加入真实反例、扩大覆盖后，现有生产链是否仍可作为 Toolbox 后续迁移的可信上游；
- 历史报告、历史 manifest 和历史运行产物保持只读；
- 新问题只通过新的回归样本、当前 overlay、新运行目录和新报告前向承接。

### 1.2 为什么必须现在重新验收

Toolbox 逐类别计划原本把 ClassificationEngine 作为冻结上游，但 TM2 已经暴露出该前提不完全成立：

1. p0151 被生产 ClassificationEngine 路由为 body.flow_text.single，实际页面同时包含大段连续正文和有实质语义的表格；独立分类 Spike 路由为 body.composite.flow_text_table。
2. p0101 的正文虽已翻译，但页脚未翻译，且行距没有达到当前统一可读性要求。
3. p0150、p0152 等页面已有完整真实译文，但因 TEXT_LAYOUT_OVERFLOW 回退为源页。
4. TM2 第 08 轮共有 16 个自然命中目标 Route 的页面没有形成完整翻译排版结果。
5. 旧 G5 的真实匿名集只有 22 个样本，允许高代价误路由率不超过 15%；实际已有 1 个高代价误路由仍获得 PASS。

这些事实说明，历史 Gate 可以真实执行并按旧口径 PASS，但不能自动证明当前真实整本 PDF 的产品链已经可靠。继续迁移新的 Toolbox 叶会让分类、语义完整性、布局和 Repair 问题混在一起，增加错误定位成本。

### 1.3 本计划要回答的问题

本计划必须给出可复核答案：

1. PageFacts 是否完整识别正文、表格、页眉、语义页脚、纯页码和 protected 对象？
2. ClassificationEngine 是否能稳定区分 single、table 和 flow_text_table 等高代价边界？
3. ClassificationRoute、ToolboxCatalog 和实际运行 Toolbox 是否一致？
4. PageTextInventory 与 SemanticUnitMap 是否覆盖所有应翻译文字？
5. 完整译文是否真实物化到 PDF，而不是因布局失败退回源页后仍被描述为完成？
6. 行距、overflow、collision、页脚和 protected 对象是否由确定性 Judge 检查？
7. Repair 是否产生真实新候选并有界收敛？
8. 完整 PDF 是否逐页产生可追溯输出，并最终保持页数、页序和可打开性？

---

## 2. 范围与非目标

### 2.1 必须重新验收的关键阶段

| 原阶段 | 当前重新验收重点 | 重新验收原因 |
|---|---|---|
| P4 / G4 | 完整 PDF 枚举、PageFacts、页面 identity、增量页输出、DocumentCoordinator | 页脚与表格事实会同时影响分类和翻译完整性 |
| P5 / G5 | 规则、真实模型主判/复核、Resolver、匿名质量集、结构扰动和高代价误路由 | p0151 已证明小型语义表格存在规则覆盖缺口 |
| P6 / G6 | SharedPdfKernel、文字/表格/绘图提取、Patch、字体、Preservation | 需要排除事实提取错误、擦除错误和 PDF 物化错误 |
| P7 / G7 | ClassificationRoute、ToolboxCatalog、owner、capability、fallback 和运行时命中 | 错误 Route 不得静默进入不具备处理能力的叶 |
| P9C / G9C、P9A.0 | PageTextInventory、SemanticUnitMap、TranslationCompletenessDecision、诊断/final 隔离 | 页脚遗漏和目标页透传表明完整性合同需在真实页上重验 |
| P9A / G9A | 文档级布局事实、页角色和边距事实的只读消费 | 页面布局不能依赖错误或缺失的跨页事实 |
| P9B / G9B | PageRepairMemory、RepairAtom、真实候选、Judge、重排和终态 | p0150/p0152 的 overflow 需要验证 Repair 是否具备真实收敛能力 |

### 2.2 只做回归、不完整重验的阶段

P0～P3 只运行以下回归：

- 环境和依赖可复现；
- Domain、Schema、Port 与 Artifact 合同不退化；
- 静态检查、类型检查、秘密扫描通过；
- Standalone、Checkpoint 和测试 AI 边界不改变。

除非上述回归出现失败，不重新设计 P0～P3。

### 2.3 由 Toolbox 计划逐叶重新证明的阶段

P8、P9 和尚未执行的 P10 不按旧 Gate 再宣布一次“Toolbox 核心迁移完成”：

- P8/P9 的历史结论保留；
- 它们的公共合同进入本计划回归；
- 各分类叶的私有 Template、owner、reading order、TranslationUnit、Layout、Judge 和 Repair，继续由 TM1～TM18 逐叶证明；
- 不能用旧 G8/G9 的 disabled fallback 或骨架 PASS 代替当前 ProductAcceptance。

### 2.4 非目标

本计划不做以下工作：

- 不接入 MerqFin 数据库、API 或生产 Worker；
- 不提前执行 P12～P23；
- 不新增 OCR、图片文字翻译或像素级重绘；
- 不把运行时改造成开放式 Agent 或动态选工具系统；
- 不修改历史 Gate 报告以制造重新验收已经通过的印象；
- 不因某个样本失败写页码、文件名、sample_id 或固定坐标特例；
- 不把单页诊断结果冒充完整 PDF 产品验收。

---

## 3. 当前已知问题与强制回归集

所有下列问题必须在 RV0 冻结为不可静默删除的回归项。

| 编号 | 问题 | 当前证据 | 期望重新验收结果 |
|---|---|---|---|
| KRV-001 | p0151 分类错配 | TM2/08 p0151 为 body.flow_text.single；分类 Spike 为 body.composite.flow_text_table | 结构规则或有界决策稳定输出正确复合 Route；不得依赖样本身份 |
| KRV-002 | p0101 语义页脚未翻译 | TM2/05 case 04 输出仍含英文语义页脚 | 语义页脚进入 PageTextInventory、SemanticUnitMap、翻译和安全排版；纯页码保持 |
| KRV-003 | p0101 行距不足 | TM2/05 case 04 视觉结果 | body.flow_text.single 当前正文 line-height ratio 不低于 1.25，Judge 可机器判定 |
| KRV-004 | p0150/p0152 完整译文无法物化 | TM2/08 finding 为 TEXT_LAYOUT_OVERFLOW | 使用通用自然纵向流或有界 Repair 形成无 overflow/collision 的真实候选 |
| KRV-005 | 目标页被源页透传 | TM2/08 有 16 个目标页 translation_coverage=NONE | 当前叶正式目标页必须 FULL + PASS + fallback=NONE，否则阶段 FAIL |
| KRV-006 | 同页模型判断不稳定 | 正式整本与独立分类 Spike 对 p0151 路由不同 | 规则可决定时不调用模型；确需模型时重复运行和 Resolver 结果达到冻结稳定性要求 |
| KRV-007 | PDF 存在但不能交付 | 部分页面输出文件只是源页副本 | Artifact 存在、EngineeringClosure 和 ProductAcceptance 分开报告；源副本不能获得产品 PASS |

KRV-005 当前失败页为：

    6, 106, 111, 122, 148, 150, 151, 152,
    184, 187, 199, 208, 212, 214, 217, 221

其中 ROUTE_CAPABILITY_MISMATCH 和 TEXT_LAYOUT_OVERFLOW 必须分别统计，不能合并成一个“翻译失败”。

---

## 4. 证据、轮次与输出规范

### 4.1 权威运行目录

所有重新验收输出放入：

    runs/critical_chain_revalidation/

按阶段和轮号组织：

    runs/critical_chain_revalidation/
      RV0/
        01-baseline-<timestamp>/
      RV1/
        01-pagefacts-kernel-<timestamp>/
      RV2/
        01-classification-<timestamp>/
      RV3/
        01-routing-catalog-<timestamp>/
      RV4/
        01-semantic-completeness-<timestamp>/
      RV5/
        01-layout-repair-<timestamp>/
      RV6/
        01-full-document-<timestamp>/
      RV7/
        01-tm2-reacceptance-<timestamp>/

失败后修复重跑必须增加轮号，不覆盖旧轮。例如：

    RV2/02-classification-<timestamp>/

### 4.2 每轮最低目录

每轮至少包含：

    input/
      source_document.pdf
      source_manifest.json
    process/
      commands.jsonl
      environment_redacted.json
      gate_results.json
      known_regressions.json
      trace_index.json
    pages/
      pXXXX/
        input/source.pdf
        process/
        output/
          candidate.pdf
          candidate.png
    output/
      transflow.pdf
      preview/
    report.md
    run_manifest.json

任何不适用的文件必须在 manifest 中记录 NOT_APPLICABLE 和原因，不得用空文件占位。

### 4.3 完成一页，输出一页

完整 PDF 运行仍以未经拆页的源 PDF 为唯一业务输入，但每完成一页必须立即提交：

- 单页 source.pdf；
- PageFacts、分类、Route、Toolbox、语义单元、TranslationBundle、Layout/Judge/Repair 过程数据；
- 当前页 candidate/final PDF；
- 当前页 PNG；
- 页级完成记录和 hash。

整本 PDF 只能在全页终态屏障完成后合并。单页输出用于追踪和诊断，不替代整本最终 Artifact。

### 4.4 模型与秘密

真实分类和真实翻译继续使用负责人授权的千问服务，但：

- API Key 只通过当前进程环境变量传入；
- 源码、计划、manifest、日志、报告和运行目录不得保存 API Key；
- 报告可以记录 Provider 类型、Base URL 的脱敏标识、模型名、请求 hash、响应 hash、耗时和状态；
- 模型原始返回中如含敏感信息，保存前必须脱敏；
- mock/fake 只能用于合同故障测试，不能进入分类质量、翻译真实性或 ProductAcceptance 证据。

---

## 5. 总体执行顺序

| 阶段 | 工作单元 | 依赖 | 阶段结束状态 |
|---|---|---|---|
| RV0 | 冻结当前代码、历史证据、缺陷和新回归集 | 当前 TM2/08 | BASELINE_FROZEN |
| RV1 | P4/G4 + P6/G6：PageFacts、Kernel、Preservation | RV0 | FACTS_KERNEL_PASS |
| RV2 | P5/G5：分类当前有效性重新验收 | RV1 | CLASSIFICATION_PASS |
| RV3 | P7/G7：Route、Catalog、capability 与 fallback | RV2 | ROUTING_PASS |
| RV4 | P9C/G9C + P9A.0：文字分母、语义映射与完整性 | RV3 | COMPLETENESS_PASS |
| RV5 | P9A/G9A + P9B/G9B：布局事实、Judge 与 Repair | RV4 | LAYOUT_REPAIR_PASS |
| RV6 | 未经拆页完整 PDF 当前链路重新验收 | RV1～RV5 | FULL_DOCUMENT_PASS |
| RV7 | 用通过后的上游重新执行并验收 TM2 | RV6 | TM2_REVIEW_PENDING |

RV7 经过负责人明确 ACCEPTED 后，才解除 TM3 阻断。

---

## 6. RV0：冻结当前基线与回归集

### 6.1 目标

建立可重放、不可后验修改的当前重新验收输入，不在修复后丢失失败现场。

### 6.2 必做项

1. 记录当前 Git commit、dirty worktree 清单、Python/依赖、字体、策略、Catalog 和 Prompt hash。
2. 只读登记历史 G4～G9B 报告、manifest 和关键 Artifact hash。
3. 登记 TM2/05、TM2/08、分类 Spike run-20260721T073454Z 和 Toolbox Spike 第 34 轮。
4. 将 KRV-001～KRV-007 写入 known_regressions.json。
5. 建立匿名回归 manifest；身份字段与 sealed gold 分离。
6. 在任何规则、Prompt 或阈值变更前冻结新分类评测口径。
7. 记录哪些改动属于当前重新验收修复，哪些是进入本计划前已存在的用户改动。

### 6.3 测试

- RV0-T01：重复构建基线；预期输入 hash、问题清单和历史证据 hash 稳定。
- RV0-T02：扫描身份泄漏；预期模型载荷中文件名、路径、page_no、sample_id 和 gold 泄漏为 0。
- RV0-T03：扫描秘密；预期 API Key 明文命中为 0。
- RV0-T04：尝试覆盖历史报告；预期 Gate 阻断。
- RV0-T05：删除任一 KRV 项后核对；预期 Gate 阻断。

### 6.4 Gate

G-RV-01：基线与已知失败证据冻结完整率 100%，历史证据改写数 0，秘密泄漏数 0。

---

## 7. RV1：PageFacts、PdfKernel 与 Preservation

### 7.1 重新验收点

1. 完整 PDF 页数、页序、旋转、MediaBox/CropBox 和页面 identity。
2. native text block/span、表格、drawing、image、annotation、link 和字体事实。
3. 页眉、纯页码、语义页脚的分离。
4. 小型有语义表格不能因面积小而从事实层消失。
5. PageTextInventory 必须基于 Kernel 独立文字分母，不依赖当前 Toolbox 先选中了哪些对象。
6. Patch 擦除和写入不能破坏 protected drawing、图片、链接和页面结构。
7. 中文字体实际嵌入、ToUnicode 和文字可提取。

### 7.2 强制样本

- p0101：语义页脚和纯页码；
- p0151：小型表格、连续正文和页脚；
- p0150、p0152：长正文和高密度布局；
- visual_only 的纯图片、纯矢量、扫描页和混合无可编辑文字页；
- 至少一份未经拆页的完整真实年报。

### 7.3 测试

- RV1-T01：完整文档枚举两次；预期 page identity 和 PageFacts hash 稳定。
- RV1-T02：检查 p0151；预期 table count、表格文字和表外正文均可追溯。
- RV1-T03：检查 p0101；预期语义页脚进入可翻译文字分母，纯页码为 KEEP_SOURCE。
- RV1-T04：对 protected 对象执行候选 Patch；预期 protected hash 无变化。
- RV1-T05：保存、重开、重新提取候选 PDF；预期页数/页序、可打开性和已写中文均通过。
- RV1-T06：重跑 P4、P6 现有静态、类型、合同和关键 E2E。

### 7.4 Gate

G-RV-02：强制样本事实覆盖率 100%；应识别表格/页脚漏失数 0；PageFacts 非确定漂移数 0。

G-RV-03：Patch 后 protected 对象无解释变化数 0；页数、页序和可打开率 100%；中文实际写入且可提取。

---

## 8. RV2：ClassificationEngine 当前有效性

> 2026-07-22 执行记录：647 页校准集、32 页事前冻结盲集和历史 22 例的高置信规则冲突/错误跳模均为 0；p0151 结构目标与五种扰动通过。因真实迁移模型环境未配置，最终 Route 重放与三次独立模型重复调用未执行，故 G-RV-04 保持 `NOT_PASSED / EVIDENCE_INSUFFICIENT`。证据见 `runs/critical_chain_revalidation/RV2/01-current-validity-20260721-233029/report.md`。

> 2026-07-22 当前有效性覆盖：已在新轮次补齐真实千问模型重放。p0151 三次均为
> `body.composite.flow_text_table`，历史匿名集 22/22；但原 32 页盲集在修复过程中已经暴露，
> 当前只能作为回归集，结果为 25/32。七个失败集中在图表注释、图表加独立主题块、表格加
> 表外正文、结构图与视觉锚定正文等 Toolbox owner 边界。规则层三个集合的高置信冲突和
> 错误跳模仍为 0，权威批次无 `unclassified`；冻结阈值未下调，新严格盲集因停止门未通过
> 而未开启。因此 G-RV-04 更新为 `NOT_PASSED / MODEL_VALIDITY_FAILED`，不是上一轮的环境
> 缺失。证据见 `runs/critical_chain_revalidation/RV2/02-live-replay-20260722-081513/report.md`。

> 2026-07-22 新严格盲测覆盖：从剩余 413 个合格原文单页中按 16 类各冻结 2 页，共 32 页、
> 31 份源文档；与旧盲集页面及旧盲集源文档重叠均为 0。公开清单不含 gold 和源身份，四批
> 模型运行完成 112/112 次成功响应后才解封计分。结果为 26/32，失败集中在
> `anchored_blocks`、`anchored_blocks_chart`、`chart_table` 和 `end`；人工查看四张联系表后
> gold 改动数为 0。`03`、`04` 两个前置轮次因盲测工具错误在模型调用前显式作废，权威轮次
> 为 `runs/critical_chain_revalidation/RV2/05-fresh-blind-20260722-094154`。新盲测未达到冻结
> 阈值 100%，所以 G-RV-04 继续 `NOT_PASSED / MODEL_VALIDITY_FAILED`。

> 2026-07-22 负责人边界裁决：保留上述 25/32、26/32、100% 冻结阈值及
> `NOT_PASSED / MODEL_VALIDITY_FAILED` 原始证据，不覆盖、不改标、不事后改写计分。负责人结合
> “整页标签是执行路由而非页面唯一内容真值、Toolbox 仅在有界能力包络内兼容相邻结构、混合正文
> 最终由 Freeform 有界拆区恢复”的设计边界，将 RV2 阶段生效状态裁决为
> `PASS_WITH_BOUNDARY_OBSERVATIONS`，G-RV-04 记为 `PASS_WITH_OWNER_DISPOSITION`。该裁决满足
> RV3 的进入依赖，但不证明六个不一致页已经被当前运行时安全消化。六页冻结为 RV3 能力边界
> 补充回归集；补充验证完成前 G-RV-05 仍为 `NOT_RELEASED`，RV4/TM3 继续阻断。处置证据见
> `runs/critical_chain_revalidation/RV2/06-owner-disposition-20260722-101614/report.md`。

### 8.1 重新验收原则

分类重新验收分成三组，不能互相替代：

1. 历史匿名集：证明旧 22 例不退化。
2. 已知反例集：证明 KRV-001/KRV-006 等已发现缺口被通用修复，正确率必须 100%。
3. 新增盲测集：在修复前冻结 gold 和阈值，验证修复没有只适配已知页面。

分类规则或 Prompt 行为改变必须留下独立评审记录。不得按运行结果临时修改阈值。

### 8.2 必须扩充的结构层

新增匿名结构样本至少覆盖：

- 大段正文 + 小型有边框语义表格；
- 大段正文 + 小型无边框语义表格；
- 只有少量数字的装饰性框，不应误判为表格；
- 单栏正文、双栏正文和视觉锚定正文；
- table 主导页与 flow_text_table 混合页边界；
- chart/table、flow_text/chart、flow_text/diagram 等 composite 边界；
- 页脚或页码不能改变 body Route；
- 缩放、平移、等长文本替换和对象数量扰动。

若某个拟启用 Route 没有足够独立真实文档，当前结论只能是 EVIDENCE_INSUFFICIENT，不得用重复页扩充样本数。

### 8.3 p0151 的强制判定

p0151 的 gold 在模型运行前依据以下结构事实冻结：

- 页面有大段连续表外正文；
- 页面有包含表头和数据行的实质表格；
- 两者分别需要 flow text 和 table owner；
- 当前 taxonomy 对应 body.composite.flow_text_table。

修复必须由结构事实驱动。禁止读取页码 151、源文件名、run 路径或已知文字片段。

### 8.4 稳定性与模型边界

1. 规则达到确定证据时，目标节点模型调用数必须为 0。
2. 规则 INCONCLUSIVE 时，primary/review 各最多一次，Resolver 确定性收敛。
3. 对高代价边界页至少执行三次独立真实模型调用，保存每次 request/response hash。
4. 已知反例 Route 一致率必须 100%。
5. 新增盲测集按事前冻结指标判断；未达标的 Route 保持 disabled 或阻断后续，不降低阈值。

### 8.5 测试

- RV2-T01：重跑原 G5 匿名集；预期无新增退化。
- RV2-T02：运行 p0151；预期 body.composite.flow_text_table。
- RV2-T03：将 p0151 缩放、移动并替换等长文本；预期 Route 不变。
- RV2-T04：输入面积相近但没有实质表格语义的页面；预期不因面积阈值误判 composite。
- RV2-T05：重复真实模型边界测试；预期达到冻结稳定性门槛。
- RV2-T06：注入 timeout、非法 JSON、非法 action 和证据引用；预期都有确定 Route/fallback，无无路由状态。
- RV2-T07：扫描样本身份特例；预期命中 0。

### 8.6 Gate

G-RV-04：

- 历史匿名集不退化；
- 已知反例正确率 100%；
- 身份特例数 0；
- 无路由状态数 0；
- 新盲测指标达到事前冻结值；
- 证据不足 Route 明确 disabled，不得伪造 PASS。

本轮负责人裁决不声称“新盲测达到事前冻结值”，而是显式保留该子项 FAIL，并按阶段职责重新解释
停止门：精确叶标签继续作为诊断指标；六个不一致页是否能由实际 Route/Toolbox 有界覆盖，转交 RV3
以能力兼容、对象所有权和安全 fallback 判定。`PASS_WITH_OWNER_DISPOSITION` 对 RV3 进入依赖等价
于 PASS，但不得用于宣称 ClassificationEngine 已获得完整产品级验收。

---

## 9. RV3：Route、Catalog 与 capability

### 9.1 重新验收点

1. 每页只有一个不可变 ClassificationRoute。
2. Route 必须通过显式 Catalog 找到唯一 Toolbox 或唯一 disabled fallback。
3. Production 不得注入目标 Route、动态换链或调用其他叶私有工具。
4. Toolbox 实际能力必须与 Route 的对象结构匹配。
5. ROUTE_CAPABILITY_MISMATCH 必须保留原分类证据并明确失败位置。
6. 对当前迁移目标页，能力错配不能被源页透传掩盖为 ProductAcceptance PASS。
7. 测试注入 Route 只能进入 TEST_ONLY 证据。

### 9.2 测试

- RV3-T01：遍历当前 taxonomy；预期每条 Route 唯一注册或唯一 disabled fallback。
- RV3-T02：对 p0151 使用重新验收后的分类；预期不再进入 body.flow_text.single。
- RV3-T03：故意把 composite 页交给 single guard；预期 ROUTE_CAPABILITY_MISMATCH，不产生伪译文。
- RV3-T04：扫描运行时目录发现、页码特例和跨叶调用；预期命中 0。
- RV3-T05：完整 PDF 乱序并发分类；预期 Route 按 page identity 正确归并。

### 9.3 Gate

G-RV-05：Route/Catalog/Toolbox 一致率 100%；动态换链、跨叶调用和产品证据中的强制 Route 数均为 0。

> 2026-07-22 执行记录：权威轮次为
> `runs/critical_chain_revalidation/RV3/02-routing-catalog-20260722-012551`。先按内容哈希排除
> 14 个跨类别冲突金标，再从 16 个具体类别各冻结 1 个无冲突原文单页；17 条 taxonomy
> 与 Catalog 唯一覆盖，具体类别与 15 个 Spike Toolbox 加内置 `visual_only` 一致率
> 100%。p0151 的结构 Route 为 `body.composite.flow_text_table`；故意错投 single 时在翻译
> 前得到 `ROUTE_CAPABILITY_MISMATCH`，翻译、Toolbox 私有阶段和 Patch 数均为 0，分类证据、
> 所需 owner、原因及失败位置完整保留。冻结年报物理页 140～169 的 30 页切片完成乱序并发
> 归并，页序与 page identity 一致率 100%；37 个定向回归、Ruff、Mypy 通过。RV3 技术条件
> 为 PASS，但前置 G-RV-04 仍为 `NOT_PASSED / EVIDENCE_INSUFFICIENT`，故 G-RV-05 正式
> 状态保持 `NOT_RELEASED`，不得进入 RV4/TM3。首轮
> `01-routing-catalog-20260722-012335` 因误纳跨类别冲突金标已显式作废并只保留过程证据。
> 补充组合回归结果为 `242 passed, 11 failed, 6 errors`：未通过项分别来自缺少真实千问
> 环境、当前 P8 页边文字分母/Kernel 预授权合同、P9C 历史指纹漂移和既有语义清单合同；
> 它们没有被本轮定向 PASS 覆盖，也没有在 RV3 中跨阶段修补。

> 2026-07-22 上游状态覆盖：RV2 真实模型环境已补齐，但 G-RV-04 因已暴露回归仅
> 25/32 且新严格盲测未开启，更新为 `NOT_PASSED / MODEL_VALIDITY_FAILED`。RV3 自身技术
> PASS 不变，G-RV-05 仍为 `NOT_RELEASED`；“等待补环境”不再是解锁动作，必须先解决
> 七个通用分类边界并完成新的严格盲测。

> 2026-07-22 新盲测状态覆盖：新的严格盲测已经执行，不再是“尚未开启”；结果为 26/32，
> 仍未达到冻结阈值。RV3 自身技术 PASS 不变，G-RV-05 继续 `NOT_RELEASED`，RV4/TM3
> 继续阻断。最新依据见 `runs/critical_chain_revalidation/RV2/05-fresh-blind-20260722-094154/report.md`。

> 2026-07-22 负责人裁决覆盖：RV2 已以 `PASS_WITH_BOUNDARY_OBSERVATIONS` 满足 RV3 进入依赖；
> 原 26/32 计分和失败明细保持不变。RV3 现获准补做六页“实际 Route 是否覆盖所需模块”的能力
> 边界验证。现有 RV3 技术 PASS 不自动证明这六页安全，故 G-RV-05 仍为 `NOT_RELEASED`；补充
> 验证通过后再正式判定，RV4/TM3 当前仍阻断。

> 2026-07-22 六页能力边界补充验收覆盖：权威轮次为
> `runs/critical_chain_revalidation/RV3/03-six-boundary-addendum-20260722-103709`。六页均按 RV2
> 真实模型给出的 Route 原样重放：1 页为零原生文字的兼容透传，5 页在翻译和 Toolbox 私有阶段
> 之前由 disabled Catalog 安全拒绝；翻译调用、Patch、动态换链、跨叶调用和未解释接受均为 0。
> 源/候选 PDF 与 PNG 哈希均为 6/6 相同，人工视觉复核 6/6 通过，38 个 RV3 定向回归、Ruff、
> Mypy 通过。据此 `G-RV-05 = PASS`，RV4 获准启动；该结论不表示 5 个 disabled Toolbox 已具备
> 翻译能力，也不表示 Freeform 已实现，TM3 仍按阶段顺序阻断。

---

## 10. RV4：文字分母、语义映射与翻译完整性

### 10.1 重新验收点

1. PageTextInventory 在 Toolbox 准备前独立建立。
2. 每个可读文字对象必须进入 SemanticUnitMap，并且只有以下处置：
   - TRANSLATE；
   - KEEP_SOURCE，带允许原因；
   - PROTECTED，带对象证据；
   - UNSUPPORTED，带能力原因并阻断产品 PASS。
3. 页眉、章节标题、正文、表格文字和语义页脚都必须纳入。
4. 纯页码、标准编号和必要 symbol literal 可 KEEP_SOURCE。
5. TranslationBundle unit ID 与请求双向覆盖 100%。
6. 空串、占位符、异常回显、未授权原文照抄、丢 required literal 和残留源语言必须失败。
7. 无完整译文时不得生成或宣称 translated final。

### 10.2 测试

- RV4-T01：p0101 页脚双向覆盖；预期语义页脚翻译、纯页码保持。
- RV4-T02：p0151 正文与表格单元双向覆盖；预期无 unresolved unit。
- RV4-T03：删除、重复、新增或错配 unit ID；预期完整性 Gate 阻断布局。
- RV4-T04：返回空串、占位符、原文回显；预期质量失败。
- RV4-T05：完整译文存在但布局失败；预期可保存隔离诊断候选，final 不冒充成功。
- RV4-T06：目标页透传；预期 ProductAcceptance=FAIL。

### 10.3 Gate

G-RV-06：PageTextInventory、SemanticUnitMap 和 TranslationBundle 双向覆盖率 100%；未授权原文残留接受数 0；目标页无译文透传接受数 0。

> 2026-07-22 执行覆盖：权威运行
> `runs/critical_chain_revalidation/RV4/04-translation-completeness-20260722-115445/`
> 以 `G-RV-06 = PASS` 收口。p0101 为 54/54、p0151 为 67/67 的
> Inventory/Map 双向覆盖，未解析与不支持单元均为 0；真实千问分别物化 15、55 个
> TRANSLATE 单元。p0101 的语义页脚由 `shared.margin.footer` 翻译，纯页码以
> `KEEP_SOURCE/PAGE_NUMBER` 保留；p0151 的正文与 9 个表格文字对象均进入同一完整性门。
> 12 类原文单页的 v2 map 结构重放均为 100%，但只证明文字分母合同，不表示 disabled
> Toolbox 已实现。RV4-T03～T06 的故障与隔离回归通过，布局失败诊断产物存在且 final
> 产物数为 0；未授权原文残留和目标页透传接受数均为 0。前三次失败运行保持只读，未被
> 覆盖。阶段报告见
> `docs/reports/RV4阶段_文字分母语义映射与翻译完整性_20260722_115445.md`。
> 据此 RV5 获准启动；统一 margin 排版属于 RV5，TM3 仍按顺序阻断。

> 2026-07-22 当前有效重放：RV5 暴露“正文行内脚注序号被按纯页码保留”的边界后，页码处置
> 收紧为“纯数字或罗马数字且位于页面顶部/底部 8% 页边区域”。由于该变化影响文字分母，已在
> `runs/critical_chain_revalidation/RV4/05-translation-completeness-20260722-124238/`
> 重新执行 RV4。p0101 54/54、p0151 67/67，真实千问物化 15、55 个单元，12 类分层单页结构
> 重放 100%，未授权原文残留和目标页透传接受数均为 0；`G-RV-06` 继续 `PASS`。当前指针已
> 前移到运行 05，运行 04 作为历史 PASS 保留。报告见
> `docs/reports/RV4阶段_文字分母语义映射与翻译完整性_20260722_124238.md`。

---

## 11. RV5：布局、Judge 与 Repair

### 11.1 重新验收点

1. 连续正文使用自然阅读流，不机械锁死每个源文字块的原高度。
2. body.flow_text.single 当前正文 line-height ratio 不低于 1.25；其他叶继续使用各自冻结策略，不把 single 数值静默推广为全局常量。
3. 标题、正文、列表、语义页脚和纯页码分别处理。
4. 页脚可以使用安全空白区，但不能覆盖页码、边注或 protected 对象。
5. Judge 必须检查 overflow、collision、owner/clip 越界、protected 修改、行距和目标文字实际物化。
6. Repair 每轮必须产生真实新 PDF、重新提取事实并重新 Judge。
7. 重复动作、相同候选 hash 循环或预算耗尽必须确定性失败。

### 11.2 强制页面

- p0101：正文行距、语义页脚和页码；
- p0150：长正文自然纵向流；
- p0152：长正文自然纵向流；
- p0122、p0148、p0184、p0187、p0212、p0217、p0221：同类 overflow 回归；
- 至少三个当前已经 PASS 的 single 页：防止修复导致退化。

### 11.3 测试

- RV5-T01：p0101；预期正文行距达到当前 single 门槛，页脚译文与页码不冲突。
- RV5-T02：p0150/p0152；预期真实译文写入且 overflow/collision 为 0。
- RV5-T03：制造超长译文；预期有界 scale/reflow/Repair 后 PASS，或诚实 FAIL，不能源页冒充译文。
- RV5-T04：制造页脚与页码竞争；预期安全横向边界或明确失败。
- RV5-T05：重复 RepairAtom；预期循环检测。
- RV5-T06：重跑 P9A/P9B、P4 和 single 相关回归。
- RV5-T07：渲染源页、候选和最终页并执行人工可读性复核。

### 11.4 Gate

G-RV-07：强制页面 overflow、collision、越 owner/clip 和 protected 修改均为 0；single 行距违规数 0；译文实际物化率 100%。

G-RV-08：Repair 越预算、重复动作、相同 hash 循环和无真实候选次数均为 0。

> 2026-07-22 执行覆盖：权威运行
> `runs/critical_chain_revalidation/RV5/04-layout-judge-repair-20260722-130815/`
> 以 `G-RV-07 = PASS`、`G-RV-08 = PASS` 收口。13 张分类 Spike 原文单页覆盖全部强制页和
> 3 张防退化页，真实译文操作在候选及最终 PDF 中实际物化 201/201；overflow、collision、
> owner/clip 越界、protected 修改和当前 single 行距违规均为 0。超长译文故障注入诚实失败，
> 无 final、无源页冒充、无重复 Repair。13 组三联图及总览图已逐张人工复核并通过。前三个失败
> 运行保留，分别记录交错清除擦除译文、过度字体余量和 Judge 假阴性，不改写为 PASS。报告见
> `docs/reports/RV5阶段_布局Judge与Repair重新验收_20260722_130815.md`。据此 RV6 获准启动；
> 本阶段完整 PDF 执行数仍为 0，不能据此宣称整本产品完成。

---

## 12. RV6：完整 PDF 当前链路重新验收

### 12.1 输入

至少使用：

1. 当前 TM2 使用的未经拆页完整真实年报；
2. 新增盲测完整 PDF；如当前授权样本不足，必须明确记录证据不足；
3. 分类与 Toolbox Spike 单页只用于问题诊断，不计为完整 PDF 产品证据。

### 12.2 执行链

必须真实经过：

    完整 PDF
      -> DocumentCoordinator
      -> PageFacts / PageTextInventory
      -> ClassificationEngine
      -> PageRouter / ToolboxCatalog
      -> SemanticUnitMap
      -> Translation / Completeness
      -> Layout / Judge / Repair
      -> Page Patch / Page Outcome
      -> 每页增量 Artifact
      -> 全页终态屏障
      -> 单一完整 PDF

### 12.3 当前阶段的结论边界

由于 Toolbox 逐叶迁移尚未全部完成：

- 当前已迁目标 Route 的自然命中页必须真实翻译并排版；
- 尚未迁移 Route 可以按已声明 disabled fallback 收敛，但必须从 ProductAcceptance 分母中明确列出，不能称整本产品翻译完成；
- 不得删除未匹配页面，不得改变完整 PDF 页数或页序；
- 最终“所有可读页均翻译”的产品级结论只在全部必要叶完成并经过 TM18/P14 类整本验收后给出。

### 12.4 测试

- RV6-T01：完整 PDF 全页执行；预期每页有唯一终态和单页 Artifact。
- RV6-T02：检查当前目标 Route；预期 FULL + PASS + fallback=NONE。
- RV6-T03：检查非目标 disabled Route；预期明确降级清单，不冒充产品完成。
- RV6-T04：最后一页失败、暂停恢复和乱序完成；预期页序与最终化正确。
- RV6-T05：重开最终 PDF；预期页数/页序/可打开率 100%。
- RV6-T06：逐页提取目标语言与源语言残留，结合 SemanticUnitMap 复核。
- RV6-T07：生成 source / candidate / final 三联预览，由负责人检查。

### 12.5 Gate

G-RV-09：完整 PDF 页数、页序、可打开率和全页终态率均为 100%；每页 input/process/output 追踪完整。

G-RV-10：当前已迁目标页真实翻译覆盖率 100%；源页透传冒充译文数 0；尚未迁 Route 的降级披露率 100%。

---

## 13. RV7：TM2 重新执行与恢复后续计划

### 13.1 前置条件

只有 G-RV-01～G-RV-10 全部 PASS，才允许重新执行正式 TM2。

### 13.2 TM2 重新验收要求

1. 使用新轮号，不覆盖 TM2/08。
2. 使用重新验收后的 ClassificationEngine、PageFacts、完整性、布局和 Repair。
3. 所有自然命中 body.flow_text.single 的目标页必须：
   - translation_coverage=FULL；
   - quality=PASS；
   - fallback=NONE；
   - patch_present=true；
   - 目标语言实际可提取；
   - overflow/collision/行距/页脚 Gate 通过。
4. p0151 应离开 single 目标集合并进入正确 composite Route 证据，不得强制留在 TM2。
5. 保存每页 input/process/output、整本 PDF 和可视化对比。
6. 技术 Gate 通过后进入 REVIEW_PENDING，等待负责人逐页抽查和明确结论。

### 13.3 恢复条件

G-RV-11：TM2 技术 Gate 全部 PASS。

G-RV-12：负责人明确 ACCEPTED；在此之前 TM3 启动数必须为 0。

### 13.4 2026-07-23 当前执行记录

- 负责人明确要求不重新运行整本所有页面，采用既有有效证据复判、失败页定向修复和批准 Patch 前向回放；
- 当前运行：`runs/toolbox_leaf_migration/TM2/15-final-sampled-acceptance-20260723-120749`；
- 23 个自然命中 single 的目标页当前完整性、Patch 回放和最终文字匹配均为 23/23；
- 最终整本 PDF 为 240 页，Preservation PASS，页级候选 PDF 拼接数 0；
- G-RV-11 记为 `PASS_WITH_OWNER_SCOPED_REUSE`，不冒充一次新的全页重分类/重翻译；
- G-RV-12 仍为 `PENDING_OWNER_REVIEW`，TM3 启动数 0。

---

## 14. 总 Gate 清单

| Gate | 验收标准 |
|---|---|
| G-RV-01 基线保护 | 当前失败与历史证据冻结完整率 100%；历史报告改写数和秘密泄漏数 0 |
| G-RV-02 事实完整 | 表格、正文、页眉、语义页脚、纯页码和 protected 对象事实覆盖率 100% |
| G-RV-03 Kernel/Preservation | Patch 后页数/页序/可打开率 100%；protected 无解释变化数 0 |
| G-RV-04 分类有效 | 历史集不退化；已知反例正确率 100%；新增盲测达到事前阈值；身份特例和无路由数 0 |
| G-RV-05 路由能力 | Route/Catalog/Toolbox 一致率 100%；强制 Route、动态换链和跨叶调用数 0 |
| G-RV-06 语义完整 | Inventory/Map/Bundle 双向覆盖率 100%；非法原文回填、未授权残留和目标页无译文接受数 0 |
| G-RV-07 布局质量 | 强制页 overflow/collision/越界/protected 修改/当前 single 行距违规数均为 0 |
| G-RV-08 Repair 收敛 | 越预算、重复动作、候选 hash 循环、无真实候选数 0 |
| G-RV-09 完整文档 | 全页终态、页数/页序、可打开和逐页追踪完整率 100% |
| G-RV-10 诚实交付 | 当前目标页 FULL/PASS/NONE 比例 100%；源页冒充译文数 0；disabled 降级披露率 100% |
| G-RV-11 TM2 技术重验 | body.flow_text.single 新正式轮全部技术 Gate PASS |
| G-RV-12 人工停点 | 负责人 ACCEPTED 前 TM3 启动数 0 |

任一 Gate FAIL 时，报告必须非零退出或明确写 FAIL，不得以“已生成 PDF”代替验收结论。

---

## 15. 状态机与停止条件

### 15.1 状态机

    NOT_STARTED
      -> BASELINE_FROZEN
      -> FACTS_KERNEL_PASS
      -> CLASSIFICATION_PASS
      -> ROUTING_PASS
      -> COMPLETENESS_PASS
      -> LAYOUT_REPAIR_PASS
      -> FULL_DOCUMENT_PASS
      -> TM2_REVIEW_PENDING
      -> ACCEPTED

任何阶段可进入 FAIL。FAIL 修复后必须建立新轮次，从受影响的最早阶段重新执行。

### 15.2 立即停止条件

出现以下任一情况立即停止，不进入下一阶段：

- 历史报告、历史 Gate 或失败运行被覆盖；
- 模型载荷包含文件名、路径、page_no、sample_id 或 gold；
- 分类规则出现样本、页码或固定坐标特例；
- p0151 等已知反例仍错分；
- PageTextInventory 或 SemanticUnitMap 存在缺失、重复、悬空或未授权原文处置；
- 目标页没有真实译文，却生成源页副本并声称完成；
- overflow、collision、越 owner/clip 或 protected 修改被接受；
- Repair 没有真实重渲染和重新 Judge；
- 完整 PDF 删除页面、改变页序或缺少逐页 Artifact；
- 使用 mock/fake 结果支持产品质量结论；
- 缺少实际命令、输入、过程、输出或 hash；
- 当前阶段仍为 REVIEW_PENDING 就启动下一阶段。

---

## 16. 回归策略

### 16.1 每次修复的最小回归

每次修改必须按影响范围运行：

1. 新增失败复现测试；
2. 修改模块的定向测试；
3. 对应历史阶段测试；
4. 直接下游合同测试；
5. KRV-001～KRV-007；
6. 完整 PDF 当前链路；
7. 静态检查、类型检查和秘密扫描。

### 16.2 影响映射

| 修改类型 | 至少重跑 |
|---|---|
| PageFacts / Kernel | RV1～RV7 |
| 分类规则、Prompt、Resolver | RV2～RV7，并留下独立评审 |
| Route / Catalog / owner | RV3～RV7 |
| Inventory / SemanticUnitMap / Completeness | RV4～RV7 |
| Layout / Judge / Repair | RV5～RV7 |
| Artifact / Finalizer | RV6～RV7 |
| 仅测试或报告 | 对应 Gate 审计；不得改变运行结论 |

### 16.3 不允许的“修复”

- 对 p0151 写 page_no == 151；
- 对已知 PDF 写文件 hash 特例；
- 把 table block 删除后强行送入 single；
- 未匹配 Toolbox 就从最终 PDF 删除页面；
- 译文过长时直接保留整页英文并记 PASS；
- 为通过 Gate 修改 sealed gold 或事后降低阈值；
- 将诊断 PDF 复制为 final；
- 只看 pytest 通过，不渲染和检查实际 PDF。

---

## 17. 报告与人工复核

### 17.1 每阶段报告

每个 RV 阶段必须输出独立报告，至少包含：

- 阶段、轮号、开始/结束时间；
- 代码、配置、策略、Prompt、字体和输入 hash；
- 实际命令、退出码和原始输出路径；
- 每个测试和 Gate 的 PASS/FAIL；
- 已知回归逐项状态；
- 新发现问题；
- 修改文件与原因；
- 单页和完整 PDF Artifact；
- EngineeringClosure、ProductAcceptance、PromotionEligibility；
- 下一阶段是否允许启动。

### 17.2 人工复核重点

负责人至少查看：

- p0101：正文、行距、语义页脚、页码；
- p0150：长正文自然流；
- p0151：分类和最终负责 Toolbox；
- p0152：长正文自然流；
- 至少三个此前通过页面，确认无退化；
- 完整 PDF 中连续页面的阅读体验和页序。

人工复核只能决定接受、继续修复或接受 disabled，不能在运行时手改 PDF 后作为机器 Gate 证据。

---

## 18. 完成定义

本计划只有在以下条件全部满足时完成：

1. G-RV-01～G-RV-11 全部 PASS；
2. KRV-001～KRV-007 全部关闭或以负责人批准的 disabled 结论明确保留；
3. 历史 Gate 和报告未被改写；
4. 分类已知反例正确率 100%，新增盲测达到事前冻结阈值；
5. 目标页文字、语义映射和译文双向覆盖率 100%；
6. p0101 页脚与行距通过；
7. p0150/p0152 无 overflow/collision 且真实译文物化；
8. 完整 PDF 页数、页序、可打开率和逐页追踪完整率 100%；
9. 新正式 TM2 技术 Gate PASS；
10. 负责人完成 G-RV-12，明确 ACCEPTED。

在此之前：

- TM2 不得描述为完成；
- TM3 不得启动；
- 不得宣称 ClassificationEngine 已经获得当前完整产品级验收；
- 不得宣称整本 PDF 已经达到“所有可读页面均翻译”的最终产品标准。

---

## 19. 负责人需要冻结的两个决策

正式执行 RV2 新盲测前，需要把以下内容写入 RV0 baseline：

1. 新分类盲测集的逐叶最小样本数和高代价误路由阈值。建议不降低原阈值，同时对已知回归集要求 100% 正确。
2. 人工 ProductAcceptance 的抽查范围。建议至少覆盖本计划列出的四个重点页、三个既有 PASS 页和完整 PDF 连续页段。

这两个决策必须在查看新盲测结果之前冻结。若负责人暂未给出额外数值，执行时采用本计划中的保守边界：已知反例 100% 正确；新增盲测沿用既有冻结阈值但单独报告样本不足，不以重复样本补足。
