# `body.composite.flow_text_diagram`

当前成熟度：`EXPERIMENTAL`。P18 工程穿刺已完成，Gate 结论为 `EVIDENCE_INSUFFICIENT`，没有生成 promotion manifest。

本工具箱只处理“实质正文与主体示意图同时存在”的单页。它先建立正文、示意图、共享文字和受保护文字的唯一所有权，再把全部可翻译原生文字合并为一个页级翻译请求。正文侧按页面事实选择 `single` 或 `multi` 私有能力；示意图侧复用 P14 的节点、连线和 owner 内适配能力；最终从不可变源页一次渲染。

当前明确阻塞：

- 当前代码对 30 页已知池的诚实口径为 14 `PAGE_PASSED`、7 `QUALITY_FAILED`、9 `CAPABILITY_FAILED`；
- P17 工具箱与 Gate 尚不存在；
- P5 `body.flow_text.multi` 为 `NOT_EVALUATED`，无 promotion manifest；
- P14 仅为 `PASS_NON_BLIND`，无 promotion manifest；
- 30 页分类池已在 workflow freeze 前预览，不能作为严格盲测。

运行：

```powershell
$env:PYTHONPATH='src;.'
python -m toolboxes.body.composite.flow_text_diagram.tools.run --batch initial --provider fixed --run-id 01-p18-initial-fixed
python -m toolboxes.body.composite.flow_text_diagram.tools.run --batch initial --provider qwen --run-id 02-p18-initial-qwen
python -m toolboxes.body.composite.flow_text_diagram.tools.run --batch three --provider qwen --run-id 03-p18-three-qwen
python -m toolboxes.body.composite.flow_text_diagram.tools.run --batch all --provider qwen --run-id 04-p18-all-qwen
```

真实模型执行遵循了 1 页、3 页、30 页的递进节奏。原始全量 Qwen 运行只有 5/30 产品通过；复用其真实翻译包在最终代码上回放，并对视觉复核发现的 3 个假 PASS 加入新增 flow/shared 碰撞硬门禁后，最终已知池口径为 14/30。完整证据见 `reports/P18_执行总结_20260718.md` 和 `reports/P18_视觉复核_20260718.md`。

生成候选 PDF、流程 PASS 或已翻译诊断候选都不等于产品通过或正式晋级。
