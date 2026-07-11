# 页面分类引擎工程化穿刺 v1

本穿刺只验证：

1. `page.role`：`cover | contents | body | end | visual_only`；
2. `body.layout_owner`：`flow_text | table | chart | diagram | anchored_blocks | composite`；
3. `body.flow.topology`：`single | multi | visual_anchored`；
4. `body.composite.kind`：`flow_text_table | anchored_blocks_chart | chart_table | flow_text_chart | flow_text_diagram`；
5. 工程规则与自建千问多模态的独立判断、一次细粒度复核和确定性归约；
6. 当前运行指定的样本目录与 `分类结果/` 中 PDF 集合完全一致。

`body.layout_owner` 支持 PDF 原生直接表格证据。显式表格由行列/单元格工具识别；无边框表格由至少四个重复行、稳定列锚点和同行配对识别。直接证据置信度达到 `0.90` 且所有权边界清晰时直接采用规则，不调用千问初判；中间边界仍走千问和一次复核。

图片只用于分类。图片像素及图片内部文字不翻译、不重排、不覆盖。

`body/freeform` 是确定性兜底目录：页面已确认属于 `body`，但任一后续节点初判和一次复核仍无法稳定归类时进入。它不是千问可直接选择的普通类别，也不等同于 `anchored_blocks` 或 `composite`。route 会记录具体 `failed_node`。

## 运行

```powershell
$env:PAGE_CLASSIFIER_QWEN_API_KEY='...'
python scripts/build_samples.py --legacy-only
python -m pytest -q
python scripts/run_puncture.py --sample-dir 样本1
python scripts/materialize_results.py --run-id <run-id> --sample-dir 样本1
python scripts/build_report.py --run-id <run-id>
```

`run_puncture.py` 启动时先删除 `分类结果/` 中上一轮 PDF，保留所有 `分类说明.md`；完成分类后再使用 `materialize_results.py` 写入本轮 PDF。默认样本目录为 `样本1`，新抽样集使用 `--sample-dir 样本2 --source-manifest manifests/sample2_source_manifest.jsonl`。

`--legacy-only` 表示当前验证集严格来自同级 `page_classification_dual_qwen_puncture/样本`；不追加年报抽样页。省略该参数才会构建“旧穿刺样本 + 年报抽样页”的扩展验证集。

API Key 只从进程环境变量读取，禁止写入文件。千问输入不包含源文件名、路径、页码标签或 gold 标签。

## GitHub 数据边界

GitHub 只保存源码、Prompt、分类定义、必要 manifest 和精简验证报告，不保存 PDF、页面 PNG、`.venv`、运行 artifacts 或其他过程数据。依赖真实 PDF 的集成测试在本地样本不存在时自动跳过；准备好 `样本1/样本2` 后会照常执行。
