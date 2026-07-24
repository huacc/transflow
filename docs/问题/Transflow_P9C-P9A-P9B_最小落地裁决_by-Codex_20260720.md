# Transflow P9C / P9A / P9B 与 Harness + Loop Engineering 最小落地评审

> 评审日期：2026-07-20  
> 评审范围：总体设计 v0.1、详细开发计划 v0.1、`page_classification_engine_puncture_v1`、`page_toolbox_engine_puncture_v1`  
> 评审目标：判断新增阶段及引擎设计是否适合在“先打通、不过度优化”的 V1 中工程化落地  
> 最终结论：**有条件通过；不需要推倒重做，也不建议改阶段名称或引入通用 Agent 框架。**

## 1. 评审前提

1. 本轮只评审当前文件快照，不改写总体设计、详细计划、历史 Gate 报告或两个 spike。
2. `P9C → P9A → P9B → P10` 是计划明确规定的执行顺序；这里按语义依赖判断，不按字母排序。
3. V1 的“打通”是：真实 PDF 可完成、可恢复、可审计、可安全降级；不等于所有 Toolbox 已达到产品质量或正式晋级。
4. spike 的代码与清单是迁移事实来源，但 spike 中存在的目录或实现不能自动等价为生产启用证据。

本次读取的两个文档 SHA-256：

- 详细计划：`9DA35EB8DD6DAA48C8F1AA64BFCE477DC6D456000B972A740E3571D55E6251BC`
- 总体设计：`4CA31F883EBCC024A5321841318389F0D755A6AB609C35DF7E3A8EF8B622A191`

## 2. 总体裁决

新增三阶段的逻辑是成立的，而且放在 P9 后、P10 前是当前迁移历史下最稳妥的位置：

1. **P9C 先补正确性合同**：先回答“是否真的完整翻译、失败产物是什么、工程完成与产品通过是否相同”。否则后续 Repair 只会更高效地修一个可能缺字、错路由或伪成功的候选。
2. **P9A 再冻结文档级只读上下文**：让并发页面共享同一份跨页事实和目标政策，避免先完成的页面污染后完成页面。
3. **P9B 最后建立页级有界修复闭环**：Repair 才能在固定源页、固定译文、固定文档上下文上做可比较的单变量尝试。

如果这是从零开始的新项目，P9C 的语义完整性合同应更早出现；但当前 P0–P9 已有历史证据，采用“保留历史、从 G9C 起前向收紧”的纠偏阶段，比回写旧 Gate 更诚实，也更低风险。

因此不建议：

- 因名称顺序为 `9C → 9A → 9B` 而重命名阶段；明确依赖并让工具识别即可。
- 合并三个阶段为一个大阶段；这样会失去“正确性合同 → 稳定上下文 → 修复闭环”的故障隔离。
- 引入 LangGraph、CrewAI、通用工作流 DSL 或多 Agent 协作层；当前显式 Python 状态机和窄 Runner 足够。

## 3. 两个 spike 实际证明了什么

### 3.1 分类 spike

分类 spike 已证明分层 Route 合同、规则优先、一次主判/冲突复核及确定性 `body.freeform` fallback 可以工作。当前计划也诚实登记了深度审计口径：457 页中 424 正确、30 错误、3 歧义，且不把它冒充原 709 页全集。

它没有证明“分类永不出错”。产品化时必须继续保留：

- `ROUTE_CAPABILITY_MISMATCH` 和安全 fallback；
- 离线纠正分类、版本化发布，运行时 Toolbox 不热改 Route/Catalog；
- `sample_id`、gold label 和带答案 exemplar 只属于测试/评估，不能进入生产模型证据。

### 3.2 Toolbox spike

实际存在 14 份叶级 `stage_gate.json` 和 14 份 `toolbox_manifest.json`，但没有 `promotion_manifest.json`。当前真实结论不是“全部穿刺成功”：

- `body.flow_text.single`：`PASS`；
- `body.chart`、`body.diagram`：`PASS_NON_BLIND`；
- `body.flow_text.multi`、`body.table`：`NOT_EVALUATED`；
- `body.flow_text.visual_anchored`、`body.composite.anchored_blocks_chart`：`FAIL`；
- 其余已登记叶多为 `EVIDENCE_INSUFFICIENT`；`flow_text_table` 还没有同级根 Gate/manifest。

Toolbox spike 已提供本设计需要的种子能力：类型化翻译单元/Bundle、Shared PDF Kernel、确定性 RepairController、叶私有 Judge、动作去重、状态哈希与循环检测。它没有证明文档级编排、跨页只读上下文、统一语义完整性、跨 run 学习或正式产品晋级。

因此工程化的正确方式是 **Lift-and-Wrap + 默认禁用未过 Gate 的叶 + 确定性降级**，而不是重写已穿刺算法，也不是把所有目录批量置为可用。

## 4. Harness 设计评审

当前 Harness 方向符合 V1 最佳实践：

- 显式状态机拥有控制流，模型只通过窄端口返回结构化决定；
- Domain / Application / Port / Adapter 边界清楚，PDF、模型、存储、Checkpoint 副作用可替换测试；
- Artifact 内容寻址、Checkpoint、租约/CAS 和 source-copy Patch 回放，使恢复与最终化可审计；
- Gate Harness 与生产 Runtime 分离，不把测试编排器放进线上主循环；
- 页面可有界并行，但最终 Patch 串行回放，避免并发修改同一 PDF；
- 失败仍进入明确终态，源文透传只证明工程闭环，不冒充翻译成功。

对 V1 而言，这已经足够。不要再增加一个通用 Workflow Engine、事件溯源平台、向量记忆库或动态插件编排层。

## 5. Repair Loop 设计评审

核心循环是合理的：

1. 固定不可变源页、同一个已通过完整性检查的 `TranslationBundle` 和冻结的文档上下文；
2. 生成 candidate-0 并由对应叶 Judge 给出 Finding；
3. 每轮只改变一个主要修复维度；
4. 从源页重新物化候选，不在上轮候选上累计不可追踪修改；
5. 硬约束无回归且质量确有改善才接受，否则回滚；
6. action hash、state hash 去重，默认最多 3 轮、连续 2 轮无改善停止；
7. 每次已提交尝试写入 PageRepairMemory/Checkpoint，恢复后不重复执行。

这与 Toolbox spike 中已有的 RepairController、typography loop、attempted-action/state-cycle 机制一致，属于工程封装而不是另造算法。

## 6. 正式进入 G9C 前必须收口的五个最小问题

### S0-1：现有治理工具不识别带字母阶段

**现状证据：**

- 计划文档自身机械闭合：27 个阶段、27 个 Gate、779 个唯一测试 ID、0 重复、1175 个精确引用且 0 悬空。
- `scripts/build_p0_assets.py` 仍以 `P\d+\.\d+`、`G\d+-\d+` 解析，静默漏掉 P9C/P9A/P9B 与 G9C/G9A/G9B。
- `scripts/verify_p0.py` 仍硬编码 P1–P14 顺序，实际报 `P1_P14_STAGE_SEQUENCE_INVALID` 和 P9A/P9B 依赖错误。
- `resources/manifests/gate_catalog.json` 当前只登记 G0–G7。
- `python -m scripts.build_p0_assets --check` 实际报 baseline/traceability drift；`python -m scripts.verify_p0 all` 也失败。因此本报告不宣称任何新 Gate PASS。

**最小整改：**

1. 只扩展阶段/Test/Gate ID parser，使其识别 `P9C/P9A/P9B`；不要重写追踪系统。
2. 排期校验改为读取 schedule 中的显式顺序和依赖，不再推导“G(n-1)”。
3. 选择一个现有 Gate 执行入口，登记 G8/G9/G9C/G9A/G9B；不要并存第二套 Gate 框架。
4. 前向刷新 current baseline/traceability；历史 P5–P9 报告与旧 Gate 文件保持不变。

### S0-2：SemanticUnitMap 的“分母独立性”还需写死

**风险：** 当前合同由 Toolbox/Recovery 构建 SemanticUnitMap。如果同一个叶既决定“有哪些文字”，又证明“这些文字已全部覆盖”，叶漏提取的文字会同时从分子和分母消失，完整性仍可能假 PASS。`KEEP_SOURCE` 若只凭 Provider 返回一个 reason 也会成为绕过翻译的自授权通道。

**最小整改：**

- 不新增服务或复杂抽象；在 Kernel `PageFacts` 中冻结一个最薄的 `PageTextInventory`（稳定对象 ID、原文 hash、owner/protected 候选）。
- Toolbox 只负责把 inventory 中可翻译对象分组/映射为 unit；Completeness 独立核对 inventory → unit 的唯一覆盖。
- 合法 `KEEP_SOURCE` 必须由冻结 map 中的 required-literal/policy 预授权；Provider 只能引用该授权，不能临时创造授权。

这样保留各叶私有分组算法，同时真正建立独立分母。

### S0-3：P9A 的 DocumentLayoutMemory 范围过宽，存在重复事实与输入环风险

**风险：** 当前设计把 table row/column/cell、owner、anchor/slot 等细粒度页事实也写入 DLM，同时页面 Toolbox 又消费 DLM。若这些事实由叶级 provider 产生，就可能形成“先构建 DLM 才能运行 Toolbox、先运行 Toolbox 才有 DLM 输入”的环；即使无环，也会让 PageFacts 和 DLM 出现两个事实源。

**最小整改：** P9A 首版只保存真正跨页且已有消费者的快照：

- source/page geometry/hash、完整 Route 快照、公共页眉页脚/边缘区；
- 字体/字号/行距/边距等分布、置信度和允许区间；
- 源/目标语言、受控字体范围与 target policy；
- Builder、Catalog、Kernel、字体和配置指纹。

table/cell、单页 owner、anchor/slot、reading order 的完整细节继续以 PageFacts/叶私有合同为唯一事实源；DLM 最多保存跨页聚合或内容寻址引用。只有出现一个明确的跨页消费者后，才增加相应字段。

### S0-4：P9B 必须区分“尝试已终结”和“候选已物化”

**风险：** “每轮实际执行都必须产出候选 PDF/PNG/Patch”在字体不可用、PDF 写入失败、进程崩溃或磁盘错误时不可保证。强行满足会诱导伪造空候选或源副本候选。P9C 已有 `DIAGNOSTIC_MATERIALIZATION_FAILED`，RepairAttempt 应采用同样诚实的失败语义。

**最小整改：**

- 每个已执行 action 必须恰好有一条终态 attempt 记录；
- 只有物化成功的 attempt 强制关联 candidate/PDF/PNG/Patch/Judge 证据；
- 物化失败记录 `MATERIALIZATION_FAILED`（或复用同义枚举）、异常类别、输入/动作 hash 和实际错误证据，不伪造候选；
- 比较器由叶 Judge 定义“硬回归 + 质量向量 + epsilon/tie”规则，Coordinator 只执行通用接受协议，不建立跨叶通用单一分数。

### S0-5：P9B 的 Repair LLM 与总体 V1 模型边界冲突，跨 run 学习应延后

**现状冲突：** RepairRuleRegistry 允许可选 `llm_question`；但总体设计后文又明确 V1 唯一模型判断节点是 `ClassificationDecision`，Toolbox Repair 保持确定性。这两条不能同时作为施工基线。

**最小整改选择：采用更简单的一侧。**

- V1 删除/禁用 Repair 的 `llm_question` 执行路径，仅使用叶私有确定性 proposal、Judge 和固定预算。
- P9B 必做范围只保留同 run 的 `PageRepairMemory`、candidate-0、单动作尝试、去重/防环、Checkpoint/恢复和安全 fallback。
- `RepairRuleRegistry` 可保留空/静态 schema 与 snapshot hash，暂不实现自动抽取、跨文档晋级、匿名 holdout 晋级流水线。
- `PriorRepairEvidenceRef` 和跨 run 排除集属于效率优化；V1 打通不依赖它，延后不会损害正确性。

## 7. 建议冻结的 V1 最小切片

| 阶段 | V1 必做 | 本版明确延后 |
|---|---|---|
| P9C | 独立语义分母、完整性 Gate、定向失败 unit 重译、final/diagnostic 隔离、route mismatch、三轴结论 | 自动质量优化、动态改路由、把诊断候选当产品结果 |
| P9A | 全页屏障、窄 DLM、内容寻址、冻结/只读、恢复 hash 校验 | 完整复制叶私有页事实、跨 run 自适应布局、复杂统计模型 |
| P9B | 同 run PageRepairMemory、确定性单动作 loop、叶 Judge、3/2 预算、去重防环、checkpoint/fallback | Repair LLM、跨文档规则学习/自动晋级、向量库、通用规则 DSL |

## 8. 最终通过条件

完成以下五项即可开始按当前计划施工，无需再次做架构重构：

1. 治理 parser/schedule/Gate catalog 能识别并执行 P9C/P9A/P9B；current baseline 重算通过。
2. SemanticUnitMap 具备独立 inventory 覆盖校验，`KEEP_SOURCE` 不能由 Provider 自授权。
3. DLM 收窄为真正跨页且有消费者的只读快照，不复制叶级事实源。
4. RepairAttempt 对物化失败有诚实终态，叶 Judge 比较协议明确。
5. V1 Repair 明确为确定性；跨 run 学习和 Repair LLM 延后。

除上述五点外，当前 Harness + Loop Engineering 主体可以直接进入“打通优先”的工程化实现。未过 Gate 的叶继续保持 Catalog disabled 和确定性降级，不应阻断整本 PDF 的工程闭环，也不应被包装成产品质量 PASS。

---

评审人：**Codex**  
签署日期：**2026-07-20**
