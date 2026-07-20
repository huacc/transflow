# Round26 执行指令：契约驱动的自引擎 PDF 翻译与排版

版本：v1.0
下达对象：Codex（自己当引擎 + 自己当裁决 LLM）
状态：待执行

---

## 0. 一句话目标

你（Codex）自己作为引擎和裁决模型，**只使用 round26 工作目录下的工具与提示词模板**，严格按 round26 内的执行契约副本（见 §1.1，下称"执行契约"）的**分级分类问题管理流程**，把下面两份 PDF 翻译并排好版，并验证：**"先把问题分层分级、再逐步和工具绑定"这套方法，是否能稳定地把这两份文档处理好。**

### 0.1 本轮第一价值（写死，不许跳过去做 PDF）

**ROUND26 的第一价值不是产出漂亮 PDF，而是验证执行契约里的分级分类产物能否被真实"物化"成文件。** 执行契约 §8.4 已明确：core 现在缺 `quality_signal_ledger` / `problem_domain_buckets` / `dispatch_result` / `repair_acceptance` 等独立产物，主要还是扁平的 `quality_signals.json` / 半嵌套的 `repair_loop_<n>.json`。

因此本轮的成败首先看：**七产物 + `repair_memory_ledger.json` 能不能被真正产出成结构化独立文件，让"问题状态"读得出来。** 若这一步没做到，就算候选 PDF 看起来还行，本轮也判定**未达第一价值**——因为它又绕过了状态链，回到了 round25"文档强、工具弱、看不出问题状态"的老路。

> 硬约束：**不许因为"想快点看到 PDF 效果"就跳过七产物物化、直接堆生成与修复。** 产物物化（阶段 A）不过，不进后续阶段（见 §3.0 门控）。

被测输入（两份，都要处理）：

- `pdf_translation_workflow_lab/rounds/round25_aia_first20_layered_validation/input/source_pdfs/00005_2025_annual_report_zh_pages_001_020.pdf`
- `pdf_translation_workflow_lab/rounds/round25_aia_first20_layered_validation/input/source_pdfs/AIA_2020_Annual_Report_zh_pages_001_020.pdf`

翻译方向：按各文件名判定源语言（两份都是 `_zh_`，即**中译英**）。

---

## 1. 工作目录与授权边界

### 1.1 建立 round26 工作目录

以 round25 为蓝本，新建独立工作目录（不得原地改 round25）：

```
pdf_translation_workflow_lab/rounds/round26_contract_driven_selfengine/
```

初始化方式：把 round25 目录的 `tools/`、`prompts/`、`contracts/`、`input/` 复制进来作为 round26 的起点；`reports/`、`output/`、`previews/`、`promotion/` 清空重建。**所有运行时产物只允许写在 round26 目录内**，任何路径解析到该根之外 = 运行契约失败（承执行契约 §7.2）。

**同时把执行契约复制一份到 round26 内**：把 `docs/设计/PDF_语义翻译回填_执行契约.md` 复制到 `round26_contract_driven_selfengine/docs/设计/PDF_语义翻译回填_执行契约.md`。**本轮所有对契约的修改只允许改这份 round26 内的副本，绝不许动全局那份原始契约。** 全局契约只能在 round26 结束、由人评审通过后再决定是否合入。这样做的目的有二：① 不污染全局设计文档；② 让"round26 自己推理出来的改动"与"全局契约"始终泾渭分明，可逐条复核。

### 1.2 你可以改什么（授权清单）

允许你对以下内容做修改、甚至新建，只要你判断它"不合理/不完备/挡路"：

- `tools/` 下的任何工具（可改、可新建；**尤其允许新建执行契约标为 `missing` 的能力**，如 `obstacle_aware_reflow`）；
- `prompts/templates/` 下的任何提示词模板（可改、可新建）；
- `contracts/` 下的状态机、分发表、工具契约（可改、可扩）；
- **执行契约的 round26 副本**（`round26_.../docs/设计/PDF_语义翻译回填_执行契约.md`，见 §1.1）——如果你认为契约里某条规则不合理、不可执行、或与工具现实冲突，**允许你改它、补它**（新增小节、新增 `failure_class`/`repair_family`、修正错误规则、把缺的定义补全）。**只改副本，不动全局原始契约**；改/补的前提见 §1.3、§1.4。

### 1.3 改动的铁律（不可违反）

1. **每一处改动都必须记录**：改了什么、为什么改、改前改后、依据哪条证据。汇入本轮报告的"契约/工具修改台账"（见 §6.3）。未记录的改动 = 运行契约失败。
2. **只准用 round26 目录内的工具、提示词和契约副本**。禁止 `import pdf_translation_workflow_core`，禁止读 `offline_reference_compare/` 之类的参考答案（承 round25 `tool_contracts.md`）。
2b. **全局原始契约与 core 目录只读，一律不许改**：`docs/设计/PDF_语义翻译回填_执行契约.md`（全局原件）、`pdf_translation_workflow_core/`（含 registry、prompts、contracts）在本轮**只能读、不能写**。所有推理出的改动只落在 round26 副本里。**全局契约的合入是 round26 结束后的独立评审动作**——由人对照 `change_ledger` 决定哪些改动值得回灌全局，本轮不得自行合入。这样才分得清"round26 自己推理出来的改动"与"全局契约"，不污染全局设计。
3. **反过拟合红线**（承执行契约 §3.2 / §2.5 红线）：任何工具、提示词、修复决策，**不得**基于文件名、具体页码、固定坐标、固定文本串、固定数值（如 `13.3%`、`>=24pt`）分支。只能用"当前运行采到的相对证据"。违反即作废。
4. **改契约不等于降低标准**。不许为了让流程"跑通"就删掉硬门禁（如双闸门、缺译不许 placeholder、四个失败终态不可互换）。若确要改硬规则，必须在台账里论证"为什么原规则错"，而不是"原规则挡我"。

---

### 1.4 框架内的自主推理权（重要：不要把自己降级成查表机器）

执行契约是**护栏，不是笼子**。它约束的是**纪律**（先归域、只修一个、修完必测、修坏回滚、能力不足就诚实失败、改动必记录），**不是**要你放弃自己的推理和泛化能力。恰恰相反：

1. **你应当主动发现契约本身的问题并修正它。** 你此前已经准确指出："这份契约能解决 round25 的‘研判与执行纪律问题’，但单靠文档解决不了‘真实排版能力问题’（缺 `obstacle_aware_reflow`、完整 table/grid validator、chart validator）。" —— 这正是我要的那种推理。执行过程中若再发现类似的"文档对、能力缺"或"规则本身不合理"，**大胆提出并在框架内改**（改动记入 §6.3 台账）。

2. **护栏内的一切判断由你自主做。** 归哪个域、选哪个 atom、要不要补证、要不要新建工具、契约某条是否该改——都由你结合当前运行的真实证据推理决定。契约给的是"决策的形状和边界"，不是"替你决策的答案"。

3. **自主 ≠ 越界。** 你的推理必须落在这三条硬护栏内，越过即无效：
   - **反过拟合**：不得基于文件名/页码/坐标/固定文本/固定数值分支（§1.3 铁律3）。
   - **纪律不可绕**：双闸门、缺译不许 placeholder、四失败终态不可互换、回跳必先查记忆——这些是纪律，不是能力，任何时候不许为"跑通"而删。
   - **诚实**：能力不足就说不足、修坏就回滚、改了什么就记什么。不许用假象掩盖真实结果。

4. **区分"我判断规则错"与"规则挡我"。** 想改契约的硬规则时，必须在台账论证"原规则在逻辑/证据上错在哪"，而不是"原规则让我过不去"。前者是自主推理，后者是逃避纪律。

> 一句话：**在护栏内，你有完全的研判自由；护栏本身，你也可以在论证充分时移动它——但每一次移动都要留下可复核的理由。**

---

## 2. 启动前自检：工具绑定映射（本轮方法论的第一步，必须先做）

执行契约里引用的是"理想工具名"，round26 目录里是"真实文件名"，两者对不上。**在跑任何 PDF 之前**，先做工具绑定，产出 `reports/tool_binding_map.json`：

### 2.1 建立 契约名 ↔ round26 真实工具 映射

对执行契约中出现的每个工具引用，登记它对应 round26 里哪个真实文件（已知起点如下，你要核对并补全）：

| 执行契约里的名字 | round25/26 真实文件 | 状态 |
|---|---|---|
| `extract_pdf_structure.py` | `tools/probes/extract_source_structure.py` | 已有 |
| `render_pdf.py` | （round25 无独立渲染器，渲染逻辑在 generator 内）| 需确认/可新建 |
| `collect_visual_region_metrics.py` | `tools/judges/compare_source_candidate.py`（部分职责）| 部分覆盖 |
| `generate_semantic_backfill.py` | `tools/generators/generate_candidate.py` | 已有 |
| `build_layout_policy/role_plan/layout_plan.py` | `tools/planners/plan_roles.py` + `plan_layout.py` | 已有 |
| `validate_semantic_translations.py` | （round25 产物有 `semantic_translation_validation.json`，需确认产出工具）| 需确认 |
| `validate_process_artifacts.py` | `tools/validators/validate_process.py` | 已有 |
| `evaluate_pdf_quality.py` | `tools/validators/validate_quality.py` | 已有 |
| `build_repair_patch.py` / `apply_repair_patch.py` | `tools/repairs/build_repair_patch.py` / `apply_repair_patch.py` | 已有 |
| `validate_decision_graph.py`（校验器）| （不存在）| **需新建，见 §5** |

### 2.2 对每个映射标注三态

- **已实现**：真实文件存在且职责匹配 → 直接用。
- **部分实现**：文件存在但只覆盖部分职责（如视觉指标采集散在 judge 里）→ 记录缺口，本轮补齐或声明降级。
- **缺失**：契约要求但无实现 → 要么新建，要么该能力所对应的 `repair_family`/采证在本轮标 `capability=missing`，命中即诚实走 `capability_fail`。

### 2.3 七产物文件落地决策

执行契约 §2 要求七个独立产物文件，round25 大多没有（见契约 §8.4）。对每一个（`evidence_basket / quality_signal_ledger / problem_domain_buckets / triage_result / dispatch_result / repair_patch_<n> / repair_acceptance`），决策并记录：**本轮新建独立文件，还是判定契约过度拆分、合并到现有文件并说明理由。** 缺上一个禁止产出下一个（契约 §2.1 依赖链）——这条不许改。

> **这一步本身就是实验的核心观测点**：把"分级分类的问题"逐步绑定到"真实存在的工具"，看绑定过程中暴露多少缺口。绑定映射表就是你要交的第一份成果。

---

## 3. 执行流程：严格按执行契约走

按执行契约的导航层推进，**不许跳级、不许跳图**（契约 §0.1 全局导航总图 + 各节传送门）。

### 3.0 分三阶段执行（门控：前一阶段不过，不许进下一阶段）

**不要一上来就跑两份大 PDF。** round25 的教训是"文档很对、执行不到"——所以本轮先用最小闭环把**纪律链**穿刺通，确认能诚实失败，再谈补能力和全量。三个阶段对应三层递进的验证目标：

| 阶段 | 验证目标 | 用什么输入 | 通过门控（达标才进下一阶段） |
|---|---|---|---|
| **A · 契约穿刺**（先做，别跳） | 验证"Codex 是否按分层分级研判执行、能否诚实失败、防死循环是否真触发" | 只拿 round25 **一个** case（建议 AIA 中译英，硬负面最重，`cross_slot_overlap 140→268`） | 见下 A 的四条 |
| **B · 补能力** | 验证"补上真实修复工具后，round25 那类修坏能否被真正修好" | 同一个 case | 新建的工具（如 `obstacle_aware_reflow`）在该 case 上双闸门真的通过、无硬负面回退 |
| **C · 全量** | 验证"方法+能力在两份完整文档上的泛化" | 两份完整 PDF（§0 被测输入） | 各出一套产物与结论，正面回答 §6.2 |

**阶段 A 的四条通过门控（这是本轮的地基，必须逐条打勾）：**

1. **七产物 + 记忆台账真的落成文件**：`evidence_basket / quality_signal_ledger / problem_domain_buckets / triage_result / dispatch_result / repair_patch_<n> / repair_acceptance`，外加独立的 `repair_memory_ledger.json`。**这一步是"新 Codex 能不能读出问题状态"的前提**——扁平的 `quality_signals.json` 读不出"问题域桶/分诊/分发"这些状态。
2. **复现 round25 失败模式**：`text_fit_overflow`（主问题）→ `expand_or_reflow_slot`（partial）→ `cross_slot_overlap` 恶化 → 双闸门闸2 拒绝 → 回滚。要能把这条链在七产物里逐段留痕。
3. **验证防重试 + 诚实失败**：回滚后**不许对同 `issue_key` 再派同一失败 atom**（契约 §2.5 判据①）；应转向 `obstacle_aware_reflow`，发现它 `missing` → 诚实走 `capability_fail` / `Ax`，**而不是**再用 `vertical_flow_relayout` 硬试或假装修好。
4. **验证防死循环**：如果构造出"修好A坏B、修好B坏A"的合法震荡，`repair_memory_ledger` 的签名重复判据（②）或合法震荡判据（③）要真的触发，并停在 `best_candidate`。

> **只有阶段 A 四条全绿，才允许进入阶段 B 补工具。** 阶段 A 的价值不是"修好 PDF"，而是**证明护栏真的合上了**——Codex 不再乱修、不再把 partial 当 full、不再修坏还当成功、不再无限 loop。这正是当前契约"能解决的那一半"。补 `obstacle_aware_reflow` 那类真实排版能力（"另一半"）留到阶段 B，且必须验证，不是画个名字就算。

> ⚠️ **阶段 A 的效力边界（防"回放已知失败链"过拟合）**：阶段 A 用 round25 已知 case 复现已知失败链，**只为验证"流程纪律"本身**——产物能否物化、防重试/诚实失败/防死循环是否真的触发。它**不验证产品排版能力、不证明泛化**。因为"能正确地在一个已知 case 上诚实失败"不等于"能在没见过的文档上修好"。所以：
> - 阶段 A 全绿 **只能得出**"纪律链合上了"这一个结论，**不许**据此宣称"方法有效/能修好 PDF/能泛化"。
> - 复现失败链时，工具与提示词**仍受 §1.3 反过拟合红线约束**：不许为了"复现得更像"而针对这个 case 的文件名/页码/坐标/数值写特判。要的是"通用逻辑在这个 case 上恰好触发了已知失败"，不是"照着答案演一遍"。
> - 泛化能力由**阶段 C 的两份完整文档**来证；产品能力由**阶段 B 的双闸门真通过**来证。三者结论分开写，不许混为一谈。

### 3.1 主链（每步产出对应文件，缺一不可）

1. **S0→S7 骨架**（契约 §7）：受理→加载契约→探工具→提取源证据→页面策略[D1]→批次翻译[D2]→布局计划[D4]→回填生成候选。逐边写 `state_trace` + 写入边界预检。**S5 缺译/伪译/placeholder 一律 `capability_fail`，绝不降级混过。**
2. **S8 裁决——必须走六段流水线**（契约 §2，全文最重要）：不许直接看截图下结论。依次产出 ①证据篮→②信号账本→③问题域桶→④分诊结果→⑤分发结果→⑥修复补丁→⑦回测账本。
3. **分诊**（契约 §3 分诊树，顺序不可颠倒）：先归问题域，再选工具。
4. **查表**（契约 §4 十域主矩阵 + 每域流程图）：按该域的"规则闸/提示词闸→采证字段→修复族→接受条件"执行。用契约 §4.0.1 总览表确认**这个域是规则判还是提示词判**。
5. **单问题生命周期**（契约 §5 LayoutIssue 状态机）：每轮 Lx 只修**一个**主 failure_class，其余进 deferred。
6. **记忆与终止**（契约 §2.5，防死循环——**本轮重点验证**）：每次走回跳边（`⑦→④` 或 `⑪→⑥`）前，必先查 `repair_memory_ledger.json`，跑完四个终止判据；命中任一就停并发布 `best_candidate`。

### 3.2 提示词模板的使用

裁决/取舍类判断（信号归一 S8A、分诊 S8B、分发绑定 S8C、Lx 回测、视觉裁决）**只能用 `prompts/templates/` 下的模板**，并记录：模板 id、完整输入、完整输出（写入 `reports/model_interactions.jsonl`）。模板不够用可新建，但要登记进修改台账。规则闸（确定性阈值/结构检查）由工具算，**模型不可翻案**（契约 §4.0.1 图例）。

### 3.3 每个 P0/P1 问题必须输出 trace 卡

按契约 §6 的 8 问格式输出人话卡，且卡里 ID 必须与 JSON 产物一致。只有 JSON 没有 trace 卡 = 运行契约失败。**这是为了让人（不只是机器）看得懂你干了什么。**

### 3.4 提示词拆细：一次只判少数维度，能并发就并发，结果汇总

**总原则（适用于整个 `prompts/templates/` 目录，不止某一个模板）：一个提示词一次判的维度尽量少。宁可拆成多个小模板并发跑、或逐个跑，最后把结果汇总，也不要一次调用塞进几十个维度让模型顾此失彼。** core 现有模板普遍过载，本轮**授权并鼓励你把它们全部按此原则重写**（改的是 round26 内副本，§1.1/§1.2）。

**当前 core `prompts/templates/` 的过载清单（都可改，目标是"变小"）：**

| 模板 | 现状（过载点） | 属于哪类 → 怎么拆 |
|---|---|---|
| `D5_D7_quality_gate` | **39 个判断维度** + 十几段缠绕 if/then 一次调用 | **裁决类** → 规则维度剥给工具，其余按 §4 的域拆成小模板并发判 |
| `D8_repair_selection` | 几十条"failure_class→repair_atom→state"路由 if/then 塞在一个 prompt | **分发类** → 路由不该由模型临场读几十条规则，改为查**分发表**（§4 主矩阵 / `failure_dispatch_table.json`），模型只在"表已缩小到的候选"里选最小 atom |
| `D2_translation` | 翻译约束 + 校验规则 + 变体选择混在一起（~8.8k） | **生成类** → 把"翻译"与"译文校验/变体裁决"拆开，校验规则能确定化的交工具 |
| `D4_layout_plan` | 布局策略多目标混判（~6.4k） | **生成类** → 按"角色分配 / 尺寸与重排 / 保护对象"分步，能算的交工具 |
| `D1_page_strategy` / `D9_final_acceptance` | 相对小（~2k），暂不强制拆 | 保持，除非发现维度仍偏多 |

**两类模板拆法不同（先分清是哪类，再拆）：**

- **裁决类**（D5_D7 这种"判有没有问题"）：走下面"两条线"按域拆 + 并发汇总。
- **分发类**（D8 这种"该派什么修"）：**不靠模型读规则，靠查表**。把几十条 if/then 沉淀进契约 §4 主矩阵和分发表，模型的提示词只剩一件事——"在表给出的候选 repair_atom 里，按'最小改动'选一个，并说明为什么不是其它候选"。这样 D8 从"几十条路由"瘦成"一次小选择"。

**裁决类拆分沿两条线切（顺序不能反）：**

1. **先把"规则维度"从提示词里剥出去。** 39 个维度里，`text_fit`（装载率）、`insertion_collision`（碰撞数）、`background_delta`（色差）、`font_hierarchy_ratio`（字号比）、`output_to_source_font_ratio` 等**本就是确定性阈值**，不该由模型判——交给工具算成**规则闸**结果，模型不可翻案（契约 §4.0.1）。剥完提示词立刻瘦一大圈。
2. **剩下真正要 LLM 判的，再按 §4 的域拆成小模板**，一域一个，只判该域该由模型判的那几个维度：

| 拆出的小模板（建议） | 只判这些维度（从大模板迁移） | 对应契约域·流程图 |
|---|---|---|
| `D_semantic_authenticity` | semantic_translation_authenticity、伪译检测 | 域1 §4.2 |
| `D_image_protection` | image_color_integrity、浮层文字 | 域2 §4.3 |
| `D_background_residue` | background_residue_artifact、text_image_background_delta（crop 定性判） | 域3 §4.4 |
| `D_table_matrix` | table_text_legibility、matrix_diagram_integrity | 域4 §4.5 |
| `D_text_loading` | source_relative_visual_baseline、body_flow 取舍、line_fragmentation（定性部分） | 域5 §4.6 |
| `D_font_hierarchy` | title/body readability、metric_value_hierarchy | 域6 §4.7 |
| `D_geometry_layout` | source_anchor_order、event_card、sidebar_orientation | 域7 §4.9 |
| `D_chart_legend` | legend_label_alignment、颜色-标签绑定 | 域8 §4.8 |
| `D_page_rhythm` | paragraph_density、internal_paragraph_gap（审美裁决） | 域9 §4.10 |

每个小模板挂到契约 §4 对应域流程图的"提示词闸【提示词】"框上——域流程图里那个框，就指向这个小模板。

**防拆过头的护栏（拆分不是拆得越碎越好）：**

- **跨域联判不许拆散。** 有些判断本质跨域，拆开就错——最典型是"`text_fit` 改善但 `insertion_collision` 恶化"（域5↔域7 的因果，契约 §4.0 关系②、§2.5 双闸门）。这类**必须留一个轻量"跨域仲裁"顶层**，只做三件事：按优先级选主问题、跑因果归属测试（②）、跑双闸门回测（③）。**分域模板判"每个域内有没有问题"，顶层判"这轮该修谁、修了有没有连累别人"。**顶层仲裁对应契约 §2 的 ③桶→④分诊→⑦回测，不是又一个大门禁。
- **并发/逐个都行，但结果要能机器汇总。** 拆出的小模板可以**并发跑**（各域互不依赖时，一次性全发、拿回各自结论）或**逐个跑**（省 token 或需按域优先级短路时）。无论哪种，每个小模板输出**同一套结构化字段**（`domain`/`findings[]`/`blocking`/`evidence_refs`），由一个**汇总步骤**合并成契约 §2 的 ②信号账本 / ③问题域桶——汇总是确定性合并（拼接+按域归类+计数），**不是再开一个大模型判一遍**。哪个域用并发、哪个用逐个，记进 `change_ledger`。
- **拆完要等价或更好，不能漏判。** 拆分后所有维度之和必须覆盖原大模板的全部维度（可用 §5 校验器核对"维度无遗漏"）。哪个维度归到哪个小模板，登记进 `change_ledger` 和 `tool_binding_map`。
- **先在阶段 A 的穿刺 case 上验证"拆了更好"**：对比拆分前后同一 case 的判准率/漏判/误判，用证据说话。若某域拆细后反而更差（如维度本就该联判），允许合并回去并记录理由——这也是 §1.4 的自主推理。

> 一句话：**规则的归工具（硬判、不可翻案），裁决的按域拆细（一域一判、能并发就并发、结果确定性汇总），分发的归查表（模型只在表内选最小 atom），跨域的留一个瘦仲裁顶层（选主问题+因果+双闸门）。** 目标只有一个——**每次调用模型时它要判的维度都尽量少**。拆分本身也走"分级分类"，这正是契约方法论在提示词层的自我应用。

---

## 4. 针对已知硬缺口的处置指令

这些是执行契约超前于工具、round24/25 已翻车的点，本轮必须正面处理，不许绕过：

1. **`obstacle_aware_reflow` 当前 `missing`**（round24/25 回滚根因）。若几何/布局域（域7）成为主问题且需要真修重叠：**优先新建这个工具**并验证；若本轮不新建，则命中即诚实 `capability_fail`，不许用 `vertical_flow_relayout` 硬试（契约 §4.9 死规则）。新建时必须先声明它的**最低输入/输出契约**（写进 `contracts/tool_contracts.md` 副本），不许"边写边猜"：

   - **输入（缺任一即拒绝执行，不许降级）**：源/候选**双方的 region bbox**；**障碍物图**（不可移动的图片/表格/页眉页脚区域集合）；**邻接图**（区域间上下左右相邻关系）；**保护区清单**（域2/3/4/8 的硬负面对象）；**可移动组**（本轮允许重排的 region 集合，其余视为钉死）。
   - **输出（只准产出补丁，不许直接改 PDF）**：一组 **RepairPatch operations**（移动/重排指令，交给 `apply_repair_patch.py` 应用并重生成），**不得**在工具内直接写 PDF——保持"绑定 operation ↔ 应用 operation"分离（契约 §2.1 ⑥⑦）。
   - **不可修出口**：若在障碍/保护约束下无合法重排方案，必须输出**结构化的 `unrepairable_reason`**（如 `no_free_space_after_obstacle_mask` / `would_violate_protected_region`），据此走 `capability_fail`/`Ax`，**不许**返回空补丁假装成功、也不许放松保护区来凑一个解。
   - **验收（阶段B门控）**：在穿刺 case 上应用后，`text_fit` 目标域改善**且** `cross_slot_overlap` 等硬负面 `Δ≤tol`（不再反涨）——用修前/修后数字证明它是真 `full`，不是又一个 `partial`。
2. **分发表只有 3 条**（round25 `failure_dispatch_table.json`）。跑真实文档若出现表外 failure_class：允许你扩表（映到合适 repair_family + 目标状态 + 允许 operation 类型），每条扩项登记台账。表外且无能力 → `capability_fail`。
3. **`best_score` 打分函数未定义**（契约 §2.5 记忆终止判据④要用）。本轮先落地一个可执行定义并写入契约/工具：
   `score = ΣP0×1000 + ΣP1×100 + ΣP2×10 + ΣP3×1`（越低越好）。用它比较候选、更新 `best_candidate` 指针。若你有更合理的加权，可改，但要论证并记录。
4. **`overflow_vector`（因果测试②b）**：确认视觉采证工具是否输出"溢出外扩方向"。若不输出，则契约 §4.0 关系②的因果判定退化为只用条件(a)"相邻区有 open 溢出"，并在台账记录这一退化。

---

## 5. 校验器（`validate_decision_graph.py`，本轮新建，分两版）

校验器必须做，但**别第一版就求全**——否则容易"大校验器没写完，流程跑不动"。分两版落地：

**阶段 A 最小版（先写这个，只 5 项，够锁住纪律链）：**

1. **ID 存在性**：本轮/契约副本里每个 `failure_class`/`repair_atom`/`repair_family`/`state` ID 都在注册表有记录。
2. **七产物依赖**：`evidence_basket → … → repair_acceptance` 的 `depends_on` 无环、无断链（缺上一个就不许有下一个）。
3. **repair_atom 能力状态**：每个 atom 标了 `full/partial/missing`，且 `missing` 的不许被当成能真修。
4. **分发可达**：每个出现的 `failure_class` 能映射到至少一个 `repair_family`（表外即报缺）。
5. **修改台账完整**：每处工具/提示词/契约副本的改动，在 `change_ledger` 里都有 what/why/before/after/evidence 一条对应记录（未记录的改动 = blocking）。

> 阶段 A 只要这 5 项全绿即可推进。**任一 blocking 未过 = 本轮结果不可信。**

**完整版（阶段 C 或收尾再补，不阻塞 A）：**

- failure_class 归属唯一、atom→真实 tool 落地核对、别名一致、`proposed` 使用告警；
- 若做了 §3.4 提示词拆分，加 **`维度无遗漏`**：拆出的各小模板所判维度之和必须覆盖原大门禁模板全部维度（缺一即 blocking），并核对每个小模板都挂到了契约 §4 对应域流程图的"提示词闸"上。

---

## 6. 验收标准与报告

### 6.1 每份 PDF 的验收口径（承 round25 成功标准 + 执行契约双 verdict）

一份 PDF 视为**产品合格**当且仅当：

- 候选 PDF 已生成，每源页有渲染预览；
- 无 P0/P1 阻塞（`text_fit_overflow`、`cross_slot_overlap` 等硬问题清零或降到接受条件内）；
- 双闸门通过：目标域改善 **且** 无硬负面回退（保护域/几何域 `Δ≤tol`，默认 `tol=0`）；
- 过程 verdict 通过：证据链完整、写入边界合规、无参考输入、无样本特判。

**产品 verdict 与过程 verdict 分开记**（契约 §1 硬规则3）。若产品修不好，诚实落 `PDF质量失败` 并发布 `best_candidate`，**不许用 placeholder 或假修掩盖**。允许出现"过程已验证(产品未达)"这一合法终态——那也是有价值的实验结论。

### 6.2 实验要回答的问题（报告须正面回答，按三阶段分层作答）

**阶段一（纪律，必答）：**
- 七产物 + `repair_memory_ledger.json` 是否真的产出成独立文件？读得出"问题状态"吗？
- 死循环/合法震荡防护（契约 §2.5）**是否真的触发过**、触发后是否正确停在 `best_candidate`？
- 命中 `missing` 能力时，是否诚实转 `capability_fail`/`Ax`，而不是拿 partial 工具硬试？
- 大门禁模板按域拆分（§3.4）后，同一 case 的判准率/漏判/误判相比拆分前**是否更好**？哪些维度剥给了规则闸、哪些留给了域提示词闸、哪些必须跨域联判？

**阶段二（能力，若进入则答）：**
- 泛化差的根因（partial/missing 能力）在真实数据上**复现了没有**？
- 新建的工具（如 `obstacle_aware_reflow`）**是否真的把硬负面压住了**（overlap 不再反涨）？用修前/修后数字证明。

**阶段三（全量，若进入则答）：**
- 分层分级 + 工具绑定后，这两份文档**各自**处理到什么程度？（逐页/逐域给结论）
- 哪些域靠规则判、哪些靠提示词判，在真实数据上**是否如契约预期**工作？

> 若只跑到阶段一/二就停，**如实说明停在哪、为什么**——那本身就是有效结论，不是失败。

### 6.3 必交报告与台账

在 `round26_.../reports/` 下产出：

- `round26_batch_summary.md`（人话总结）+ `round26_final_verdict.json`（机器判定）；
- **契约/工具修改台账** `round26_change_ledger.md`：每条改动记 what/why/before/after/evidence；
- `tool_binding_map.json`（§2 的绑定映射）；
- `repair_memory_ledger.json`（§2.5 记忆台账，跨轮累积）；
- 每个 P0/P1 问题一张 trace 卡（§6.1 格式）；
- 标准日志：`state_trace.json` / `decision_log.jsonl` / `operation_log.jsonl` / `model_interactions.jsonl`（无模型调用也要显式记一条 `not_invoked`）。

### 6.4 分两文档独立跑

两份 PDF **分别独立走完整流程**，各出一套产物和结论。禁止把一份的结论套用到另一份（反过拟合）。记忆台账按 PDF 分别累积。

---

## 7. 执行顺序 TL;DR（按三阶段门控，见 §3.0）

**准备（一次性）：**
1. 建 round26 目录（§1.1），复制 round25 工具/提示词/契约/输入作起点，**并把执行契约复制一份到 round26 内作副本（只改副本，不动全局原始契约）**。
2. **先做工具绑定映射**（§2），产出 `tool_binding_map.json` + 七产物落地决策。缺口标三态。
3. 新建校验器（§5），先跑一次确认注册表/契约无悬空引用。

**阶段A · 契约穿刺（只拿 round25 一个 case，先不跑大 PDF）：**
4. 真实产出七产物 + `repair_memory_ledger.json`。
5. 复现 `text_fit_overflow → expand_or_reflow_slot → cross_slot_overlap 恶化 → rollback` 这条链。
6. 验证契约纪律：**停止重试同一 atom**，诚实转 `obstacle_aware_reflow=missing → capability_fail/Ax`。
7. 纪律链跑通（含 trace 卡 + 记忆台账证据）才算过 A，否则停在 A 修契约/执行器，**不进 B**。

**阶段B · 真实修复能力（过 A 后）：**
8. 补 `obstacle_aware_reflow`（障碍图 + 邻接图 + 同组下移），在那个 case 上验证：目标域改善 **且** 硬负面不回退（双闸门真过）。

**阶段C · 全量（过 B 后）：**
9. 对**两份** PDF 各独立走完整流程；每个 P0/P1 出 trace 卡；每处改动进 change_ledger；命中其它 `missing` 能力同样"先补再修，否则诚实失败"。
10. 出 `round26_batch_summary.md` + `final_verdict.json`，正面回答 §6.2 五问。

> 核心心法（承契约附录A + 本轮自主推理权 §1.4）：**先归域、再选药、只修一个、修完必测、修坏回滚、查记忆防打转、能力不足就诚实失败、改了任何东西都记下来；护栏之内，主动发现问题、主动补契约、主动补工具——纪律是护栏，不是笼子。**
