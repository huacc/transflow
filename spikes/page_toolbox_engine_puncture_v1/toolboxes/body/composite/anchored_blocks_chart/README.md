# P15 `body.composite.anchored_blocks_chart`

本目录只处理“独立锚定块 + 实际图表”复合页。P15 不复制 P11/P13 的页面知识：

- P11 `body.anchored_blocks` 决定块 owner、块边界和块内布局；
- P13 `body.chart` 决定图表区域、语义标签、锚点和锁定对象；
- P15 只负责一次性所有权分区、统一页级翻译请求、按不可变 ID 回切、跨 owner 约束和单次合成渲染。

每个原生文本对象必须恰好属于 `anchored`、`chart`、`shared` 或 `protected`。任何子工具能力失败都使整页进入 `CAPABILITY_FAILED`，但不能阻断其他明确 owner 的翻译。最终交付中的每一页都必须留下包含真实译文的 `output/candidate.pdf`；源页复制件不算候选，模型调用未完成的运行只能标记为未交付并重试。

## 运行

在 `page_toolbox_engine_puncture_v1` 根目录执行：

```powershell
$env:PYTHONPATH='src;.'
python -m toolboxes.body.composite.anchored_blocks_chart.tools.run --batch initial --provider fixed --run-id 01-p15-one-fixed
python -m toolboxes.body.composite.anchored_blocks_chart.tools.run --batch three --provider fixed --run-id 02-p15-three-fixed
python -m toolboxes.body.composite.anchored_blocks_chart.tools.run --batch all --provider fixed --run-id 03-p15-all-fixed
```

固定 provider 只验证所有权、布局、锁图、渲染和裁决链，产品结论固定为 `NOT_EVALUATED`。真实产品验证必须配置 `PAGE_TOOLBOX_QWEN_API_KEY` 后改用 `--provider qwen`。

当前 30 份样本在分组冻结前已被预览，故证据只能标记为 `NON_BLIND`。同时 P11/P13 尚无满足正式复合晋级的 promotion manifest，P15 不生成 `promotion_manifest.json`。

2026-07-17 的最终真实千问聚合证据来自 `runs/24-p15-seven-semantic-recheck-qwen-translated`、`runs/20-p15-all-qwen-translated-final-v2`、`runs/21-p15-remaining-a-qwen-translated` 和 `runs/22-p15-remaining-b-qwen-translated`：30/30 生成真实译后 PDF，30/30 通过语义校验，1095/1095 个译文单元可从 PDF 提取，228 个无法正常适配的译文也已诊断性物化，没有源页复制件或被省略译文。产品终态为 8 `PAGE_PASSED`、14 `CAPABILITY_FAILED`、6 `QUALITY_FAILED`、2 `PROCESS_FAILED`。译后交付合同为 `PASS`，P15 产品 Gate 仍为 `FAIL`；全部候选和对照图见 `reports/qwen_translated_candidate_index_20260717.md`。
