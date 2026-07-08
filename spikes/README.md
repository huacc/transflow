# Spikes

本目录用于独立验证 PDF 语义翻译回填工作流的工程完备性。

spike 不是实验源码目录，也不是稳定主框架。它的价值是让一个新的 Codex 会话在隔离包内运行，验证 `pdf_translation_workflow_core` 或某个 lab promotion 是否能够仅凭包内文档、工具、契约和输入产出可审计结果。

## 职责

- 验证 core 或 lab promotion 的文档、工具、状态机和契约是否完整。
- 记录独立会话执行过程、工具调用、模型交互、失败边界和最终 verdict。
- 暴露当前设计缺口，例如缺少输入、缺少工具、状态迁移不完整、prompt 契约不清晰、repair loop 未闭合。

## 不承担的职责

- 不作为稳定运行入口。
- 不作为 core 能力源码。
- 不保存新的实验主线。大改动应先进入 `pdf_translation_workflow_lab/`。
- 不把历史 round、父目录输出或人工对照 PDF 当作当前运行证据。

## 包边界

每个 `spikes/<spike_id>/` 都是硬边界：

- 运行输入必须来自包内 `input/` 或 prompt 明确允许的包内路径。
- 输出必须写入包内 `docs/output/`、`reports/` 或 prompt 指定的包内目录。
- 审计只认可当前 spike 包内生成的报告、日志、候选 PDF 和 verdict。
- 如果需要读取父级 core 或 lab 内容，必须在构建 spike 时复制进包内，并在 manifest 或 prompt 中写清来源。

## 推荐 spike 结构

```text
spikes/<spike_id>/
  README.md
  SPIKE_PROMPT.md
  input/
  pdf_translation_workflow_core/
  docs/
    业务流程/
    测试提示词/
    output/
    reports/
  reports/
  final_verdict.json
```

具体结构可以按验证目标裁剪，但必须保留可审计证据链。

## 验收口径

- `process_contract_verdict`：文档、状态机、工具调度、证据文件和终态是否符合契约。
- `product_quality_verdict`：候选 PDF 的视觉、布局、文本覆盖、字体层级、图表和表格是否达标。
- `terminal_state`：必须真实反映执行结果，例如 `S_PASS`、`S_FAIL_PROCESS_CONTRACT`、`S_FAIL_QUALITY`、`S_FAIL_CAPABILITY`。

流程通过不等于产品质量通过；产品质量通过也不能掩盖流程契约失败。

## 当前内容

- `spike18/`
- `spike19/`
- `spike20/`

这些目录是历史独立验证包，保留用于追溯，不应直接作为新实验源码修改。
