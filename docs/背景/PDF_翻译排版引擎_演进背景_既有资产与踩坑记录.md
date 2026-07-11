# PDF 翻译排版引擎：演进背景、既有资产与踩坑记录

> 文档状态：新引擎设计的背景依据，不是运行契约
> 修订日期：2026-07-12
> 适用范围：MerqFin PDF 翻译、原页回填、排版修复、质量裁决与工具箱建设
> 核心用途：告诉后续维护者“为什么不能再走旧路”“哪些资产可以复用”“哪些结论已有证据、哪些仍只是候选”

## 1. 一句话背景

本项目不是因为缺少状态机、Prompt、工具注册表或通用工作流而失败；真正反复暴露的问题是：**不同页面结构需要不同的排版知识和修复工具，而旧架构把这些差异压进了一套过度通用的决策与修复链，导致修一类页面时影响另一类页面，最终形成“代码很多、流程很全、产品仍修不好、维护者也改不动”的局面。**

新方向不再把“防止过拟合”理解为“所有页面必须共用同一工具”。新方向追求的是：

```text
可审计的共性合同
+ 按页面类型隔离的工具箱
+ 直接机械证据优先
+ 千问逐节点受限裁决
+ 每个工具箱独立回归和晋升
```

允许工具代码重复。禁止隐藏的跨类型耦合。Locality 比 DRY 更重要。

## 2. 演进时间线

### 2.1 阶段一：v4 通用 Harness

位置：

```text
../translation_layout_harness_engine_spike_v4/
```

v4 建立了较完整的工程骨架：

- `INTAKE → PRODUCE → JUDGE ↔ REPAIR → DECIDE` 状态主线；
- TranslationProvider、KnowledgeProvider 等 Seam；
- 规则 Judge、LLM Judge、静态 Dispatch、Repair Atom、Checkpoint；
- source/candidate 视觉合同、修复接受、回滚和停止策略；
- trace、artifact、gate、运行目录和测试脚本。

这些工作证明了“候选生成、裁决、修复、回滚、终态”必须有确定性合同，也留下了大量可复用机械原语。

但 v4 的核心困难是：页面类型知识、问题域、修复轴和工具能力仍然通过一个大体通用的 Harness 汇合。随着状态、病种、参数口、视觉轴、Repair Atom 和 Gate 增加，理解和修改一次行为需要跨越多个 Module。模型虽然可以研判和分发，但缺少真实、针对性的排版能力时，增加 Prompt 或 Dispatch 不能补出算法。

最新可核查结果：

```text
../translation_layout_harness_engine_spike_v4/runs/59_p9m_document_repair_loop/
```

该轮只有第一轮布局硬约束修复被接受，后续两轮回滚，最终：

```text
closed = false
stop_reason = consecutive_no_improvement
```

这说明 v4 的流程能诚实停止，但没有证明通用修复链能稳定收敛。

### 2.2 阶段二：`pdf_translation_workflow_core` 通用工作流

位置：

```text
pdf_translation_workflow_core/
```

core 的目标是把 Codex 当作执行引擎，通过统一契约、工具分类、Prompt 包、运行模式和反过拟合规则建造可复用工具链。它补强了：

- process/product verdict 分离；
- 语义翻译输入校验；
- workspace 写入边界；
- source structure、role plan、layout plan、candidate generation；
- visual metrics、quality gates、RepairPatch 和 rollback；
- anti-overfit 扫描和 change manifest。

core 还确立了一个重要原则：样本文件名、页码、固定坐标、固定颜色和已知文本不能进入通用逻辑。

问题并不在这些原则本身，而在它们被进一步解释为“一个全局工具链应适配所有页面”。`build_role_plan → build_layout_plan → generate_semantic_backfill → evaluate → repair` 必须同时理解封面、目录、正文、多栏、表格、图表、卡片和复杂混排。随着兼容分支增加，工具的 Interface 接近其 Implementation，成为浅 Module；修复一个页面结构时，其他结构也会走到同一代码路径。

结果是：反过拟合扫描可以 PASS，过程契约可以 PASS，但产品质量仍然 FAIL。**没有样本特例不等于有正确的排版能力。**

### 2.3 阶段三：Round22 表格布局实验

位置：

```text
pdf_translation_workflow_lab/rounds/round22_table_layout/
```

Round22 的贡献：

- 从当前页几何推导表格/相邻网格区域；
- 把同一行跨列文字拆成独立组；
- 对图表标签、注脚和局部卡片限定边界；
- 两阶段“先擦除计划区域、再插入译文”；
- 产出 role plan、layout plan、quality gates、repair plan 和过程审计。

Round22 的真实结论不是“表格工具已完成”，而是：

```text
process_contract_verdict = PASS
product_quality_verdict = FAIL
promotion_status = candidate_only
```

阻塞仍包括 fit、字号下限和局部重叠，Repair 选择存在但没有形成可执行、可闭合的自动修复链。其 `promotion_manifest.json` 已明确要求在回归样本和反过拟合验证后才能晋升。

### 2.4 阶段四：Round25 分层验证

位置：

```text
pdf_translation_workflow_lab/rounds/round25_aia_first20_layered_validation/
```

Round25 在三个案例上跑通了状态机、RepairPatch、重生成、重裁决和回滚：

| 案例数 | Process | Product | Repair |
|---:|---|---|---|
| 3 | 全部 PASS | 全部 FAIL | 全部 `REJECTED_ROLLBACK` |

最有价值的证据是：目标问题 `text_fit_overflow` 被修到 0，但非目标硬问题 `cross_slot_overlap` 从 70 增长到 78，所以修复候选被拒绝。

这证明：

1. “修好了当前指标”不能等于接受修复；
2. 全局或粗粒度 `expand_or_reflow_slot` 不理解邻区、障碍和页面类型；
3. Repair 必须经过“目标改善 + 非目标硬约束不回退”双闸门；
4. 一个通用工具同时改多种结构，回退风险会快速增长。

### 2.5 阶段五：Round26 契约驱动自引擎

位置：

```text
pdf_translation_workflow_lab/rounds/round26_contract_driven_selfengine/
```

Round26 进一步物化了七类决策产物：

```text
evidence_basket
→ quality_signal_ledger
→ problem_domain_buckets
→ triage_result
→ dispatch_result
→ repair_patch
→ repair_acceptance
```

两个 20 页案例的结果都是：

```text
process_contract_verdict = PASS
decision_graph_verdict = PASS
product_quality_verdict = FAIL
terminal_state = S_FAIL_QUALITY
repair = REJECTED_ROLLBACK
```

AIA 案例把 `text_fit_overflow` 从 135 降到 2，却把 `cross_slot_overlap` 从 140 增加到 268。

Round26 证明“把分诊、分发、补丁、回测做得更规范”很有价值，但也再次证明：**决策图正确不代表工具能力正确。缺少 obstacle-aware、页面结构感知的真实修复工具时，Codex 作为引擎无法靠更复杂的 Prompt 把产品修好。**

### 2.6 阶段六：页面画像与分类穿刺

研究文档：

```text
docs/设计/PDF_年报页面画像与千问分类树_语料研究_v0.1.md
```

工程穿刺：

```text
spikes/page_classification_engine_puncture_v1/
```

本阶段改变了问题的切入方式：先判断页面承担什么功能、主要翻译排版权属于什么结构，再把页面交给唯一、专属的工具箱。

当前分类树已经覆盖：

- `cover / contents / body / end / visual_only`；
- `flow_text / table / chart / diagram / anchored_blocks / composite`；
- `single / multi / visual_anchored`；
- 五类已经观察到的 composite；
- `body/freeform` 确定性兜底。

2026-07-11 的 86 页专项回归结果：

| 指标 | 结果 |
|---|---:|
| 总体 | 76/86，88.37% |
| 原本正确页保持 | 30/30，100% |
| 无关页 | 29/30，96.67% |
| 用户指出问题页 | 17/26，65.38% |
| 高置信度直接表格规则 | 15/15，100% |

因此，方向成立但分类尚未完成：

- 高置信度表格直接证据可以进入新引擎基线，但仍需扩大校准集；
- flow_text/table/composite、chart/anchored_blocks 等边界仍需继续穿刺；
- `freeform` 必须保留为可观察缺口，不能掩盖成成功；
- 分类器可以迁移为版本化 Module，不能宣称当前 Prompt 已经达到生产准确率。

## 3. 血泪史：已经被证据否定的做法

### 3.1 把“防过拟合”等同于“所有页面共用一套工具”

错误表现：

- 以通用 `role/layout/repair` 逻辑同时处理表格、正文、图表和卡片；
- 为兼容新页面不断向共享工具添加条件；
- 改一处后必须回归所有页面，而且很难知道变化来自哪里。

结论：真正应避免的是样本身份特例，不是页面类型特化。页面类型特化是领域事实，不是过拟合。

### 3.2 先抽象工具，再确认页面结构

错误表现：

- 一个 `expand_slot` 同时面对正文流、表格单元格、卡片和图表标签；
- 工具不知道自己的固定对象、邻区、阅读顺序和允许修改范围；
- Interface 暴露大量可调参数，调用者仍需理解全部实现细节。

结论：先建立 leaf，再在 leaf 内设计工具。工具的适用范围属于 Interface，不是运行时让模型猜。

### 3.3 让 LLM 开放式选择工具

错误表现：

- Prompt 同时判断页面、问题、工具、参数、修复和终态；
- 模型需要在缺少直接证据时猜工具；
- 工具缺失时模型仍会给出看似合理的修复计划。

结论：LLM 只在当前分类节点或当前 leaf 允许的有限选项内裁决。工具选择由 leaf 和静态映射决定。

### 3.4 用更多 Prompt 弥补缺失工具

Round25/26 已证明：分诊可以正确、Dispatch 可以正确、RepairPatch 可以执行，但一个不读障碍和邻区的扩框工具仍然会产生更多重叠。

结论：产品问题缺少机械能力时，必须返回能力不足并到 lab 建工具，不能继续堆 Prompt。

### 3.5 把过程 PASS 当成产品 PASS

Round22、Round25、Round26 都出现了 `process PASS + product FAIL`。候选 PDF 存在、状态机完整、证据齐全，只说明流程可信，不说明排版合格。

结论：candidate 永远不是 accepted。只有过程和产品双 PASS 才能交付。

### 3.6 修目标指标，不回测其他硬约束

Round25/26 的共同失败：溢出明显减少，重叠明显增加。

结论：Repair 接受必须同时满足：

```text
目标 finding 改善
AND 页面硬约束全过
AND 非目标 blocking finding 不回退
AND 当前 leaf 的局部 Judge 通过
```

### 3.7 过早建立全局问题域和全局 Repair Atom

文字溢出在正文页、表格页和卡片页不是同一个问题：

- 正文页可能允许同阅读流内回流；
- 表格页只能在单元格内适配；
- 卡片页禁止跨块；
- composite 还要保证区域间零影响。

结论：failure code 可以同名，但 Repair handler、参数、接受条件和测试应归当前 PageToolbox 私有所有。

### 3.8 把分类不确定页塞进正文或 freeform 并继续执行

这会让错误工具箱接管页面，并把分类问题伪装成排版问题。

结论：`body/freeform` 是分类树缺口的可视化兜底，不是自动成功路径。没有经过验证的工具箱时应返回能力不足。

### 3.9 把图片内文字当作可编辑文字

千问多模态能看见图片内文字，不代表 PDF 工具能安全移除和回填。

结论：图片可用于分类；图片像素和图片内部内容不翻译、不重排、不覆盖。执行文字清单只来自可安全绑定的 PDF 原生文字对象。

### 3.10 一开始就追求全量页面闭环

全量页面把分类错误、翻译问题、文字移除问题、工具缺失、排版失败和质量裁决混在一起，无法定位能力缺口。

结论：先分类穿刺，再按 leaf 做单页 tracer，再做文档级组合。每次只证明一个窄能力。

## 4. 已锁定的新方法论

1. 整份 PDF 由文档级 Engine 拆页、汇总和合并；页级 Pipeline 保持单页 Locality。
2. 页面先分类，再绑定唯一 PageToolbox；不得从全局工具池开放选工具。
3. 分类一次只判断一个维度。
4. 直接机械证据可以在经过校准的 proposition 上直接裁决；证据不足才进入规则/千问双路。
5. 千问只消费匿名截图、当前节点允许值和可审计证据，不读取文件名、人工标签或工具实现。
6. 图片内部内容保持原样。
7. `DocumentStyleMemory` 只在当前文档运行中保持正文样式一致，终态删除。
8. 每个 leaf 私有拥有 tools、Judge、Repair、Prompt、阈值和回归集。
9. 相似代码可以复制；只有无 PageType 语义的 PDF 机械函数才共享。
10. 修改一个 leaf 必须运行目标 leaf 正向集和非目标 leaf 哨兵集。
11. 候选不等于产品；只有双 PASS 才产生 accepted PDF。
12. 工具缺失必须暴露成 capability failure，不能由模型“规划出成功”。

## 5. 既有资产处置矩阵

### 5.1 可以优先迁移或改写的机械资产

| 来源 | 资产 | 建议处置 | 迁入位置 | 原因 |
|---|---|---|---|---|
| core | `tool_probe.py` | 改写迁移 | `pdf_kernel/runtime_probe.py` | 纯环境能力探测，无页面类型语义 |
| core | `extract_pdf_structure.py` | 缩小 DTO 后改写 | `analysis/facts_extractor.py` | 字体、bbox、drawings、images 是 PageFacts 基础；删除旧 `page_type_guess` 权威性 |
| core | `render_pdf.py` | 迁移 | `pdf_kernel/render.py` | 源页/候选页渲染是通用机械能力 |
| core | `render_source_output_crop.py` | 改写迁移 | `quality/evidence_crop.py` | 为当前 leaf Judge 生成局部对比证据 |
| core | `validate_workspace_boundary.py` | 迁移 | `runtime/path_guard.py` | 防止运行产物逃逸工作区 |
| core | translation unit 覆盖、重复、protected token 校验 | 缩小后迁移 | `translation/validation.py` | TranslationProvider 必须一一对应，不依赖页面类型 |
| core | `validate_process_artifacts.py` | 按新状态重写 | `runtime/process_validator.py` | 保留“缺 trace/越界写即流程失败”原则 |
| core | source/candidate object hash、render diff 思路 | 拆分迁移 | `quality/constraint_judge.py` | 验证 locked objects、颜色和页几何不变 |
| v4 | `orchestrator/models.py` 中 immutable DTO 思路 | 重新定义，不复制大合同 | `contracts.py` | 保留 frozen contract、typed verdict、artifact ref |
| v4 | `orchestrator/transitions.py` 的合法迁移校验 | 按中文状态重写 | `runtime/state_machine.py` | 状态迁移必须确定性 |
| v4 | Dispatch 参数口、mutable/detection fact 区分 | 下沉到 leaf 私有 | `page_tree/<leaf>/repair.py` | 禁止模型修改检测事实 |
| v4 | source/candidate visual contract | 拆成硬约束与 leaf 指标 | `quality/` + leaf `judge.py` | 同一证据模型比较源页和候选页 |
| v4/core | Repair 接受、回滚、best candidate | 迁移原则，重写合同 | `quality/repair_coordinator.py` | Round25/26 已证明双闸门必要 |
| v4/core | artifact hash、trace、decision log | 简化迁移 | `runtime/run_ledger.py` | 支持恢复和审计 |

### 5.2 只能按 leaf 提炼、不能整包迁移的资产

| 来源 | 资产 | 为什么不能直接迁移 | 正确提炼方式 |
|---|---|---|---|
| core | `build_role_plan.py` | 角色体系覆盖所有页面，分支过多 | 在每个 PageToolbox 内只保留本类型需要的容器角色 |
| core | `build_layout_plan.py` | 同时处理流式正文、网格、色块和障碍 | 将表格网格推导移到 table，将正文流移到 flow_text，将块隔离移到 anchored_blocks |
| core | `generate_semantic_backfill.py` | 全局 generator 承担擦除、fit、fallback 和多种页面策略 | 提炼 PDF 写入机械原语；具体 plan 必须由 leaf 产生 |
| core | `collect_visual_region_metrics.py` | 指标混合多个页面角色 | 拆成 ConstraintJudge 公共硬指标和 leaf 私有指标 |
| core | `evaluate_pdf_quality.py` | 一个总 gate 不知道 leaf 的不变量 | QualityController 只归约 typed Finding；Finding 由公共 Judge 和 leaf Judge 分别产出 |
| core | layout profiles | 文档方向有价值，但不能代替页面结构 | 作为字体 fallback、语言方向和样式边界输入，不直接决定 leaf 工具 |
| v4 | `repair_relayout_slot` / `repair_text_fit_page` | 主要是缩字号或通用槽修复，部分实现仍标记 mock/deferred | 分别在 flow_text/table/anchored_blocks 内重写，并使用不同约束 |
| v4 | visual repair axes | 可作为候选观察量，但不是所有 leaf 共用的修复轴 | 由每个 leaf 选择真正有用的指标和接受条件 |
| Round22 | table cell split/header binding/obstacle pack | promotion 仍为 candidate_only | 在 `body.table` 和 `flow_text_table` 单页样本中重新穿刺后迁移 |
| 分类穿刺 | evidence/rules/resolver/Qwen 协议 | 当前整体准确率 88.37%，仍有明确误判 | 迁移 Module 结构和 trace 合同；阈值、Prompt 和规则继续版本化校准 |

### 5.3 明确不迁移的架构

- v4 的全局病种→全局工具→全局 Repair Loop 主干；
- core 的万能 role/layout/generator 运行主干；
- 全局 ToolRegistry 和让模型开放式选择工具的 Interface；
- 一个 Prompt 同时判断页面类型、问题域、工具、参数和终态；
- 以“反过拟合”为理由禁止 PageType 私有工具；
- sample-specific 分支、固定页码、文件名、公司名、固定坐标和固定颜色；
- mock checkpoint 结果作为产品能力证据；
- candidate PDF 作为交付件；
- 未通过 leaf 回归和非目标哨兵验证的 promotion。

## 6. 工具晋升规则

任何旧工具或 lab 新工具进入正式引擎前，必须提交一份 leaf-local promotion record：

```text
asset_id
source_path
target_leaf
supported_page_features
input_contract
output_contract
invariants
known_limitations
positive_samples
negative_sentinels
before_metrics
after_metrics
non_target_regression
promotion_verdict
reviewer
version
```

晋升最小门槛：

1. 不读取样本身份；
2. 只修改当前 leaf 允许的 TextContainer；
3. 目标 finding 改善；
4. locked objects 和非目标 blocking finding 不回退；
5. 目标 leaf 正向样本通过；
6. 非目标 leaf 哨兵不变；
7. 有明确失败语义，不用 fallback 伪造成功；
8. 产物和版本可追溯。

## 7. 对后续 Codex 的工作约束

当后续 Codex 研究或实现一个工具时：

1. 先声明目标 leaf 和具体失败；
2. 只读取目标穿刺目录、明确允许的旧资产和当前样本；
3. 先生成直接证据和失败基线；
4. 工具必须在当前 PageToolbox 内调用；
5. 不允许为了一个样本修改父级分类器、公共 renderer 或其他 leaf；
6. 若必须修改公共机械原语，要运行所有 leaf 哨兵；
7. 如果缺少真实算法，返回 capability missing；
8. candidate、process PASS、product PASS 必须分别陈述；
9. 报告必须给出实际 PDF、证据、Finding、verdict 和失败边界；
10. 只有明确批准后才能把 spike 资产复制进正式引擎。

## 8. 证据索引

| 主题 | 直接证据 |
|---|---|
| v4 不能收敛 | `../translation_layout_harness_engine_spike_v4/runs/59_p9m_document_repair_loop/00_result_index.md`、`document_repair_loop_summary.json` |
| Round22 candidate-only | `pdf_translation_workflow_lab/rounds/round22_table_layout/reports/round22_experiment_report.md`、`promotion/promotion_manifest.json` |
| Round25 三案例全部质量失败 | `pdf_translation_workflow_lab/rounds/round25_aia_first20_layered_validation/reports/round25_batch_summary.md` |
| Round25 修溢出但重叠回退 | 同目录 `round25_final_verdict.json`、`repair_loop_0001.json` |
| Round26 决策图通过但产品失败 | `pdf_translation_workflow_lab/rounds/round26_contract_driven_selfengine/reports/round26_execution_audit.md`、`round26_final_verdict.json` |
| Round26 AIA 135→2 / 140→268 | 同目录 `round26_contract_driven_selfengine_report.md`、`repair_acceptance.json` |
| 年报语料画像 | `docs/设计/PDF_年报页面画像与千问分类树_语料研究_v0.1.md` |
| 当前分类合同 | `spikes/page_classification_engine_puncture_v1/README.md`、`src/page_classifier/config.py` |
| 86 页专项回归 | `spikes/page_classification_engine_puncture_v1/reports/runs/run-20260711T154941Z/无边框表格规则回归报告.md` |

## 9. 最终结论

旧项目不是“全部错误”，它们提供了大量有价值的合同、机械工具、审计产物和失败证据。真正需要放弃的是它们共同形成的一个隐含假设：**只要工作流足够通用、Prompt 足够详细、状态机足够完整，模型就能选择工具并修好所有页面。**

新引擎应保留共性合同的 Depth，把页面知识下沉到专属 PageToolbox，从而获得 Locality：

```text
公共层稳定：拆页、PageFacts、翻译 Interface、PDF 机械写入、硬约束、账本、合并

页面层可复制：分类叶子、模板、工具、Judge、Repair、Prompt、阈值、测试
```

以后增加页面类型或修复能力，不再修改一个万能引擎；只增加一个分类分支、一个工具箱和它自己的证据与回归。
