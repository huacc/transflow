# body.flow_text.single 工具箱

成熟度：`EXPERIMENTAL`，当前阶段：`P3`。

本目录只处理“单一主文字流从上到下推进”的正文页，不处理多列、表格、图表或组合页。

P3 目标是打通首条真实纵向闭环，不在本阶段生成 `promotion_manifest.json`。

## 组成

- `docs/`：分类边界、调度、工具流程和裁决规则；
- `prompts/`：中文千问页级翻译提示词；
- `samples/`：三分区清单，P3 只读 development；
- `tools/`：单列正文专用的 TemplateBuilder、Planner、Renderer、Judge 和页级引擎；
- `runs/`：真实页级运行证据；
- `reports/`：P3 阶段验收结果。
