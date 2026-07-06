# PDF 中文翻译回填标准流程设计

## 1. 目的

本文件是 PDF 源语言内容翻译为目标语言并回填 PDF 的标准执行规范。历史默认方向是英译中；当输入包声明 `source_language`、`target_language` 和 `target_text_field` 时，执行器必须按声明方向运行，例如中译英。

它用于指导一个新的 Codex 会话按固定状态机执行：

```text
读取契约 -> 探测工具 -> 提取源 PDF -> 制定页面策略 -> 校验译文 -> 制定布局策略 -> 生成候选 PDF -> 验证质量 -> 修复循环 -> 最终审计
```

本文件不是历史记录。历史实验、截图反馈、失败过程和旧轮次证据只放在审计文档或报告中，不作为新执行器的主调度依据。

## 2. 核心原则

1. 状态先行：每一步必须属于一个明确状态。
2. 工具先证据：文件存在、JSON 可解析、PDF 可渲染、页数一致等事实由工具判断。
3. 大模型只裁决：大模型用于页面策略、翻译、布局策略、视觉质量和修复选择，不用于替代文件系统或工具检查。
4. 提示词不重造：执行轮次只能使用核心提示词模板填槽位，不能重新发明判断提示词。
5. 失败可接受：产品质量失败可以接受，但必须诚实进入修复循环或终态失败。
6. 反过拟合：生产工具和契约不能依赖样本文件名、固定页码、固定坐标、固定文本、固定颜色或已知文档身份。

### 2.1 翻译方向契约

每个产品质量运行必须在 `run_request.json` 或语义译文 JSON 中声明方向：

```json
{
  "source_language": "en|zh|...",
  "target_language": "zh|en|...",
  "target_text_field": "translation_zh|translation_en|translation_target_text"
}
```

默认兼容值：

```json
{
  "source_language": "en",
  "target_language": "zh",
  "target_text_field": "translation_zh"
}
```

方向只影响“哪些源行需要翻译”和“目标文本字段/残留文本如何校验”。布局策略、状态机、修复 loop、视觉裁决和反过拟合边界不因具体语言或样本文档而换规则。

每个方向必须绑定一个通用 language layout profile，profile 只能描述语言方向的开口策略，不能描述具体样本：

```text
pdf_translation_workflow_core\profiles\en_to_zh.layout_profile.json
pdf_translation_workflow_core\profiles\zh_to_en.layout_profile.json
```

profile 必须由 `S6_LayoutPlan` 作为 `build_layout_policy.py --language-profile` 输入进入 `layout_policy.json`，并在 `layout_policy.json` 中记录：

```text
language_pair_profile
language_profile_json
language_profile_sha256
layout_strategy
source_language
target_language
target_text_field
```

方向 profile 的允许内容：

```text
target_language_reflow：目标语扩框策略，例如 zh->en 先扩正文框再缩字号
target_composition：目标语视觉构图策略；对流式正文，源 bbox 是遮罩、锚点和阅读顺序证据，不是硬目标容器
flow_grouping.body：同栏正文流合并、段落 gap、短续行合并、密集页下方正文带规则
font_profiles：各 region kind 的字号上下限、source_scale、shrink_scales、min_insert_pt
fallback：无法 fit 时的显式失败/降级策略
prompt_overlay：D2/D4/D7 的方向性判断提醒
```

方向 profile 的禁止内容：

```text
样本文件名
官方对照页码
固定 bbox 坐标
固定年份或财务指标
固定专有文本或术语白名单
从人工对照样本抄来的布局坐标
```

### 2.2 受限槽位与流式正文

执行器在 `S6_LayoutPlan` 必须把区域分成两类：

```text
constrained_slot：表格单元格、图例、图表标签、侧边导航、页码、短标签。以源 bbox 为硬约束，优先使用 compact/table/legend/side-nav 译文变体。
event_card：时间线、里程碑、人物/图片旁的窄多行事件说明。属于受限槽位，但允许在本事件卡内部多行重排；不能跨年份、图片或相邻事件卡合并。
fluid_body：正文段落、下方正文带、连续正文栏。以源 bbox 为遮罩和阅读顺序锚点，但目标语可按当前页正文带、页边距、避让区和下边界重构文本框。
```

判断依据只能来自当前运行：

```text
page_type_guess
region_kind
同栏 x0/width/y-gap 统计
字体大小层级
绘图/表格密集度
当前页可用正文带和避让区
目标语言方向 profile
```

禁止依据：

```text
文件名
官方对照页码
固定坐标
具体财务词汇或年份
历史 round 的候选 PDF
```

当 `target_composition.enabled=true` 且区域是 `fluid_body/body_flow` 时，`S7_GenerateCandidate` 的顺序是：

```text
1. 按源 bbox 周边像素做背景聚类采样，记录 fill_color provenance。
2. 按源 bbox 擦除可替换源文本；填充色必须来自局部背景，不得来自源字形颜色。
3. 合并同栏正文为 body_flow。
4. 用 target_composition 根据当前页正文带重算目标文本框。
5. 用 overlap_guard 防止侵入下一同栏区域。
6. 在临时页探测字号和换行。
7. 只把通过探测的最终文本画到真实候选页。
```

当区域是 `constrained_slot` 时，不能使用正文构图扩张；只能用紧凑译文、字号曲线、旋转绘制或显式失败。

### 2.3 已是目标语的可见文本

双语 PDF 中可能有一部分源页面文本已经是目标语言，例如中文页里的英文专名或英文页里的中文专名。它们不属于语义翻译覆盖率，但如果正文重排会覆盖它们，必须进入回填流程：

```text
translation_mode = preserve_already_target_language_span
```

执行规则：

```text
该行不含源语言字符
该行含目标语言字符
不要求出现在 semantic_translations.json
生成器可擦除并原文重绘
计入 preserved_target_language_unit_count
不计入 semantic_translated_unit_count
```

如果这类文本未擦除且与新正文重叠，`S8_VerifyProductQuality` 必须判定 `failed_probe_residue`、`collision` 或 `visual_similarity` 失败。

### 2.4 擦除填充色与背景差异

擦除填充色是视觉质量的一等约束。生成器必须以当前页、当前 bbox 的周边像素为输入做开放式背景采样：

```text
input = page pixels + source bbox
sample = bbox outside ring points, multi-distance
selection = quantized majority color cluster
forbidden = filename/page/text/fixed color/known brand coordinate
output = fill_color + provenance in candidate_generation_evidence.json
```

判断逻辑：

```text
如果源字形为红色/蓝色/灰色，但周边背景为白色或米色，则 fill_color 必须接近周边背景。
如果源文字位于红色/灰色底栏中，且周边背景也是该颜色，则 fill_color 可为该栏底色。
如果候选页出现可见色块、遮罩块或与周边背景不一致的擦除痕迹，S8 归因为 background_delta。
background_delta 的修复原子是 background_fill_resample，回到 S7 重新生成，不归因为翻译语义或字体缩放。
```

该规则只依赖当前渲染像素，不得写入任何样本专属颜色、页码、坐标或文本。

### 2.5 对照样本边界

如果存在同一报告的官方中文/英文对照版本，它只能作为离线评估集使用：

```text
允许：在 round 完成后，用对照样本观察结果偏差、总结通用规则缺陷、设计新的非过拟合质量指标。
禁止：把对照样本作为 D2 翻译输入、D4 布局输入、D7 视觉裁决输入、运行时 prompt 槽位、坐标来源、页码规则来源或术语强制映射来源。
```

执行报告必须区分：

```text
runtime_input: 当前 round 实际输入
offline_reference: 仅用于事后评估的对照样本
```

`offline_reference` 不得出现在 `prompt_instance.json`、`slot_values.json`、`semantic_translations.json`、`layout_policy.json` 的输入来源中。

对照样本相关脚本、回放工具、样本敏感词列表和人工结果差异报告也不得进入 `pdf_translation_workflow_core`。它们只能放在：

```text
docs\offline_reference_evaluation\
docs\reports\<round_id>\
```

`pdf_translation_workflow_core` 只存放真实运行时会调用的通用抽取、布局、回填、验证工具，以及与这些工具一致的提示词和契约。

## 3. 目录约定

每个执行轮次必须有独立工作目录，或在根目录 `docs` 下用 round 编号隔离。根目录验证优先使用：

```text
docs\input\<round_id>\
docs\output\<round_id>\
docs\reports\<round_id>\
```

外部会话验证包可以使用独立 round 根目录：

```text
<round_root>\
  pdf_translation_workflow_core\
  docs\
    业务流程\
    input\
      semantic_translations\
    output\
    reports\
    测试提示词\
  state_trace.json
  operation_log.jsonl
  decision_log.jsonl
```

输入 PDF 可以放在 round 根目录或 `测试数据\` 下。

输出 PDF 只能放在：

```text
docs\output\
```

所有证据、报告、质量门禁、裁决记录必须放在：

```text
docs\reports\
```

## 4. 状态机

| 状态 | 目的 | 必须产物 | 失败终态 |
|---|---|---|---|
| `S0_Request` | 确认目标、输入、run mode、非目标 | `run_request.json` | 输入缺失则等待用户 |
| `S1_ContractLoad` | 读取流程文档、契约、工具说明、提示词绑定 | `contract_load_record.json` | `S_FAIL_PROCESS_CONTRACT` |
| `S2_ToolProbe` | 探测 Python、PDF 库、字体、渲染能力 | `tool_probe.json` | `S_FAIL_TOOLING` |
| `S3_SourceExtract` | 提取源 PDF 页尺寸、文字、bbox、字体、图像、绘图对象，并渲染源图 | `source_extraction.json`、源 PNG | `S_FAIL_TOOLING` |
| `S4_PageStrategy` | 判断页面类型和区域角色 | `page_strategy.json` | `S_FAIL_PROCESS_CONTRACT` |
| `S5_TranslationPlan` | 生成或校验目标语语义译文 | `*.translations.json`、`semantic_translation_validation.json` | `S_FAIL_CAPABILITY` |
| `S6_LayoutPlan` | 生成或修订布局策略；必须区分 `constrained_slot` 与 `fluid_body`，并声明 `target_composition` 是否适用 | `layout_policy.json`、`layout_plan.json` | `S_FAIL_PROCESS_CONTRACT` |
| `S7_GenerateCandidate` | 擦除源语文本并回填目标语候选 PDF；流式正文可按目标语构图，受限槽位必须原位约束 | 候选 PDF、`candidate_generation_evidence.json` | `S_FAIL_TOOLING` 或 `S_FAIL_CAPABILITY` |
| `S8_VerifyProductQuality` | 执行机器质量门禁和视觉裁决；必须检查正文构图、字体层级、重叠残留、侧边栏方向和表格/图表完整性 | `product_quality_gates.json`、`visual_adjudication.json` | `S_FAIL_QUALITY` |
| `Lx_RepairLoop` | 对一个阻塞失败执行一次修复循环 | `repair_loop_<n>.json` | `S_FAIL_QUALITY` |
| `Ax_AdaptiveChange` | 当工具/契约/提示词不足时做小幅方法论修补 | `adaptive_change_record.json`、前后 manifest | `S_FAIL_CAPABILITY` |
| `S9_VerifyProcessContract` | 验证状态 trace、操作日志、反过拟合和最终审计 | `process_validation.json`、`anti_overfit_scan.json`、`final_acceptance.json` | `S_FAIL_PROCESS_CONTRACT` |

成功终态：

```text
S_DONE_PRODUCT_ACCEPTED
S_DONE_PROCESS_VALIDATED
```

失败终态：

```text
S_FAIL_PROCESS_CONTRACT
S_FAIL_TOOLING
S_FAIL_CAPABILITY
S_FAIL_QUALITY
```

### 4.1 主状态机图

```mermaid
stateDiagram-v2
  [*] --> S0_Request
  S0_Request --> S1_ContractLoad: 输入和 run mode 明确
  S1_ContractLoad --> S2_ToolProbe: 契约/工具/提示词可读
  S1_ContractLoad --> S_FAIL_PROCESS_CONTRACT: 契约缺失

  S2_ToolProbe --> S3_SourceExtract: 工具能力满足
  S2_ToolProbe --> S_FAIL_TOOLING: 必需工具不可用

  S3_SourceExtract --> S4_PageStrategy: 源结构和源渲染完整
  S3_SourceExtract --> S_FAIL_TOOLING: PDF 无法提取或渲染

  S4_PageStrategy --> S5_TranslationPlan: 页面策略有证据
  S4_PageStrategy --> S_FAIL_PROCESS_CONTRACT: 页面策略缺失或无依据

  S5_TranslationPlan --> S6_LayoutPlan: 语义译文覆盖目标文本且通过真实性校验
  S5_TranslationPlan --> S_FAIL_CAPABILITY: 缺译/placeholder/元描述式伪译文/非语义译文

  S6_LayoutPlan --> S7_GenerateCandidate: 布局策略可追溯
  S6_LayoutPlan --> S_FAIL_PROCESS_CONTRACT: 布局策略缺失或过拟合

  S7_GenerateCandidate --> S8_VerifyProductQuality: 候选 PDF 和生成证据存在
  S7_GenerateCandidate --> S_FAIL_TOOLING: 生成工具失败
  S7_GenerateCandidate --> S_FAIL_CAPABILITY: 生成能力不足

  S8_VerifyProductQuality --> S9_VerifyProcessContract: 产品质量 blocking gate 全通过
  S8_VerifyProductQuality --> Lx_RepairLoop: 发现可修复质量失败
  S8_VerifyProductQuality --> S_FAIL_QUALITY: 不可修或 loop 耗尽

  Lx_RepairLoop --> S6_LayoutPlan: 需要重建布局策略
  Lx_RepairLoop --> S7_GenerateCandidate: 只需重新生成候选
  Lx_RepairLoop --> Ax_AdaptiveChange: 工具/契约/提示词表达能力不足
  Lx_RepairLoop --> S_FAIL_QUALITY: 修复失败或重复失败

  Ax_AdaptiveChange --> S6_LayoutPlan: 方法论修补影响布局策略
  Ax_AdaptiveChange --> S8_VerifyProductQuality: 只补证据或裁决维度
  Ax_AdaptiveChange --> S_FAIL_CAPABILITY: 当前轮无法补齐能力

  S_FAIL_TOOLING --> S9_VerifyProcessContract: 记录失败审计
  S_FAIL_CAPABILITY --> S9_VerifyProcessContract: 记录失败审计
  S_FAIL_QUALITY --> S9_VerifyProcessContract: 记录失败审计

  S9_VerifyProcessContract --> S_DONE_PRODUCT_ACCEPTED: 产品和过程均通过
  S9_VerifyProcessContract --> S_DONE_PROCESS_VALIDATED: 过程通过但产品未通过
  S9_VerifyProcessContract --> S_FAIL_PROCESS_CONTRACT: trace 或证据不可信

  S_DONE_PRODUCT_ACCEPTED --> [*]
  S_DONE_PROCESS_VALIDATED --> [*]
  S_FAIL_PROCESS_CONTRACT --> [*]
```

### 4.2 Loop 与主状态机调用关系图

```mermaid
flowchart TD
  S8[S8_VerifyProductQuality<br/>质量门禁和视觉裁决] -->|blocking gate pass| S9[S9_VerifyProcessContract]
  S8 -->|repairable failure| L0[Lx_RepairLoop<br/>进入一次修复循环]
  S8 -->|unrepairable or budget exhausted| FQ[S_FAIL_QUALITY]

  L0 --> L1[读取 product_quality_gates<br/>和 visual_adjudication]
  L1 --> L2[归类 failure_class]
  L2 --> L3[选择 repair_atom]
  L3 --> L4[写 repair_loop_n.json]
  L4 --> L5[执行最小修复]
  L5 --> L6[重新生成或重新裁决]
  L6 --> L7[验证目标 gate]

  L7 -->|布局策略改变| S6[S6_LayoutPlan]
  L7 -->|只需候选重建| S7[S7_GenerateCandidate]
  L7 -->|工具/契约/提示词不足| AX[Ax_AdaptiveChange]
  L7 -->|仍失败且不可继续| FQ

  AX -->|修补后影响布局| S6
  AX -->|修补后只影响证据裁决| S8
  AX -->|无法补齐能力| FC[S_FAIL_CAPABILITY]

  FQ --> S9
  FC --> S9
```

### 4.3 Loop 内部状态图

```mermaid
stateDiagram-v2
  [*] --> L0_EnterLoop
  L0_EnterLoop --> L1_ReadEvidence
  L1_ReadEvidence --> L2_ClassifyFailure
  L2_ClassifyFailure --> L3_SelectRepairAtom
  L3_SelectRepairAtom --> L4_WriteRepairRecord
  L4_WriteRepairRecord --> L5_ApplyMinimalChange
  L5_ApplyMinimalChange --> L6_RebuildOrRejudge
  L6_RebuildOrRejudge --> L7_VerifyTargetGate

  L7_VerifyTargetGate --> L_DONE_REPAIRED: gate fixed
  L7_VerifyTargetGate --> L2_ClassifyFailure: still repairable
  L7_VerifyTargetGate --> L_FAIL_UNREPAIRABLE: no repair atom
  L7_VerifyTargetGate --> L_FAIL_REPEAT: repeated same failure

  L_DONE_REPAIRED --> [*]
  L_FAIL_UNREPAIRABLE --> [*]
  L_FAIL_REPEAT --> [*]
```

## 5. 状态调度表

| 状态 | 必须调用的工具 | 大模型裁决 | next_state |
|---|---|---|---|
| `S1_ContractLoad` | 文件读取 | 否 | 契约齐全 -> `S2` |
| `S2_ToolProbe` | `tools\probes\tool_probe.py` | 否 | 工具可用 -> `S3` |
| `S3_SourceExtract` | `extract_pdf_structure.py`、`render_pdf.py` | 否 | 源证据完整 -> `S4` |
| `S4_PageStrategy` | 无必需工具，读取 S3 产物 | `D1_page_strategy.prompt.json` | 页面策略完整 -> `S5` |
| `S5_TranslationPlan` | `validate_semantic_translations.py` | `D2_translation.prompt.json` | 译文有效 -> `S6` |
| `S6_LayoutPlan` | `build_layout_policy.py` | `D4_layout_plan.prompt.json` | 布局策略可追溯 -> `S7` |
| `S7_GenerateCandidate` | `generate_semantic_backfill.py` | 否 | 候选生成 -> `S8` |
| `S8_VerifyProductQuality` | `render_pdf.py`、`collect_visual_region_metrics.py`、`render_source_output_crop.py`、`evaluate_pdf_quality.py`、`plan_visual_region_repairs.py` | `D5_D7_quality_gate.prompt.json` | 全 pass -> `S9`；可修 -> `Lx`；不可修 -> fail |
| `Lx_RepairLoop` | 按 failure_class 选择工具 | `D8_repair_selection.prompt.json` | 回到 `S6` 或 `S7` |
| `S9_VerifyProcessContract` | `validate_process_artifacts.py`、`scan_core_overfit.py` | `D9_final_acceptance.prompt.json` | 输出最终终态 |

## 6. 提示词边界

执行轮次只能使用：

```text
pdf_translation_workflow_core\prompts\templates\D1_page_strategy.prompt.json
pdf_translation_workflow_core\prompts\templates\D2_translation.prompt.json
pdf_translation_workflow_core\prompts\templates\D4_layout_plan.prompt.json
pdf_translation_workflow_core\prompts\templates\D5_D7_quality_gate.prompt.json
pdf_translation_workflow_core\prompts\templates\D8_repair_selection.prompt.json
pdf_translation_workflow_core\prompts\templates\D9_final_acceptance.prompt.json
```

允许调整：

```text
round 本地路径
run_id / case_id
当前页码和裁剪图引用
当前 metrics 和 gate summary
工具输出 JSON 的读取包装
```

禁止调整：

```text
新造 D4/D7/D8/D9 判断提示词
删除必填质量维度
把 blocking gate 改为非 blocking
用 placeholder 译文通过 product_quality
用“本行说明/This line reports”这类元描述式伪译文通过 product_quality
没有 backrotated crop 就判侧栏字形方向通过
把样本文件名、固定页码、固定坐标、固定文本写成规则
```

如果提示词模板确实不够用，进入 `Ax_AdaptiveChange`，记录原因、变更文件、兼容性和验证结果。

## 7. 工具契约

### 7.1 探测

```powershell
python pdf_translation_workflow_core\tools\probes\tool_probe.py --out docs\reports\tool_probe.json
```

### 7.2 源 PDF 提取

```powershell
python pdf_translation_workflow_core\tools\probes\extract_pdf_structure.py --input <source_pdf> --out docs\reports\<run_id>\source_extraction.json
```

### 7.3 PDF 渲染

```powershell
python pdf_translation_workflow_core\tools\renderers\render_pdf.py --input <pdf> --out-dir docs\reports\<run_id>\<preview_dir> --prefix <prefix> --manifest docs\reports\<run_id>\<manifest>.json
```

### 7.4 语义译文校验

```powershell
python pdf_translation_workflow_core\tools\validators\validate_semantic_translations.py --source-extraction docs\reports\<run_id>\source_extraction.json --translations docs\input\semantic_translations\<run_id>.translations.json --out docs\reports\<run_id>\semantic_translation_validation.json
```

校验必须同时覆盖：

```text
unit_id/source_text 精确匹配
source_language/target_language/target_text_field 正确
目标语字段非空且符合目标语字符规则
数字、年份、百分比、货币、脚注标记保留
禁止 placeholder
禁止元描述式伪译文：本行说明、本行列示、This line reports、This line describes、当前页的财务报告/治理/业务信息、保留数值与标记/preserve figures markers
layout_variants 也必须满足同样的真实性规则
compact_label、short_label、table_cell、table_header、legend、chart_label、vertical_nav 等受限槽位必须提供对应目标语 layout_variants；缺失时 S7 不得自行发明缩写，只能用全文尝试排版并把溢出记录为 text_fit/D2-D4 修复输入
```

### 7.5 布局策略

```powershell
python pdf_translation_workflow_core\tools\planners\build_layout_policy.py --source-extraction docs\reports\<run_id>\source_extraction.json --semantic-translations docs\input\semantic_translations\<run_id>.translations.json --language-profile pdf_translation_workflow_core\profiles\<source_language>_to_<target_language>.layout_profile.json --out docs\reports\<run_id>\layout_policy.json
```

`layout_policy.json` 至少要表达：

```text
classification_rules.table_note
classification_rules.event_card
classification_rules.table_cell
classification_rules.legend
classification_rules.vertical_nav
reflow.reflow_kinds / preserve_line_kinds
flow_grouping.body.enabled
flow_grouping.body.min_region_count
flow_grouping.body.max_x0_delta_pt
flow_grouping.body.max_width_delta_ratio
flow_grouping.body.max_vertical_gap_pt
flow_grouping.body.paragraph_gap_pt
flow_grouping.body.line_joiner_en / line_joiner_zh
flow_grouping.body.disable_page_type_guesses
flow_grouping.body.allow_short_continuation_lines
flow_grouping.body.min_continuation_width_page_ratio
flow_grouping.body.allow_dense_page_body_below_y_ratio
target_composition.enabled
target_composition.region_kinds
target_composition.min_width_page_ratio / max_width_page_ratio
target_composition.height_expand_ratio
target_composition.max_bottom_page_ratio
target_composition.overlap_guard
target_language_reflow.enabled
target_language_reflow.region_kinds
target_language_reflow.allow_dense_page_body_below_y_ratio
target_language_reflow.overlap_guard
layout_text_variants 中 compact_label、short_label、table_cell、legend 的目标语字段
font_profiles 中 event_card、table_cell、legend、table_note、footnote、body、body_flow、heading
source_separator_policy
draw_modes.vertical_nav
fallback policy
```

正文流规则：

```text
aligned body regions in the same column -> may merge into body_flow
mixed image/text timeline or milestone narrow descriptions -> event_card, local reflow only, never body_flow
same-paragraph wrapped lines -> join with target-language line_joiner
paragraph y-gap >= paragraph_gap_pt -> join with paragraph_separator
short continuation lines may join only when an active body_flow exists, x0 is aligned, y-gap is within max_vertical_gap_pt, and width exceeds min_continuation_width_page_ratio
dense table/chart page -> preserve table cells and legends, but allow body_flow below allow_dense_page_body_below_y_ratio when current-run geometry proves it is a body copy band
target_composition applies only to fluid body/body_flow; it uses source bbox as anchor evidence and recomputes target frame from current-page body band before font shrink
target_language_reflow may expand only declared region_kinds and must obey overlap_guard so expanded text does not invade the next same-column region
```

### 7.6 候选 PDF 生成

```powershell
python pdf_translation_workflow_core\tools\generators\generate_semantic_backfill.py --input <source_pdf> --source-extraction docs\reports\<run_id>\source_extraction.json --semantic-translations docs\input\semantic_translations\<run_id>.translations.json --layout-policy docs\reports\<run_id>\layout_policy.json --output docs\output\<run_id>_semantic_backfill_candidate.pdf --evidence docs\reports\<run_id>\candidate_generation_evidence.json --translations docs\reports\<run_id>\translations.used.json --layout-plan docs\reports\<run_id>\layout_plan.json
```

`product_quality` 模式禁止用 placeholder 生成器冒充产品候选。

生成器必须先在临时预检页执行 textbox fit 尝试；失败的字号尝试不得绘制到真实候选 PDF。真实页只允许绘制最终 fit 成功字号，或记录明确 fallback。`candidate_generation_evidence.json` 中每个 insertion 必须记录：

```text
attempts
status
font_size
region_kind
layout_mode
target_language_reflow_applied
target_language_reflow_profile
target_composition_applied
target_composition_profile
source_anchor_bbox
source_block_ids
source_line_indexes
semantic_translated_unit_count
preserved_target_language_unit_count
```

### 7.7 质量门禁

先采集块级视觉指标：

```powershell
python pdf_translation_workflow_core\tools\validators\collect_visual_region_metrics.py --source <source_pdf> --output docs\output\<run_id>_semantic_backfill_candidate.pdf --generation-evidence docs\reports\<run_id>\candidate_generation_evidence.json --source-extraction docs\reports\<run_id>\source_extraction.json --out docs\reports\<run_id>\visual_region_metrics.json --crop-dir docs\reports\<run_id>\visual_region_crops
```

再生成修复计划：

```powershell
python pdf_translation_workflow_core\tools\repairs\plan_visual_region_repairs.py --visual-region-metrics docs\reports\<run_id>\visual_region_metrics.json --out docs\reports\<run_id>\visual_repair_plan.json
```

最后合并机器 gate、视觉裁决和块级门禁：

```powershell
python pdf_translation_workflow_core\tools\validators\evaluate_pdf_quality.py --source <source_pdf> --output docs\output\<run_id>_semantic_backfill_candidate.pdf --out docs\reports\<run_id>\product_quality_gates.json --generation-evidence docs\reports\<run_id>\candidate_generation_evidence.json --visual-adjudication docs\reports\<run_id>\visual_adjudication.json --visual-region-metrics docs\reports\<run_id>\visual_region_metrics.json
```

### 7.8 局部裁剪对比

```powershell
python pdf_translation_workflow_core\tools\renderers\render_source_output_crop.py --source <source_pdf> --output docs\output\<run_id>_semantic_backfill_candidate.pdf --page-index <zero_based_page_index> --crop "<x0,y0,x1,y1>" --out docs\reports\<run_id>\compare\<crop_name>_source_vs_output.png --manifest docs\reports\<run_id>\compare\<crop_name>_source_vs_output.json
```

侧栏横排整体旋转检查必须额外输出：

```powershell
--backrotate-output-degrees -90 --backrotate-output-out docs\reports\<run_id>\compare\<crop_name>_backrotated_output.png
```

### 7.9 过程与反过拟合验证

```powershell
python pdf_translation_workflow_core\tools\validators\scan_core_overfit.py --root pdf_translation_workflow_core --token-file docs\reports\<round_id>\anti_overfit_tokens.json --out docs\reports\<round_id>\anti_overfit_scan.json
python pdf_translation_workflow_core\tools\validators\validate_process_artifacts.py --run-dir docs\reports\<round_id> --out docs\reports\<round_id>\process_validation.json
```

### 7.10 自检线束

自检线束如需使用，必须放在 `docs\offline_reference_evaluation\tools` 或具体 round 报告目录下。它可用于验证工具链和状态机证据结构是否能跑通，但它不是完整执行主体，不能替代新 Codex 写 `state_trace.json`、`operation_log.jsonl`、`decision_log.jsonl`、视觉裁决和最终审计报告。

```powershell
python docs\offline_reference_evaluation\tools\run_state_machine_selftest.py --modes product_quality --generator semantic_backfill --semantic-translations-dir docs\input\semantic_translations --process-doc docs\业务流程\PDF_中文回填_标准流程设计.md --out-dir docs\reports\selftest
```

## 8. 质量 gate

产品质量通过必须至少满足：

```text
page_count pass
page_geometry pass
text_residue pass
translation_authenticity pass
semantic_translation_preflight pass
semantic_coverage pass
text_fit pass
source_relative_visual_baseline pass
source_anchor_order pass
visual_similarity pass
hero_banner_text_readability pass when hero/banner title exists
title_readability pass when title/heading exists
body_paragraph_readability pass when body/body_flow exists
table_text_legibility pass when table cells or table headers exist
image_color_integrity pass
```

存在侧栏时还必须判断：

```text
sidebar_orientation_group_consistency
sidebar_glyph_orientation
```

### 8.1 块级视觉采集

`S8_VerifyProductQuality` 不能只看整页相似度。候选渲染后必须先采集块级视觉指标：

```powershell
python pdf_translation_workflow_core\tools\validators\collect_visual_region_metrics.py --source <source_pdf> --output docs\output\<run_id>_semantic_backfill_candidate.pdf --generation-evidence docs\reports\<run_id>\candidate_generation_evidence.json --source-extraction docs\reports\<run_id>\source_extraction.json --out docs\reports\<run_id>\visual_region_metrics.json --crop-dir docs\reports\<run_id>\visual_region_crops
```

该工具必须输出：

```text
page_metrics: 图片数量、页面平均颜色、主色差异
region_metrics: 每个插入区的 quality_role、font_size、source_median_font_size、output_to_source_font_ratio、generation_status、background_delta、crop_evidence、reasons、repair_atoms
role_gates: source_relative_visual_baseline、hero_banner_text_readability、title_readability、body_paragraph_readability、table_text_legibility、footnote_readability、legend_label_alignment、sidebar_navigation_legibility、event_card_readability、image_color_integrity
```

阻塞规则：

```text
source_relative_visual_baseline = fail -> 产品质量 fail；不能用固定阈值替代源文-译文对比证据
hero/banner/title/body/table/sidebar/image 等 critical role 的 role gate = fail -> 产品质量 fail
非关键 footnote/short_label 可以 warn，但如果影响整体可读性，D7 仍可升级为 fail
整页 visual_similarity 不能覆盖块级 critical fail
```

典型例子：

```json
{
  "gate_id": "hero_banner_text_readability",
  "status": "fail",
  "sample": [
    {
      "quality_role": "hero_banner_title",
      "generation_status": "fallback_insert_text",
      "font_size": 3.4,
      "repair_atoms": ["heading_frame_fit_or_short_title_variant"]
    }
  ]
}
```

`evaluate_pdf_quality.py` 必须消费该文件：

```powershell
python pdf_translation_workflow_core\tools\validators\evaluate_pdf_quality.py --source <source_pdf> --output docs\output\<run_id>_semantic_backfill_candidate.pdf --out docs\reports\<run_id>\product_quality_gates.json --generation-evidence docs\reports\<run_id>\candidate_generation_evidence.json --visual-adjudication docs\reports\<run_id>\visual_adjudication.json --visual-region-metrics docs\reports\<run_id>\visual_region_metrics.json
```

修复规划：

```powershell
python pdf_translation_workflow_core\tools\repairs\plan_visual_region_repairs.py --visual-region-metrics docs\reports\<run_id>\visual_region_metrics.json --out docs\reports\<run_id>\visual_repair_plan.json
```

`sidebar_glyph_orientation` 的通过条件：

```text
候选侧栏裁剪图反向旋转后，中文标签可以横向阅读。
```

`source_anchor_order` 的通过条件：

```text
一个中文 reflow region 不能跨过同一源 block 内未被翻译但可见的源分隔行。
源分隔行包括年份、数字标题、项目符号、纯分隔标签等。
生成证据必须包含 source_block_ids 和 source_line_indexes。
如果同一 region 的同一 block 行号从 1 跳到 3，说明中间第 2 行被跨过，必须进入质量失败或修复循环。
```

## 9. 修复循环

`Lx_RepairLoop` 是组合状态。

每次只修一个主要 failure_class：

```text
读取 product_quality_gates.json
读取 visual_adjudication.json
选取一个阻塞失败
映射到 repair_atom
写 repair_loop_<n>.json
做最小变更
重新生成候选或重新验证
回到 S8
```

常见映射：

| failure_class | repair_atom | 回到状态 |
|---|---|---|
| `semantic_translation_authenticity_fail` | `regenerate_D2_translation_without_meta_description` | `S5` |
| `line_fragmentation` | `body_flow_region_reflow` | `S6` |
| `source_anchor_order_mismatch` | `split_region_at_source_separator` | `S6` |
| `paragraph_density_mismatch` | `font_size_and_region_density_rebalance` | `S6` |
| `single_dense_paragraph` | `body_flow_paragraph_gap_rebalance` | `S6` |
| `body_flow_fallback_truncation` | `short_continuation_and_reflow_frame_repair` | `S6` |
| `dense_page_body_band_fragmentation` | `dense_page_body_band_flow_repair` | `S6` |
| `failed_probe_residue` | `textbox_probe_isolation_repair` | `S7` |
| `font_hierarchy_ratio_mismatch` | `font_hierarchy_profile_repair` | `S6` |
| `sidebar_glyph_orientation_fail` | `rotated_horizontal_text_image_draw_mode` | `S6` |
| `side_nav_group_consistency_fail` | `side_nav_group_writing_mode_policy` | `S6` |
| `text_fit_overflow` | `region_fit_repair` | `S6` |
| `visual_similarity_fail` | `visual_similarity_targeted_repair` | `S6` |
| `table_integrity_fail` | `table_cell_variant_or_grid_preserve_repair` | `S6` |

## 10. 小幅方法论变更

允许新执行器为了跑通流程做小幅变更，但必须满足：

```text
不改变既有状态语义
不删除必填 gate
不降低 product_quality 标准
不引入样本硬编码
不静默修改提示词框架
```

必须记录：

```text
docs\reports\adaptive_change_record.json
docs\reports\change_manifest_before.json
docs\reports\change_manifest_after.json
```

最终报告必须说明：

```text
改了什么
为什么改
是否应该反向合并到核心流程
是否影响本轮结论可信度
```

## 11. 反过拟合

生产工具、契约、提示词不能出现样本分支。

必须运行：

```powershell
python pdf_translation_workflow_core\tools\validators\scan_core_overfit.py --root pdf_translation_workflow_core --token-file docs\reports\<round_id>\anti_overfit_tokens.json --out docs\reports\<round_id>\anti_overfit_scan.json
```

通过条件：

```text
blocking_hit_count == 0
```

`anti_overfit_tokens.json` 是本轮外部扫描输入，不属于 core。它可以包含本轮 PDF 文件名片段、已知页码组合、人工反馈中出现的专有名词、年份、财务指标缩写、标题词等敏感 token，用来证明这些 token 没有进入 core 的工具、契约或提示词。

`pdf_translation_workflow_core` 内部不允许保留样本事实目录。历史回归证据、官方中英对照、人工参考结果、样本 token 文件和回放脚本必须位于 `docs\offline_reference_evaluation` 或本轮 `docs\reports\<round_id>`。

## 12. 最终报告

每轮必须输出最终审计报告。根目录验证使用：

```text
docs\reports\<round_id>\execution_audit.md
```

报告必须包含：

```text
executed_state_sequence
tool_invocation_summary
prompt_templates_used
prompt_slot_values_summary
model_decision_records
candidate_pdf_paths
quality_gate_summary
repair_loop_summary
adaptive_changes
anti_overfit_scan_summary
process_contract_verdict
product_quality_verdict
terminal_state
requires_core_revision
```

最终 JSON：

```json
{
  "round": "<round_id>",
  "run_mode": "product_quality",
  "state_machine_followed": "PASS|FAIL",
  "tool_orchestration_followed": "PASS|FAIL",
  "prompt_template_boundary_followed": "PASS|FAIL",
  "semantic_translation_validation": "PASS|FAIL",
  "semantic_candidate_generation": "PASS|FAIL",
  "anti_overfit_scan": "PASS|FAIL",
  "process_contract_verdict": "PASS|FAIL",
  "product_quality_verdict": "PASS|FAIL|NOT_ATTEMPTED",
  "terminal_state": "S_DONE_PRODUCT_ACCEPTED|S_DONE_PROCESS_VALIDATED|S_FAIL_QUALITY|S_FAIL_CAPABILITY|S_FAIL_PROCESS_CONTRACT|S_FAIL_TOOLING",
  "adaptive_changes_made": true,
  "adaptive_change_summary": [],
  "design_gaps_found": [],
  "requires_core_revision": true
}
```

## 13. 最低诚实标准

不能因为流程跑完就说产品质量通过。

不能因为候选 PDF 生成就说视觉质量通过。

不能因为用了中文就说语义翻译通过。

不能因为侧栏旋转了就说字形方向通过，必须看反向旋转裁剪图。

不能因为没有发现问题就省略 `state_trace.json`、`operation_log.jsonl` 或最终审计报告。

## 14. 关键设计视图

本节补足面向执行器的系统设计视图。它不记录历史轮次，只描述当前标准流程应该如何被一个新的 Codex 会话执行。

### 14.1 系统上下文（对应 10.1 System Context）

系统边界：

```text
PDF 中文回填执行系统 = 新 Codex 会话 + pdf_translation_workflow_core + round 工作目录内的输入/输出/报告
```

外部参与者：

| 参与者 | 职责 | 与系统交互 |
|---|---|---|
| 用户 | 指定输入 PDF、目标、输出目录、验收关注点 | 提供任务；查看最终 PDF 和审计报告 |
| 新 Codex 执行器 | 按本文件调度状态机、工具和裁决 | 读取流程文档；运行工具；写 trace、report、candidate PDF |
| 大模型裁决器 | 对 D1/D2/D4/D5-D9 做结构化判断 | 只接收工具证据和模板槽位；输出 JSON 裁决 |
| 本地文件系统 | 保存输入、候选 PDF、证据和报告 | 所有状态产物都必须落盘 |
| PDF 工具链 | 提取、渲染、擦除、回填、裁剪、质量检测 | 由状态机显式调用 |

系统输入：

```text
source_pdf
semantic_translations_json
workflow_core
standard_process_design
round_execution_prompt
```

系统输出：

```text
candidate_pdf
state_trace.json
operation_log.jsonl
product_quality_gates.json
visual_adjudication.json
anti_overfit_scan.json
round_execution_audit.md
```

上下文约束：

```text
不读取旧轮次输出作为产品质量证据
不把样本事实写进生产工具逻辑
不跳过状态 trace
不跳过最终审计
不把过程通过等同于产品通过
```

### 14.2 Block Definition Diagram（对应 10.2 BDD）

以下是文本化 BDD。每个 block 都是执行器必须理解的职责边界。

| Block | 类型 | 职责 | 输入 | 输出 |
|---|---|---|---|---|
| `WorkflowRun` | system | 管理一次 round 的状态、输入、输出、终态 | 用户目标、round root | state trace、final report |
| `ContractLoader` | service block | 读取流程设计、契约、提示词绑定 | Markdown、JSON contracts | contract load record |
| `ToolProbe` | service block | 探测 Python/PDF/font/render 能力 | round environment | tool_probe.json |
| `SourceExtractor` | service block | 提取 PDF 页面、文字、bbox、字体、图像、绘图对象 | source_pdf | source_extraction.json |
| `SourceRenderer` | service block | 将源 PDF 和候选 PDF 渲染成 PNG | PDF | render manifest、PNG |
| `PageStrategyJudge` | model-judge block | 判断页面类型和区域角色 | source extraction、render refs | page_strategy.json |
| `SemanticTranslationPackage` | data block | 保存语义译文和术语决策 | source units | translations.json |
| `TranslationValidator` | service block | 校验译文覆盖、真实性和 token 保留 | source extraction、translations | semantic_translation_validation.json |
| `LayoutPolicyPlanner` | service/model block | 生成并裁决布局策略 | extraction stats、translations、font capabilities | layout_policy.json |
| `CandidateGenerator` | service block | 擦除英文并回填中文 | source_pdf、translations、layout_policy | candidate_pdf、generation evidence |
| `CropEvidenceRenderer` | service block | 生成局部源/候选裁剪和反向旋转证据 | source_pdf、candidate_pdf、crop bbox | crop PNG、crop manifest |
| `ProductQualityEvaluator` | service/model block | 计算机器 gate 并合并视觉裁决 | source/candidate/evidence | product_quality_gates.json |
| `RepairLoopController` | control block | 将阻塞失败映射到 repair atom | failed gates、repair matrix | repair_loop_<n>.json |
| `AdaptiveChangeController` | control block | 管理小幅方法论变更 | design/tool gap | adaptive change records |
| `AntiOverfitScanner` | validation block | 扫描生产 core 是否含样本规则 | workflow core | anti_overfit_scan.json |
| `ProcessValidator` | validation block | 验证状态 trace 和产物完整性 | run directory | process_validation.json |
| `FinalAcceptanceJudge` | model-judge block | 输出过程/产品双 verdict 和终态 | all summaries | final_acceptance.json |

BDD 关系：

```text
WorkflowRun
  contains ContractLoader
  contains ToolProbe
  contains SourceExtractor
  contains SourceRenderer
  contains PageStrategyJudge
  contains SemanticTranslationPackage
  contains TranslationValidator
  contains LayoutPolicyPlanner
  contains CandidateGenerator
  contains CropEvidenceRenderer
  contains ProductQualityEvaluator
  contains RepairLoopController
  contains AdaptiveChangeController
  contains AntiOverfitScanner
  contains ProcessValidator
  contains FinalAcceptanceJudge

CandidateGenerator depends on LayoutPolicyPlanner and SemanticTranslationPackage
ProductQualityEvaluator depends on CandidateGenerator, SourceRenderer, CropEvidenceRenderer
RepairLoopController depends on ProductQualityEvaluator and page_type_repair_matrix
FinalAcceptanceJudge depends on ProcessValidator, AntiOverfitScanner, ProductQualityEvaluator
```

### 14.3 Internal Block Flow

核心 artifact 流：

```text
source_pdf
  -> SourceExtractor
  -> source_extraction.json
  -> PageStrategyJudge
  -> page_strategy.json
  -> LayoutPolicyPlanner
  -> layout_policy.json
  -> CandidateGenerator
  -> candidate_pdf
  -> ProductQualityEvaluator
  -> product_quality_gates.json
```

语义译文流：

```text
source_extraction.json
  -> SemanticTranslationPackage
  -> translations.json
  -> TranslationValidator
  -> semantic_translation_validation.json
  -> CandidateGenerator
```

视觉证据流：

```text
source_pdf + candidate_pdf
  -> SourceRenderer
  -> full page PNGs
  -> CropEvidenceRenderer
  -> source-vs-output crop PNG
  -> optional backrotated output crop
  -> visual_adjudication.json
  -> ProductQualityEvaluator
```

审计流：

```text
state transitions + tool invocations + model decisions + gate results
  -> state_trace.json
  -> operation_log.jsonl
  -> process_validation.json
  -> final_acceptance.json
  -> round_execution_audit.md
```

## 15. 状态机详细设计

本节是执行状态机的规范视图。第 4 节是简表；本节定义每个状态的准入、动作、不变量和迁移条件。

### 15.1 通用主状态机图

```mermaid
stateDiagram-v2
  [*] --> S0_Request

  S0_Request --> S1_ContractLoad: inputs declared
  S1_ContractLoad --> S2_ToolProbe: contracts loaded
  S1_ContractLoad --> S_FAIL_PROCESS_CONTRACT: required contract missing

  S2_ToolProbe --> S3_SourceExtract: required tools available
  S2_ToolProbe --> S_FAIL_TOOLING: required tool unavailable

  S3_SourceExtract --> S4_PageStrategy: source structure and renders exist
  S3_SourceExtract --> S_FAIL_TOOLING: PDF unreadable or extraction failed

  S4_PageStrategy --> S5_TranslationPlan: D1 page strategy recorded
  S4_PageStrategy --> S_FAIL_PROCESS_CONTRACT: page strategy incomplete

  S5_TranslationPlan --> S6_LayoutPlan: semantic translations validated
  S5_TranslationPlan --> S_FAIL_CAPABILITY: missing/placeholder/meta-description/incomplete translation

  S6_LayoutPlan --> S7_GenerateCandidate: traceable layout_policy exists
  S6_LayoutPlan --> S_FAIL_PROCESS_CONTRACT: policy missing or overfit

  S7_GenerateCandidate --> S8_VerifyProductQuality: candidate PDF and evidence exist
  S7_GenerateCandidate --> S_FAIL_TOOLING: generator failed
  S7_GenerateCandidate --> S_FAIL_CAPABILITY: required generation capability missing

  S8_VerifyProductQuality --> S9_VerifyProcessContract: all blocking product gates pass
  S8_VerifyProductQuality --> Lx_RepairLoop: repairable blocking quality failure
  S8_VerifyProductQuality --> S_FAIL_QUALITY: no valid repair

  Lx_RepairLoop --> S6_LayoutPlan: repair requires policy/layout change
  Lx_RepairLoop --> S7_GenerateCandidate: repair only requires regeneration
  Lx_RepairLoop --> Ax_AdaptiveChange: workflow/tool/prompt gap blocks repair
  Lx_RepairLoop --> S_FAIL_QUALITY: repeated or unrepairable failure

  Ax_AdaptiveChange --> S6_LayoutPlan: method change verified; rerun from policy
  Ax_AdaptiveChange --> S8_VerifyProductQuality: evidence-only change verified
  Ax_AdaptiveChange --> S_FAIL_CAPABILITY: gap cannot be resolved in round

  S_FAIL_TOOLING --> S9_VerifyProcessContract: write failure audit
  S_FAIL_CAPABILITY --> S9_VerifyProcessContract: write failure audit
  S_FAIL_QUALITY --> S9_VerifyProcessContract: write failure audit
  S_FAIL_PROCESS_CONTRACT --> [*]

  S9_VerifyProcessContract --> S_DONE_PRODUCT_ACCEPTED: process and product pass
  S9_VerifyProcessContract --> S_DONE_PROCESS_VALIDATED: process pass only
  S9_VerifyProcessContract --> S_FAIL_PROCESS_CONTRACT: trace/evidence invalid

  S_DONE_PRODUCT_ACCEPTED --> [*]
  S_DONE_PROCESS_VALIDATED --> [*]
```

图上约束：

```text
S8 是唯一能进入产品质量修复 loop 的入口。
Lx_RepairLoop 不是线性状态清单，而是可重复执行的复合状态。
Ax_AdaptiveChange 只处理方法论/工具/契约不足，不直接宣称产品质量修复成功。
任何失败终态仍必须进入 S9 写最终审计，除非 S9 自身的过程证据缺失。
```

### 15.2 RepairLoop 复合状态图

```mermaid
stateDiagram-v2
  [*] --> L0_EnterLoop
  L0_EnterLoop --> L1_ReadQualityEvidence
  L1_ReadQualityEvidence --> L2_ClassifyFailure
  L2_ClassifyFailure --> L3_SelectRepairAtom
  L3_SelectRepairAtom --> L4_WriteRepairPlan
  L4_WriteRepairPlan --> L5_ApplyMinimalChange
  L5_ApplyMinimalChange --> L6_RegenerateOrRejudge
  L6_RegenerateOrRejudge --> L7_VerifyTargetGate

  L7_VerifyTargetGate --> L_DONE_REPAIRED: target gate fixed
  L7_VerifyTargetGate --> L2_ClassifyFailure: still failing and repairable
  L7_VerifyTargetGate --> L_FAIL_UNREPAIRABLE: no valid repair atom
  L7_VerifyTargetGate --> L_FAIL_REPEAT: same failure repeats without new evidence

  L5_ApplyMinimalChange --> Ax_AdaptiveChange: contract/tool/prompt cannot express needed repair

  L_DONE_REPAIRED --> [*]
  L_FAIL_UNREPAIRABLE --> [*]
  L_FAIL_REPEAT --> [*]
```

RepairLoop 和主状态机的连接规则：

```text
L_DONE_REPAIRED -> S6 or S7，根据 repair_atom 是否改变 layout policy。
L_FAIL_UNREPAIRABLE -> S_FAIL_QUALITY。
L_FAIL_REPEAT -> S_FAIL_QUALITY，最终报告必须说明重复失败证据。
```

一次 RepairLoop 必须写：

```json
{
  "loop_iteration": 1,
  "entered_from_state": "S8_VerifyProductQuality",
  "failure_class": "sidebar_glyph_orientation_fail",
  "failed_gate_ids": ["sidebar_glyph_orientation"],
  "repair_atom": "rotated_horizontal_text_image_draw_mode",
  "changed_files": ["..."],
  "verification_to_run": ["render crop", "backrotate output crop", "rerun quality gate"],
  "exit_decision": "retry|done|fail_quality"
}
```

### 15.3 AdaptiveChange 复合状态图

```mermaid
stateDiagram-v2
  [*] --> A0_Trigger
  A0_Trigger --> A1_RecordBeforeManifest
  A1_RecordBeforeManifest --> A2_DiagnoseDesignGap
  A2_DiagnoseDesignGap --> A3_ApplyRoundLocalChange: minimal change allowed
  A2_DiagnoseDesignGap --> A_FAIL_CAPABILITY: required capability missing
  A3_ApplyRoundLocalChange --> A4_RecordAfterManifest
  A4_RecordAfterManifest --> A5_ReverifyChangedContract
  A5_ReverifyChangedContract --> A_DONE_RETURN: change sufficient
  A5_ReverifyChangedContract --> A2_DiagnoseDesignGap: still insufficient
  A_DONE_RETURN --> [*]
  A_FAIL_CAPABILITY --> [*]
```

AdaptiveChange 不能静默发生。每次必须写：

```text
adaptive_change_record.json
change_manifest_before.json
change_manifest_after.json
```

并在最终审计报告中写明：

```text
为什么原设计不足
改了哪些文件
是否改变裁决维度
是否需要回灌核心流程
```

### 15.4 状态到工具调用关系图

```mermaid
flowchart LR
  S1[S1_ContractLoad] --> C1[read contracts/prompts/standard design]
  S2[S2_ToolProbe] --> T1[tool_probe.py]
  S3[S3_SourceExtract] --> T2[extract_pdf_structure.py]
  S3 --> T3[render_pdf.py source]
  S4[S4_PageStrategy] --> D1[D1_page_strategy.prompt.json]
  S5[S5_TranslationPlan] --> D2[D2_translation.prompt.json]
  S5 --> T4[validate_semantic_translations.py]
  S6[S6_LayoutPlan] --> T5[build_layout_policy.py]
  S6 --> D4[D4_layout_plan.prompt.json]
  S7[S7_GenerateCandidate] --> T6[generate_semantic_backfill.py]
  S8[S8_VerifyProductQuality] --> T7[render_pdf.py candidate]
  S8 --> T8[render_source_output_crop.py]
  S8 --> T9[evaluate_pdf_quality.py]
  S8 --> D7[D5_D7_quality_gate.prompt.json]
  LX[Lx_RepairLoop] --> D8[D8_repair_selection.prompt.json]
  LX --> M1[page_type_repair_matrix.md]
  AX[Ax_AdaptiveChange] --> M2[change manifests]
  S9[S9_VerifyProcessContract] --> T10[scan_core_overfit.py]
  S9 --> T11[validate_process_artifacts.py]
  S9 --> D9[D9_final_acceptance.prompt.json]
```

这个图定义“状态触发工具”，不是“工具自己驱动状态”。如果执行中工具直接跳状态，必须在 `operation_log.jsonl` 中标记为 process-contract 风险。

### 15.5 状态不变量

| 状态 | 不变量 |
|---|---|
| `S0_Request` | run mode、输入 PDF、输出目录、非目标必须明确 |
| `S1_ContractLoad` | 核心流程文档、contracts、prompts、tools README 必须可读 |
| `S2_ToolProbe` | 不能在工具能力未知时进入 PDF 提取 |
| `S3_SourceExtract` | 每个目标页必须有 page geometry；可提取文字必须有 bbox/font/text |
| `S4_PageStrategy` | 页面类型和区域角色必须来自当前 source evidence |
| `S5_TranslationPlan` | product_quality 禁止 placeholder、元描述式伪译文或缺覆盖译文 |
| `S6_LayoutPlan` | generator 使用的布局参数必须来自 `layout_policy.json`，不能隐藏在代码常量里 |
| `S7_GenerateCandidate` | product_quality 候选必须由 `generate_semantic_backfill.py` 生成 |
| `S8_VerifyProductQuality` | 产品 gate 失败不能进入产品成功终态 |
| `Lx_RepairLoop` | 每次 loop 只修一个主要 failure_class |
| `Ax_AdaptiveChange` | 方法论变更必须记录 before/after 和原因 |
| `S9_VerifyProcessContract` | 必须同时输出 process verdict、product verdict、terminal state |

### 15.6 迁移规则

| From | 条件 | To |
|---|---|---|
| `S0_Request` | 输入齐全 | `S1_ContractLoad` |
| `S1_ContractLoad` | 契约齐全 | `S2_ToolProbe` |
| `S1_ContractLoad` | 契约缺失 | `S_FAIL_PROCESS_CONTRACT` |
| `S2_ToolProbe` | 必需工具可用 | `S3_SourceExtract` |
| `S2_ToolProbe` | 必需工具缺失且无替代 | `S_FAIL_TOOLING` |
| `S3_SourceExtract` | 源结构和渲染完成 | `S4_PageStrategy` |
| `S4_PageStrategy` | D1 裁决完整 | `S5_TranslationPlan` |
| `S5_TranslationPlan` | 语义译文校验通过 | `S6_LayoutPlan` |
| `S5_TranslationPlan` | 译文缺失、placeholder、元描述式伪译文、覆盖不足 | `S_FAIL_CAPABILITY` |
| `S6_LayoutPlan` | policy 可追溯、无过拟合 | `S7_GenerateCandidate` |
| `S7_GenerateCandidate` | 候选 PDF 和生成证据存在 | `S8_VerifyProductQuality` |
| `S8_VerifyProductQuality` | 全部阻塞 gate 通过 | `S9_VerifyProcessContract` |
| `S8_VerifyProductQuality` | 存在可修复阻塞失败 | `Lx_RepairLoop` |
| `S8_VerifyProductQuality` | 阻塞失败不可修复 | `S_FAIL_QUALITY` |
| `Lx_RepairLoop` | repair atom 需要重新规划布局 | `S6_LayoutPlan` |
| `Lx_RepairLoop` | repair atom 只需重新生成 | `S7_GenerateCandidate` |
| `Lx_RepairLoop` | 无有效 repair atom | `S_FAIL_QUALITY` |
| `Ax_AdaptiveChange` | 小幅变更验证通过 | 返回触发状态 |
| `S9_VerifyProcessContract` | 过程和产品均通过 | `S_DONE_PRODUCT_ACCEPTED` |
| `S9_VerifyProcessContract` | 仅过程验证目标通过 | `S_DONE_PROCESS_VALIDATED` |

### 15.7 状态 trace 最小记录

每次迁移必须追加：

```json
{
  "transition_id": "T01",
  "from": "S6_LayoutPlan",
  "to": "S7_GenerateCandidate",
  "entry_condition": "layout_policy.json exists and D4 decision recorded",
  "tools": ["tools/planners/build_layout_policy.py"],
  "input_artifacts": ["docs/reports/<run_id>/source_extraction.json"],
  "output_artifacts": ["docs/reports/<run_id>/layout_policy.json"],
  "decision_record_ids": ["D4_layout_plan"],
  "gates": [{"gate_id": "layout_policy_traceable", "status": "pass"}],
  "next_state_rule": "candidate generation may start"
}
```

### 15.8 终态语义

| 终态 | 含义 |
|---|---|
| `S_DONE_PRODUCT_ACCEPTED` | 产品 PDF 质量和过程契约都通过 |
| `S_DONE_PROCESS_VALIDATED` | 过程验证通过，但不声明产品质量成功 |
| `S_FAIL_PROCESS_CONTRACT` | 缺状态 trace、操作日志、契约产物或审计报告 |
| `S_FAIL_TOOLING` | 工具、字体、PDF 读写或渲染能力不足 |
| `S_FAIL_CAPABILITY` | 语义翻译、OCR 或必须能力缺失 |
| `S_FAIL_QUALITY` | 产品质量 gate 阻塞且无法继续修复 |

## 16. 活动流详细设计

### 16.1 主活动流

```text
Start
  -> A1 初始化 round
  -> A2 读取契约与标准流程
  -> A3 探测工具
  -> A4 对每个输入 PDF 执行源提取
  -> A5 对每个输入 PDF 执行页面策略裁决
  -> A6 校验或生成语义译文
  -> A7 生成布局策略
  -> A8 生成候选 PDF
  -> A9 渲染候选并执行质量 gate
  -> A10 如失败则进入修复循环
  -> A11 执行反过拟合扫描
  -> A12 验证过程契约
  -> A13 生成最终审计报告
End
```

### 16.2 每个输入 PDF 的活动流

```text
For each case/input item:
  create docs/reports/<run_id>/
  extract source structure
  render source preview
  bind D1 page strategy prompt slots
  record D1 decision
  validate semantic translation JSON
  stop at S_FAIL_CAPABILITY if validator finds placeholder or meta-description pseudo translation
  build baseline layout policy
  bind language layout profile based on source_language/target_language
  bind D4 layout prompt slots
  record final layout policy
  verify layout_policy records language_pair_profile/table_cell/legend/body_flow/source_separator/font profiles/target_language_reflow when those roles exist
  generate semantic backfill candidate
  render candidate preview
  evaluate product quality gates
  if visual evidence needed:
    render focused crop
    render backrotated crop for side nav when applicable
    write visual_adjudication.json
    rerun quality evaluator with visual adjudication
```

### 16.3 修复 loop 活动流

```text
While product_quality_gates has blocking failures:
  classify failure_class
  map failure_class to repair_atom
  write repair_loop_<n>.json
  decide whether repair starts at S6 or S7
  apply minimal policy/tool/doc change
  if method changed:
    enter Ax_AdaptiveChange
    write adaptive_change_record.json
  regenerate candidate or rerun quality gate
  stop when:
    blocking gate passes
    no repair atom remains
    failure repeats without new evidence
```

### 16.4 AdaptiveChange 活动流

```text
Trigger:
  existing tool/contract/prompt cannot express required evidence or repair

Actions:
  record why existing design is insufficient
  capture before manifest
  make smallest change
  capture after manifest
  rerun affected validation
  record whether core workflow should be revised

Exit:
  return to triggering state, or fail with S_FAIL_CAPABILITY
```

## 17. 数据与接口契约

### 17.1 核心 artifact

| Artifact | Producer | Consumer | 必填要点 |
|---|---|---|---|
| `source_extraction.json` | `extract_pdf_structure.py` | D1、D2、D4、generator、validator | page_index、bbox、text、font_size、page_rect |
| `semantic_translations.json` | D2 或输入包 | translation validator、generator | unit_id、source_text、source_language、target_language、target_text_field、translation_target_text 或目标字段、coverage |
| `semantic_translation_validation.json` | `validate_semantic_translations.py` | S5 gate、final report | verdict、missing units、invalid units |
| `layout_policy.json` | `build_layout_policy.py` + D4 | generator | language_pair_profile、language_profile_json、classification_rules、draw_modes、source_separator_policy、flow_grouping、target_composition、target_language_reflow、font_profiles、fallback |
| `layout_plan.json` | generator | audit、quality review | inserted regions、region kinds、layout mode |
| `candidate_generation_evidence.json` | generator | quality evaluator、audit | `tool`、`strategy`、`real_backfill_pdf`、`translation_quality`、`semantic_coverage`、`layout_policy_json`、`layout_policy_sha256`、`inserted_unit_count`、`inserted_region_count`、`semantic_translated_unit_count`、`preserved_target_language_unit_count`、`fit_warning_count`、`source_block_ids`、`source_line_indexes`、`target_composition_applied`、redaction `fill_color` and background-sampling provenance |
| `product_quality_gates.json` | quality evaluator | D7、repair loop、final report | gates、blocking failures、page metrics |
| `visual_adjudication.json` | D7/human/model visual review | quality evaluator、final report | dimensions、status、evidence refs |
| `anti_overfit_scan.json` | anti-overfit scanner | D9、final report | verdict、blocking_hit_count |
| `state_trace.json` | execution engine | process validator、final report | all transitions |
| `operation_log.jsonl` | execution engine | process validator、final report | all tool calls |
| `decision_log.jsonl` | execution engine | process validator、final report | all D1-D9 decision records |

### 17.2 模型裁决输出契约

| Decision | 状态 | 输出必须包含 |
|---|---|---|
| `D1_role_classification` | `S4` | page_type、region_roles、evidence_refs、risk_flags |
| `D2_translation` | `S5` | translations、coverage、term_decisions、provider、target_text_field、forbidden_pattern_check、layout_variants |
| `D4_layout_plan` | `S6` | layout_policy or policy_overrides、language_pair_profile、constrained_slot/event_card/fluid_body 分流、target_composition、target_language_reflow、body_flow_gap_policy、short_continuation_policy、dense_page_body_band_policy、event_card_policy、table_cell_policy、font_profiles、probe_isolation_requirement、evidence、fit_risks |
| `D5_D7_quality_gate` | `S8` | dimension statuses、blocking status、repair hints、next_state |
| `D8_repair_selection` | `Lx` | failure_class、repair_atom、target_state、verification_to_run |
| `D9_final_acceptance` | `S9` | process verdict、product verdict、anti-overfit verdict、terminal_state |

### 17.3 状态 trace 必填 schema

`state_trace.json` 每条迁移必须包含以下字段，缺一项就是 `S_FAIL_PROCESS_CONTRACT`：

```json
{
  "transition_id": "T001",
  "from": "S0_Request",
  "to": "S1_ContractLoad",
  "entry_condition": "why this transition can start",
  "run_mode": "product_quality",
  "tools": ["tool names used in this transition"],
  "input_artifacts": ["paths read"],
  "output_artifacts": ["paths written"],
  "decision_record_ids": ["D1_role_classification"],
  "gates": [{"gate_id": "name", "status": "pass|fail|warn|skipped"}],
  "next_state_rule": "explicit rule that selected the next state",
  "timestamp_local": "local ISO timestamp"
}
```

### 17.4 决策日志必填 schema

`decision_log.jsonl` 必须至少包含这些 `decision_id`，ID 不得改名：

```text
D1_role_classification
D2_translation
D3_visual_only_text
D4_layout_plan
D5_initial_verification
D6_user_feedback_adjudication
D7_similarity_gate
D8_minimal_repair_selection
D9_final_acceptance
```

每条决策必须包含：

```json
{
  "decision_id": "D7_similarity_gate",
  "state": "S8_VerifyProductQuality",
  "purpose": "what this judgement decides",
  "input_artifacts": ["tool evidence paths"],
  "prompt_contract": "prompt or contract path used",
  "required_output_dimensions": ["dimensions judged"],
  "model_output": {
    "verdict": "pass|fail|warn|skipped",
    "backend_model_call_made": false,
    "reason": "honest result"
  },
  "next_state": "S_FAIL_QUALITY"
}
```

如果没有调用外部后端大模型，必须写 `backend_model_call_made=false`，并说明裁决由 Codex 执行器基于哪些工具证据完成。不能伪造模型交互。

## 18. 关键裁决逻辑

### 18.1 工具事实不交给大模型

以下事实只能由工具或文件系统判定：

```text
文件是否存在
JSON 是否可解析
PDF 是否可打开
页数是否一致
是否有候选 PDF
是否有源语言残留
是否有 fit_warning
anti_overfit blocking_hit_count
```

### 18.2 大模型裁决范围

大模型只裁决：

```text
页面类型和区域角色
译文语义是否符合源文本
译文是否是元描述式伪译文
布局策略是否符合当前源 PDF 证据
body_flow 中哪些行是同段续行、哪些是新段落
表格/图例/侧栏的 preserve-line 与 variants 是否合理
视觉相似度、段落节奏、字号比例、空白密度
repair atom 选择
最终 split verdict 的解释
```

### 18.3 侧栏字形方向裁决

侧栏不是“有旋转就通过”。

通过条件：

```text
源文侧栏是横排标签整体旋转
候选中文先横排成一行，再整体旋转贴回
候选侧栏裁剪图反向旋转后能横向阅读
同一侧栏组 writing mode 一致
```

必须证据：

```text
source-vs-output sidebar crop
backrotated output crop
visual_adjudication.json 中的 sidebar_glyph_orientation
product_quality_gates.json 中对应 gate
```

## 19. 执行核对表

新 Codex 在开始执行前必须确认：

```text
[ ] 已读取标准流程设计
[ ] 已读取 state_machine/tool_contracts/product_quality/decision contracts
[ ] 已读取 prompt_tool_bindings.json
[ ] 已确认不会新造提示词
[ ] 已确认 docs/output 无旧 PDF 作为本轮结果
[ ] 已确认 docs/reports 将由本轮重新生成
```

生成候选前必须确认：

```text
[ ] source_extraction.json 存在
[ ] semantic translations 校验通过
[ ] layout_policy.json 存在
[ ] generator 使用 generate_semantic_backfill.py
```

最终报告前必须确认：

```text
[ ] candidate PDF 路径存在
[ ] product_quality_gates.json 存在
[ ] visual_adjudication.json 如需要则存在
[ ] anti_overfit_scan.json 存在且 blocking_hit_count=0
[ ] process_validation.json 存在
[ ] state_trace.json、operation_log.jsonl 和 decision_log.jsonl 存在
[ ] final report 写明 process/product 双 verdict
```
