# P16 `body.composite.chart_table`

本工具箱只处理同页同时存在实际图表和结构化表格的复合页。它复用 P13 的公开图表模板、排版与渲染合同，以及 P6 的公开表格模板合同；P16 自己只负责一次性所有权分区、统一页级翻译请求、跨区域约束和单次合成渲染。

每个原生文字对象必须恰好属于 `chart`、`table`、`shared` 或 `protected`。任一基础 owner 缺少直接证据时整页返回 `CAPABILITY_FAILED`，禁止把表格降格为正文、把图表当附件，或用 OCR 伪造可编辑文字。

## 当前结论

P16 的真实 Gate 为 `EVIDENCE_INSUFFICIENT`，工具箱保持 `EXPERIMENTAL`：

- 新一轮真实千问按“1 页 → 3 页 → 30 页”完成，初始 1/1、扩展 3/3 `PAGE_PASSED`；
- 全量真实千问运行 `36-full-qwen-v3-translated-candidate` 覆盖 30 页、995 个译文单元；
- 修正“百万”数量级和独立专业缩写的校验误报后，`p16-rules-v4` 下全量 recorded replay 为 20 `PAGE_PASSED`、3 `QUALITY_FAILED`、6 `CAPABILITY_FAILED`、1 `PROCESS_FAILED`；
- 30/30 翻译校验通过，30/30 生成可打开、单页、尺寸不变且不与源页字节相同的 `candidate.pdf`；20 页为产品候选，10 页为已翻译诊断候选，源页复制候选为 0；
- 30/30 重放候选 PNG 与真实千问候选逐页像素一致；5 张接触表覆盖全部 30 页，3 个用户点名页面另做全分辨率复核；
- P6 为 `NOT_EVALUATED`，P13 为 `PASS_NON_BLIND`，均无 promotion manifest。

因此目录中没有 `promotion_manifest.json`。

## 运行

在 `page_toolbox_engine_puncture_v1` 根目录执行：

```powershell
$env:PYTHONPATH='src;.'
python -m toolboxes.body.composite.chart_table.tools.run --initial --provider fixed --fixed-translations toolboxes/body/composite/chart_table/fixtures/fixed_initial_translations.json --run-id <new-initial-run-id>
python -m toolboxes.body.composite.chart_table.tools.run --initial-expansion --provider fixed --fixed-translations toolboxes/body/composite/chart_table/fixtures/fixed_expansion_translations.json --run-id <new-expansion-run-id>
```

规则冻结且前两轮通过后，才允许用真实千问执行全量最终验证：

```powershell
$env:PAGE_TOOLBOX_QWEN_API_KEY='<secret>'
$env:PYTHONPATH='src;.'
python -m toolboxes.body.composite.chart_table.tools.run --all --allow-holdout --final-validation --provider qwen --run-id <new-full-qwen-run-id>
```

密钥只从环境变量读取，不得写入代码或 Artifact。固定 provider 只验证既定译文下的产品与机械链，不能替代真实千问全量验收。

## 证据入口

- `contracts/rule_freeze_v4.json`：当前冻结规则及 18 个跟踪文件哈希；
- `reports/full_translated_candidate_validation_v4.json`：真实千问全量结果、v4 重放结果、语言/PDF/像素完整性；
- `reports/full_translated_candidate_pdf_index_v4.md`：30 个产品或诊断候选 PDF 的总索引；
- `runs/39-full-recorded-v4-validation/reports/contact_sheets/`：5 张全量视觉接触表；
- `reports/recorded_replay_validation.json`：4 页像素重放证据；
- `reports/anti_overfit_audit.json`：静态与结构多样性审计；
- `reports/independent_acceptance.md`：独立验收边界；
- `stage_gate.json`：最终真实 Gate。
