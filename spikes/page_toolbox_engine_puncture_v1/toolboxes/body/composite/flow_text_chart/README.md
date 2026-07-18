# P17 `body.composite.flow_text_chart`

该工具箱只处理同页同时包含实质正文和主体图表的分类叶。它从 PDF 直接事实建立穷尽且互斥的 `flow / chart / shared / protected` 所有权，在每个 FlowBand 内调用单流正文规划能力，在图表固定区内调用 P13 图表规划能力，最后把一个页级翻译请求的结果合并为一次源 PDF 渲染。

当前状态为 `EXPERIMENTAL / EVIDENCE_INSUFFICIENT`。人工复核发现旧运行 `34-full-qwen-post-repair` 的 `FTC_ZH_07_00405_p0168` 并非真实排版能力边界，而是同一长译文被两个相邻源片段重复承载；同类问题也存在于 `FTC_ZH_06_00405_p0059`，因此旧 31/32 结论已由 v5 证据取代。修复后的真实千问证据由无代码变更间隔的三次串行运行拼合为 32/32 页 `PAGE_PASSED`，当前代码对这 32 份翻译包执行的 `45-full-recorded-frozen-v5` 单批回放也是 32/32，并且 32/32 候选 PNG 与对应真实千问候选逐像素一致。32 页分类样本在分区前已经全部预览，因此 holdout 只能记作非盲工程证据；P4、P5、P13 也没有同时满足形式晋级条件，本目录不得生成 `promotion_manifest.json`。

运行顺序：

```powershell
python -m toolboxes.body.composite.flow_text_chart.tools.prepare_samples
python -m toolboxes.body.composite.flow_text_chart.tools.run --initial --provider qwen --run-id 01-initial-qwen
python -m toolboxes.body.composite.flow_text_chart.tools.run --initial-expansion --provider qwen --run-id 02-expansion-qwen
python -m toolboxes.body.composite.flow_text_chart.tools.run --all --allow-holdout --final-validation --provider qwen --run-id 03-full-qwen
```

千问配置只从进程环境读取：`PAGE_TOOLBOX_QWEN_BASE_URL`、`PAGE_TOOLBOX_QWEN_API_KEY`、`PAGE_TOOLBOX_QWEN_MODEL`。密钥不得写入 Prompt、manifest、运行包或报告。

失败页若已经取得完整译文，会尽量生成 `TRANSLATED_DIAGNOSTIC_CANDIDATE`；该文件只用于审阅，`product_acceptance=false`。模型或模板在取得译文前失败时不发布源页复制候选。
