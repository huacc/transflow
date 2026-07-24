# Transflow v0.1 P9C/P9A/P9B 与 Harness + Loop 架构评审

- 评审日期：2026-07-20
- 评审结论：**有条件通过（GO WITH CORRECTIONS）**
- 评审快照：tracked HEAD `fa6bfbe3e5189e1797f30ec218e447cc2c0aa06f`；评审期间并行出现的未跟踪 P9C WIP 不作为已通过证据，本报告未修改或吸收这些文件
- 目标尺度：先打通 Transflow 独立 PDF 翻译排版闭环，再依据真实运行证据优化；不为未来能力预建通用平台
- 评审范围：两份 v0.1 文档、`page_classification_engine_puncture_v1`、`page_toolbox_engine_puncture_v1`，以及当前 `src/transflow` 已迁移实现和 Gate/迁移治理资产
- 非目标：本次不改总体设计和详细计划，不重写已归档 P5～P9 报告/Gate，不把 spike 目录或运行产物改造成生产运行时依赖

## 1. 总结判断

整体方向是合理的，不需要推倒重来：

1. `P9 -> P9C -> P9A -> P9B -> P10` 的顺序正确。先解决翻译完整性、诊断结果和路由错配，再冻结文档级只读上下文，最后运行页级有限修复，能够避免 Repair 在不完整译文或错误 Route 上反复优化。
2. `显式状态机 + typed contract + Port/Adapter + 薄 BoundedDecisionRunner` 适合当前问题。PDF 页面流水线的分支和终态是有限的，不需要 LangGraph、CrewAI、AutoGen 或另一套 Agent 持久化/重试系统。
3. Repair Loop 的主形态正确：每轮只选一个动作、从不可变源页和同一 TranslationBundle 重建、实际裁决、接受或回滚、达到预算后确定性降级。
4. 最终文档从源 PDF 副本串行回放 approved Patch，页面候选不拼接成最终文档；诊断候选与发布产物隔离。这是 PyMuPDF 路线下应保留的安全底线。
5. 两个 spike 证明了可迁移的技术种子，不证明全路由已经具备产品启用资格。当前设计按 leaf Gate 启用、证据不足则 fallback，并区分 EngineeringClosure、ProductAcceptance、PromotionEligibility，这一点是诚实且必要的。

需要收口的是：**P9C 补一个可信语义分母，P9A 只保留真正跨页的只读快照，P9B 从“学习系统”缩成“同 run 可恢复的确定性修复循环”。**

## 2. 已核实的基础事实

### 2.1 文档机械一致性

- 详细计划包含 27 个阶段和 27 个 Gate；P9C/P9A/P9B 已同步进入依赖排期，顺序为第 10～12 位，P10 顺延到第 13 位。
- 计划中识别到 779 个测试定义，ID 全部唯一；共 1175 次测试引用，未发现悬空引用。
- 因而阶段命名中的 `C/A/B` 虽不按字母时间排序，但文档已经明确解释和机械同步，本身不是问题。

### 2.2 穿刺成熟度边界

- 分类 spike 当前深度审计口径为 457 页：424 正确、30 错误、3 歧义；它证明分类树、一次主判/一次复核和 Resolver 路线可迁移，但不能替代 Route mismatch/fallback。
- toolbox spike 的状态并不整齐：存在 `PASS`、`PASS_NON_BLIND`、`NOT_EVALUATED`、`EVIDENCE_INSUFFICIENT` 和 `FAIL`，且没有可把全部叶视为已晋级生产的统一 Promotion 事实。
- 当前 P8/P9 已采取诚实策略：成熟叶启用，证据不足叶为 `PASS_DISABLED_WITH_FALLBACK`。P9 的 12 个真实千问样本只有 1 个候选安全接受、11 个安全回退，说明 P9C 的完整性、诊断双轨和结果分层不是纸面复杂度。
- 现有公共 Repair 已有 `max_rounds=3`、有限动作和硬回归回滚的种子，说明 P9B 应做工程封装，而不是另造开放式 Agent Loop。

## 3. 必须修正的问题

### F-01｜P0｜新增阶段尚未接入当前权威基线和可执行 Gate 治理

**证据**

- 当前总体设计 SHA-256 为 `4ca31f883ebcc024a5321841318389f0d755a6ab609c35df7e3a8ef8b622a191`，详细计划为 `9da35eb8dd6daa48c8f1aa64bfce477dc6d456000b972a740e3571d55e6251bc`。
- `docs/迁移/baseline_manifest.json` 仍绑定旧设计/计划 hash；`docs/迁移/traceability_matrix.json:4135-4139` 仍绑定旧计划 hash，且没有 P9C/P9A/P9B/G9C/G9A/G9B。
- 实跑 `python -m scripts.build_p0_assets --check` 返回退出码 1，明确报告这两个文件 `DRIFT`。
- `resources/manifests/gate_catalog.json` 当前只登记 G0～G7；P8/G8、P9/G9 另有阶段 verifier 和报告，但新增三阶段尚未明确选择哪一种权威执行/归档机制。
- 计划 `:86` 要求 P9C 登记新 Gate，但 P9C.1～P9C.4 没有独立任务负责当前基线修订、双向追踪和可执行 Gate 注册。

**影响**

新增阶段在 Markdown 中逻辑成立，但另一个执行者无法仅依靠当前治理资产稳定回答“按哪个设计/计划 hash 执行、怎样运行 G9C/G9A/G9B、证据归档在哪里”。后续 P14 的全 Gate 汇总也会遇到两个事实源。

**最小修正**

把 P9C 的第一个动作定义为“前向基线增补”，可以命名为 P9C.0，也可以并入 P9C.1，但必须完成：

1. 保留 P0/G0 和 P5～P9 历史报告、时间戳、结论和旧 hash，不伪造历史重验。
2. 生成一个当前生效、内容寻址的设计/计划 baseline revision，并让追踪矩阵包含 P9C/P9A/P9B。
3. 为 G9C/G9A/G9B 选择**唯一**执行机制：要么进入统一 Gate Catalog，要么沿用 P8/P9 的阶段 verifier；不要同时维护两套互相漂移的真相源。
4. 固定每个新 Gate 的命令、机器可读 evidence 路径、报告路径、输入 baseline hash 和退出码语义。
5. 对本地/被忽略的 spike 审计材料只登记相对路径、内容 hash 和必需前置条件；缺失时 fail fast，不把大体积运行目录复制进生产包。

**关闭条件**

- 当前 baseline 校验不再漂移；
- 新增阶段/测试/Gate 可双向追踪；
- G9C/G9A/G9B 各有一条可执行、可归档、非人工口述的权威路径；
- P5～P9 历史文件 hash 变化为 0。

### F-02｜P0 / G9C blocker｜SemanticUnitMap 缺少独立分母及 KEEP_SOURCE 授权源

**证据**

- 设计 `:1334-1344` 和计划 `:2127-2153` 正确要求 SemanticUnitMap 覆盖整页全部可翻译原生文字。
- 但当前文字是由各 leaf 私有分组后投影为 Map。如果 leaf 漏掉一个文本对象，再对自己生成的较小集合做 100% 对账，仍可能得到假完整。
- toolbox spike 的公共 Bundle 只验证非空文本、ID 唯一及顺序一致（`spikes/page_toolbox_engine_puncture_v1/src/page_toolbox_puncture/contracts.py:193-222`），不能证明整页分母完整。
- SharedPdfKernel 已能提供稳定文本对象及 object ID（`spikes/page_toolbox_engine_puncture_v1/src/shared_pdf_kernel/facts.py:45-89`），可直接作为独立盘点来源，无需再造通用语义分组器。
- 文档允许“带枚举原因的 KEEP_SOURCE”，但没有明确 Provider 是否可以自行声明该豁免。若可以，模型可把任意原文照抄包装为合法 KEEP_SOURCE。

**最小修正**

1. 在 leaf 之前，由 Kernel/统一 PageFacts 冻结 `PageTextInventory`，或直接把现有稳定 text object 清单作为独立分母。
2. 每个 text object 必须恰好对账为以下之一：`TRANSLATE(unit_id)`、预授权 `KEEP_SOURCE(reason_code)`、`PROTECTED(reason_code)`、`NON_TRANSLATABLE(reason_code)`。
3. leaf 仍负责段落、cell、block 等私有分组，只负责把已盘点对象映射到 unit；不要建设万能语义分组器。
4. `allowed_dispositions` 和 KEEP_SOURCE reason 必须在调用 TranslationPort 前由确定性事实/Toolbox 冻结。Provider 只能返回与预授权相符的 disposition，不能自行授予 KEEP_SOURCE。
5. G9C 增加两个反例：故意让 leaf 漏一个 Kernel text object；让 Provider 对未授权 unit 返回原文和 KEEP_SOURCE reason。两者都必须 FAIL，且 Layout 调用数为 0。

**关闭条件**

Kernel 文本盘点到最终 disposition 的独立覆盖率为 100%；漏项、重复归属、Provider 自授权 KEEP_SOURCE 和未通过完整性门禁进入 Layout 均为 0。

### F-03｜P1 / G9A blocker｜DocumentLayoutMemory 输入 seam 未闭合，容易形成依赖环和重复页级事实

**证据**

- 页面在进入 `PageToolbox.prepare/build_template` 时已经要携带 `DocumentLayoutMemoryRef`（总体设计 `:913-939`）。
- P9A Builder 又要求消费 table/cell/anchor/owner 等 G8/G9 叶级事实（计划 `:2274-2290`）。
- 当前生产 `PageToolbox` 只有 `prepare -> build_translation_request -> consume_translation_bundle -> render -> judge -> repair`，没有一个“不依赖 document memory、只产布局事实”的前置接口（`src/transflow/toolboxes/contracts.py:221-274`）。
- 设计 `:1240-1249` 还要求 DocumentLayoutMemory 保存大量表格和视觉页内事实，这会与 SharedPdfKernel PageFacts、leaf template/owner 清单形成第二份事实源。

**影响**

如果不先明确该 seam，实现时很容易二选一：在没有 memory 时提前调用本来依赖 memory 的 leaf，形成环；或在 P9A 新写一套通用 table/anchor/owner 解析器，形成过度设计和双事实源。

**最小修正**

V0.1 保留 `DocumentLayoutMemory` 名称，但把内容收窄为真正的文档级只读快照：

- source hash、页数、每页 geometry/route/PageFacts ref；
- 重复页眉页脚、页码、shared margin/protected region；
- 有样本数/provenance/confidence 的角色级字体、字号、行距、段距分布；
- 目标语言字体回退和受控调整范围；
- config/font/classifier/catalog/kernel/builder/schema hash。

table cell、anchor、owner、页内 reading order 等继续由 PageFacts/leaf 私有合同持有，DocumentLayoutMemory 只引用其 hash，不复制内容。若真实消费者证明必须在屏障前聚合 leaf 事实，只增加一个窄的只读 `collect_layout_facts(route, raw_facts)` seam；该 seam 不得调用 Translation、render、judge、repair，也不得依赖 DocumentLayoutMemory。

P9A 只证明一次构建、不可变、内容寻址、恢复后同 hash；线程/进程池的吞吐和故障矩阵留给 P13，避免提前实现后续并发阶段。

**关闭条件**

Builder 输入图无环；文档级字段都有至少一个 P9B/P10 消费者；页级事实只有一个权威来源；不需要为 P9A 新建通用 leaf 解析器。

### F-04｜P0 / G9B blocker｜Repair 的改善比较和候选物化失败没有闭合合同

**证据**

- 设计 `:1307-1317`、`:1773-1785` 和计划 `:2422-2452` 要求“改善且无硬回归才接受”，但没有冻结不同质量维度的方向、优先级、精度/epsilon、相等和 tie 语义。
- spike 公共 `RepairController` 以单一数值下降判改善（`spikes/page_toolbox_engine_puncture_v1/src/shared_pdf_kernel/repair.py:17-46`）；multi typography 实现却允许从一种失败变成另一种失败并记为 `ACCEPTED_NEW_FAILURE`（`spikes/page_toolbox_engine_puncture_v1/toolboxes/body/flow_text/multi/tools/orchestrator/typography_repair_loop.py:83-95`）。直接统一封装会得到不一致的接受语义。
- 设计要求每个实际轮次先产生可打开候选，但 Attempt 结果只有 `ACCEPTED | ROLLED_BACK | REJECTED | SKIPPED_DUPLICATE`。真实 PDF 运行仍可能在字体、PyMuPDF 写入、子进程或磁盘阶段无法物化候选。

**最小修正**

1. 不造跨 leaf 的万能质量总分。由 leaf 的版本化 Judge 合同提供 `compare(before, after, target_finding)`，明确硬拒绝项、目标指标方向、epsilon/tie 和允许的软指标变化；RepairCoordinator 只执行统一的 ACCEPT/ROLLBACK。
2. 禁止 `ACCEPTED_NEW_FAILURE` 直接进入产品合同；新 Finding 只有在明确的 leaf comparator 判定不构成硬回归时才可能接受，否则回滚。
3. 区分“提案预检未通过”和“实际 RepairAttempt”：未执行动作只记 proposal audit，不消耗实际渲染轮次；一旦选择并执行动作，就必须形成唯一终态 Attempt。
4. 增加 `MATERIALIZATION_FAILED`（或等价枚举）：保存 action、输入 state hash、结构化错误、已产生的安全证据，消耗该轮预算并进入下一确定动作或 fallback。
5. Gate 改成“每个已执行动作都有唯一终态记录”；只有成功物化的 Attempt 要求可打开候选/PNG/Patch 证据 100%，物化失败要求结构化失败证据 100%。不能用一条日志冒充候选成功。
6. 第一阶段本地恢复使用现有 `expected_version` 加明确的 `writer_epoch/recovery_generation`（若确需多 Worker fencing）；不要提前借用 MerqFin 数据库租约语义。完整并发 fencing 在 P12/P13/P16/P17 对应阶段再扩展。

**关闭条件**

每个 leaf 的比较器有版本和反例；同一输入决定确定；硬回归发布为 0；物化失败可恢复/可降级且不伪造候选；恢复后同一 action 不重复执行。

### F-05｜P0 / 范围 blocker｜P9B 的 Repair LLM 与 V1 决策冲突，跨运行学习不应阻断打通

**证据**

- 总体设计 `:2405-2431` 明确：V1 唯一启用的模型判断节点是 ClassificationDecision；Toolbox Repair 保持确定性，未来模型选择 Repair 需要独立设计和 Gate。
- 同一设计 `:1280-1301` 又允许 Rule IR 的 `optional llm_question + allowed_verdicts`。
- 计划把模型问题写成 P9B 必测项（`:2393`、`:2415`、`:2449`）并进入 G9B 硬 Gate，形成直接矛盾。
- P9B 还把跨新 run 的 `PriorRepairEvidenceRef`、通用 `mechanical_when` Rule IR、两文档 + holdout 规则晋级做成 P10 的硬依赖。这些提高复跑效率或规则治理质量，但不是首次完整 PDF 闭环的必要条件，两个 spike 也没有给出统一跨叶 Rule DSL/跨运行复用的已晋级生产证据。

**最小修正**

V0.1 的 P9B 只保留：

- 当前 run 的 append-only `PageRepairMemory`；
- candidate-0 和每轮一个未试确定性动作；
- action/state 去重、默认 3/2 停止预算；
- 从不可变源页和同一完整 TranslationBundle 重建；
- leaf comparator、接受/回滚、Checkpoint、恢复和确定性 fallback；
- 静态只读的 leaf rule/action 索引及版本 hash，实际 predicate 仍由 leaf Python 实现。

以下能力从 G9B/P10 硬依赖移到 V0.1 打通后的增强 backlog：

- Repair 模型节点及 `optional llm_question`；V0.1 Gate 应要求 Repair 模型调用数为 0；
- 跨新 run `PriorRepairEvidenceRef` 自动排除；
- 通用可执行 `mechanical_when` DSL；
- 跨文档规则自动/半自动晋级和“两文档 + holdout”发布流程。

Schema 如需兼容未来版本，可以保留明确标为 dormant/unsupported 的字段，但不能在 V0.1 运行或硬 Gate 中启用。

**关闭条件**

设计和计划对 V1 模型节点的说法唯一；P10 只依赖同 run Repair 闭环；移除上述增强能力后，首次运行、崩溃恢复、失败降级和最终 PDF 完整性不受影响。

## 4. V0.1 最小保留面

| 部分 | 必须保留 | 本版收窄/后移 |
|---|---|---|
| P9C | Kernel 独立文字分母、SemanticUnitMap、可信 disposition、CompletenessDecision、失败 unit 有界重译、final/diagnostic 双轨、Route mismatch、三类结果 | 不增加万能语义分组器；不把人工观感替代机器完整性 |
| P9A | 全页事实/Route/公共边缘屏障、一次构建、不可变 hash、跨页角色软分布、目标字体/间距政策、Checkpoint ref | 不复制 table/cell/anchor/owner；并发吞吐和故障注入后移到 P13 |
| P9B | 同 run PageRepairMemory、一个动作/轮、真实候选或物化失败终态、leaf comparator、action/state 去重、3/2 预算、回滚、恢复、fallback | Repair LLM、跨 run PriorRef、通用 Rule DSL、跨文档晋级后移 |
| Harness | 显式 Document/Page/Repair 状态、Port/Adapter、内容寻址 Artifact、单一 Checkpoint 语义、串行 Finalizer | 不引入通用 Agent/Workflow 框架，不复制 MerqFin JobRunner 状态机 |
| PDF | 源 PDF 副本回放 typed Patch、对象/几何/字体/Preservation 硬校验、失败仍保留完整页序 | 不从旧 candidate PDF diff 反推权威 Patch，不拼接候选页发布 |

## 5. 建议的施工顺序与停止线

顺序不改，只在每阶段前增加明确停止线：

1. **P9C.0/P9C.1：** 当前 baseline、追踪和 Gate 执行路径闭合；否则不开始运行时合同施工。
2. **P9C.2：** 独立 PageTextInventory 与预授权 disposition 通过反例；否则不允许 TranslationBundle 进入 Layout。
3. **P9C.3/P9C.4：** final/diagnostic 隔离、Route mismatch 和三类结果完成真实 PDF 回归；否则不进入 P9A。
4. **P9A：** 先冻结最小 DocumentLayoutMemory 字段和无环输入图；某字段没有真实消费者则不加入 V0.1 Schema。
5. **P9B：** 先冻结 leaf comparator、Attempt 终态和同 run 恢复，再接一个已穿刺 leaf 跑通 candidate-0 → repair → accept/rollback/fallback；不要先做 Registry 晋级。
6. **P10：** 复合叶继续逐 leaf Lift-and-Wrap。旧 leaf 若只返回 candidate PDF，必须在其既有 layout/renderer seam 显式导出 typed Patch operations；不得用 PDF diff 反推 Patch，也不得因目录存在就启用 leaf。

## 6. 最终裁决

- **架构方向：通过。** Harness 与有限 Loop 的边界符合当前技术证据和产品目标。
- **阶段顺序：通过。** P9C/P9A/P9B 插入位置合理，不建议删除、合并或改序。
- **按当前文本直接施工：不通过。** F-01、F-02、F-04、F-05 需要先形成批准后的唯一合同；F-03 必须在 G9A 前收窄，否则很容易产生第二套页级事实系统。
- **推荐动作：小修订，不重做设计。** 主流程、类型边界和 Gate 思想均保留，只补可信分母、无环数据边界、Repair 终态，并把跨运行学习/Repair LLM 后移。

完成上述最小修订后，这两份文档可以作为“先打通、后优化”的 v0.1 施工基线。
