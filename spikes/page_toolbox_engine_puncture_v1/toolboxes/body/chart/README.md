# body.chart 工具箱

当前成熟度：`EXPERIMENTAL`。P13 Round 18 全量 Gate：`FAIL`。

本目录只处理 `body.chart`，不修改分类器、公共 PDF kernel、其他叶子或整链集成。运行时只翻译可定位的 PDF 原生图表标题、图例、轴/类别标签和注释；图片文字、数值、刻度、数据标签、单位记号、图形和页面装饰保持不变。

Round 18 已按 `1 页 -> 3 页 -> 2 页 -> 全部 30 页` 执行真实千问验证。全量结果为 29 页通过、1 页产品质量失败；30/30 均产出单页 PDF，30/30 翻译校验通过，中文源候选无中文残留。唯一失败是环图外侧长标签向关联图形方向扩宽后发生矢量碰撞，因此仍不生成 `promotion_manifest.json`。完整结论见 `stage_gate.json` 与 `reports/p13_round18_full_qwen_generalized_fixes_20260716.json`。

产物契约：每个已建立运行包的样本都必须生成 `output/candidate.pdf`。通过页保存正常候选；已有译文但翻译校验或布局失败时，诊断候选仍删除可翻译源文、回填全部返回译文且不扩展页面；只有翻译服务尚未返回任何译文或模板尚未建立等更早失败，才保存与源页字节一致的兜底候选。诊断候选仅用于查看问题，PDF 存在不代表 Gate 通过。

常用命令（在穿刺项目根目录执行）：

```powershell
$env:PYTHONPATH="$PWD\src;$PWD"
python -m unittest discover -s toolboxes/body/chart/tests -v
python -m toolboxes.body.chart.tools.prepare_samples
python -m toolboxes.body.chart.tools.run --initial --provider qwen --run-id <run-id>
python -m toolboxes.body.chart.tools.run --initial-expansion --provider qwen --run-id <run-id>
python -m toolboxes.body.chart.tools.run --all --allow-holdout --final-validation --provider qwen --run-id <run-id>
```

千问凭据只从 `PAGE_TOOLBOX_QWEN_API_KEY`、`PAGE_TOOLBOX_QWEN_BASE_URL` 和 `PAGE_TOOLBOX_QWEN_MODEL` 读取，不写入本目录。
