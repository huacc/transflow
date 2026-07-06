# SPIKE13 Package Manifest

## 目的

本轮验证当前 PDF 中文/英文回填工作流的原子能力、状态机调度、源文-译文对比门禁和标准流程设计是否能在随机、非对照、多语言输入上独立执行。

本轮不是翻译质量 benchmark。它重点验证：

- 状态机是否能驱动两个独立输入分支；
- `input` 是否保持纯 PDF；
- D2 是否能在运行时生成语义译文，而不是依赖预置 JSON；
- 版式策略是否来自当前 PDF 结构和当前语言方向；
- S8 是否执行源文-候选译文相对门禁，包括 `source_relative_visual_baseline`；
- 视觉裁决、修复 loop、失败边界和所有小幅运行性改动是否如实记录；
- 是否存在对照页、目标语言参考、历史输出或样本事实泄漏。

## 工作目录

```text
spikes\spike13
```

所有命令必须从本目录执行。

## 输入目录

`input` 目录只包含两个 PDF：

```text
input\AIA_2020_Annual_Report_en_pages_081_152_214_220_231.pdf
input\AIA_2020_Annual_Report_zh_pages_034_036_144_228_261.pdf
```

禁止在 `input` 目录放 JSON、截图、译文、报告、缓存或任何对照资料。

## 页码选择

英文源 PDF 随机页：

```text
81, 152, 214, 220, 231
```

中文源 PDF 随机页：

```text
34, 36, 144, 228, 261
```

两组页码没有对照关系。执行过程中不得把其中一个输入 PDF 当另一个输入 PDF 的翻译参考。

## 输入分支

| Regression ID | Source PDF | Direction | Target field |
|---|---|---|---|
| `S13_EN_random_5pages` | `input\AIA_2020_Annual_Report_en_pages_081_152_214_220_231.pdf` | `en -> zh` | `translation_zh` |
| `S13_ZH_random_5pages` | `input\AIA_2020_Annual_Report_zh_pages_034_036_144_228_261.pdf` | `zh -> en` | `translation_en` |

## 禁止引用

执行器不得使用：

- 另一份 `input` PDF 作为翻译参考；
- 官方 AIA 对应语言年报；
- 之前 round/spike 的译文 JSON、输出 PDF、截图、报告；
- 父目录 `docs\output`、`docs\reports` 或任意历史 `spikes\round*` / `spikes\spike*` 输出作为翻译或质量裁决证据；
- 任何外部对照页、双语材料或已知目标译文。

## 框架改动规则

`pdf_translation_workflow_core`、`docs\业务流程\PDF_中文回填_标准流程设计.md`、`docs\测试提示词\SPIKE13_ATOMIC_WORKFLOW_VALIDATION_PROMPT.md`、契约、提示词模板和 profile 均视为框架。

新 Codex 不得修改框架。只有在执行被阻塞且不修改框架无法继续时，才允许做极小的运行性补丁；补丁必须写入 `Ax_AdaptiveChange`，并记录：

```text
docs\reports\adaptive_change_record.json
docs\reports\change_manifest_before.json
docs\reports\change_manifest_after.json
docs\reports\change_manifest_delta.json
docs\reports\spike13_execution_audit.md
```

如果发生框架改动，`process_contract_verdict` 不能无条件通过，最终报告必须说明该改动是否暴露核心设计缺口。

## 必要方法论

```text
docs\业务流程\PDF_中文回填_标准流程设计.md
pdf_translation_workflow_core\
```

## 必要执行提示词

```text
docs\测试提示词\SPIKE13_ATOMIC_WORKFLOW_VALIDATION_PROMPT.md
```

## 输出位置

```text
docs\output\
docs\reports\
```

候选 PDF、渲染图、译文 JSON、质量门、视觉裁决、流程审计都必须写到上面两个目录下，不能写入 `input`。

