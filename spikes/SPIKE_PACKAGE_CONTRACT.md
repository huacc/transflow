# Spike Package Contract

本文件定义新建 spike 包时必须满足的最小契约。

## 1. 输入契约

spike 包必须明确声明：

- 验证目标：验证 core、验证 lab promotion，还是验证某个 regression case。
- 输入 PDF 和翻译 JSON 的包内路径。
- 是否允许调用外部模型。
- 是否允许读取人工对照 PDF。默认不允许运行时读取。
- 是否允许修改包内工具。严格验证默认不允许。

## 2. 工具契约

如果 spike 要验证 core 或 lab 能力，必须把需要的工具复制到包内，或在 prompt 中明确只读引用路径。

工具调用记录必须包含：

- 状态名。
- 工具路径。
- 输入文件。
- 输出文件。
- 返回码。
- 失败信息。

## 3. 状态契约

每个 spike 至少应记录这些阶段：

| 状态 | 目的 | 主要证据 |
|---|---|---|
| `S0_Request` | 确认目标、输入、边界和运行模式 | `run_request.json` |
| `S1_ContractLoad` | 读取流程、契约、提示词和工具说明 | contract load 记录 |
| `S2_ToolProbe` | 检查 Python、PDF、字体、渲染和工具可用性 | `tool_probe.json` |
| `S3_SourceExtract` | 提取页面、文字、bbox、字体、颜色、图片和绘图对象 | `source_extraction.json` |
| `S4_TranslationPlan` | 生成或校验语义翻译 | translation artifacts |
| `S5_LayoutPlan` | 生成布局策略和回填计划 | `layout_plan.json` |
| `S6_GenerateCandidate` | 生成候选 PDF 和预览图 | candidate PDF, previews |
| `S7_VerifyProductQuality` | 执行视觉和文本质量门禁 | `product_quality_gates.json` |
| `Lx_RepairLoop` | 对阻塞失败执行修复或记录不能修复原因 | `repair_loop_<n>.json` |
| `S8_VerifyProcessContract` | 验证过程契约和最终终态 | process audit, final verdict |

## 4. 模型交互契约

如果调用大模型，必须记录：

- 调用目的。
- 系统提示词。
- 用户提示词。
- 输入数据摘要或文件路径。
- 要求输出的 JSON schema 或字段。
- 模型返回结果。
- 使用返回结果做出的裁决。

如果未调用大模型，也要记录 `model_backend: not_invoked` 和原因。

## 5. 质量门禁契约

产品质量门禁至少分开记录：

- 文本溢出。
- 文本重叠。
- 字体层级异常。
- 图片、图表、表格遮挡。
- 擦除残影。
- 页面空白比例异常。
- 目标语言字符集异常。

每个失败项必须映射到 repair family，或明确标记为当前工具无法修复。

## 6. 反过拟合契约

spike 运行时不得引入：

- 按具体页码分支的逻辑。
- 按精确样本文字分支的逻辑。
- 按精确样本数值分支的逻辑。
- 把人工对照 PDF 当作运行时输入的逻辑。

如果为了继续运行做了小幅修改，必须在报告中写明修改点、原因、影响范围和是否应反馈到 core 或 lab。

## 7. 终态契约

最终报告必须同时给出：

- `process_contract_verdict`
- `product_quality_verdict`
- `terminal_state`
- 候选 PDF 路径，或未生成原因
- 审计报告路径
- 所有框架改动或包内临时改动

不能只给候选 PDF，也不能只给流程通过结论。
