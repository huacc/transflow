# P5 body.flow_text.multi 当前背景与第 13 轮续作交接

## 1. 当前目标

只继续完善：

```text
spikes/page_toolbox_engine_puncture_v1/toolboxes/body/flow_text/multi
```

目标是补齐：

```text
工程规则采证
  -> 千问判断字号/行距问题
  -> 分发表选择修复工具
  -> 一次只修一个问题
  -> 重渲染与硬门禁
  -> 千问复判
  -> 接受或回滚
```

然后使用第 12 轮的 7 页输入构建第 13 轮回归。

## 2. 已完成的关键进度

- 已形成 `body.flow_text.multi` 独立工具箱、提示词、合同、规则、渲染和裁决流程。
- 已支持单页翻译、multi 模板提取、布局规划、PDF 回填和真实译文 PDF 输出。
- 已接入自建千问进行页面翻译、布局范式辅助判断和字号/行距视觉裁决。
- 已有确定性修复：跨栏分块、尾部单栏回收、语义段落合并、标题安全扩宽、段间距修复、结构锚点检查等。
- 已修复短右锚标题错误换行、语义片段重复合并阈值漂移、微小段间距误压缩和负向位移导致重叠等问题。
- 已增加有限的 `spacious / balanced / relaxed` 排版候选，用于页面有安全空白时改善字号和行距。
- `S2P0395` 的“单栏 -> 多栏 -> 单栏”页内范式已经跑通。
- `S2P0624` 的字号和行距美化已经通过千问复判和产品裁决。
- 相关通用经验已写入：
  `spikes/page_toolbox_engine_puncture_v1/docs/经验/P4_body.flow_text.single_稳定修复与LLM裁决经验.md`

## 3. 第 12 轮当前结果

目录：

```text
toolboxes/body/flow_text/multi/runs/12-p5-multi-seven-sample-regression-20260713T042028Z
```

| case | 产品结果 | 当前主要情况 |
|---|---|---|
| S2P0395 | PASS | multi 页内混合范式可用 |
| S2P0624 | PASS | 字号/行距已美化并通过复判 |
| AR_EN_P0066 | FAIL | 字号偏小、行距偏紧；另有结构锚点问题 |
| AR_ZH_P0066 | FAIL | 字号偏小、行距偏紧；另有结构锚点问题 |
| S2P0351 | FAIL | margin wrap 未解决，尚未进入字体复判 |
| S2P0869 | FAIL | 字号偏小、行距偏紧；另有源文残留 |
| S2P0906 | FAIL | 字号/行距可接受，但结构锚点失败 |

第 13 轮仍使用这 7 页，不能先换样本。

## 4. 当前最关键的问题

### 4.1 千问裁决还没有真正驱动修复

入口：

```text
toolboxes/body/flow_text/multi/tools/engine.py
```

当前 `engine.py` 在千问返回 `too_small`、`too_tight` 或
`too_small_and_tight` 后，只生成一个 HARD finding，随后直接失败。

还缺少：

- 病因标准化；
- 分发表选择工具；
- 生成一个安全修复候选；
- 重渲染和机械硬门禁；
- 千问复判；
- 接受或回滚。

### 4.2 必须增加当前页的修复记忆

不能只依赖千问聊天上下文。引擎要在当前页、当前 run 内记录：

- 上次发现了什么问题；
- 改了哪一栏、哪个参数；
- 调用了哪个修复工具；
- 修改前后 profile；
- 硬门禁和千问复判结果。

核心规则：

- 同一个修复动作不能重复执行。
- 修改后仍是同一问题：判定 `NO_IMPROVEMENT`，回滚并换下一个安全候选。
- 原问题消失但出现新问题：保留修改，再处理新问题。
- 页面状态再次出现：判定循环并停止。
- 安全候选用尽：明确输出候选耗尽，不能假装通过。

建议落盘：

```text
reports/typography_repair_memory.json
```

记忆只用于当前页当前 run，不跨 PDF 继承。

### 4.3 其他 HARD 问题仍需诚实保留

结构锚点、源文残留、margin wrap 等失败，不属于字号/行距修复能解决的范围。
字号/行距通过后，如果还有这些 HARD finding，产品结果仍应为 FAIL。

## 5. 已冻结的修复原则

- 一次只修一个主病因。
- 规则能确定的由规则裁决；视觉美观问题由千问裁决。
- 千问只判断问题，不提供任意数值，不直接选择未注册工具。
- 字号和行距只能从工程预设的有限安全候选中选择。
- 优先纵向调整；横向 bbox 一般不动。
- 独立多栏一次只修一栏；成对栏必须同步修。
- 先过机械硬门禁，再调用千问复判。
- 禁止按公司名、页码、固定文字、固定坐标写特例。
- 产品质量失败但已有真实译文候选时保留 `result.pdf`；能力或流程失败且 `candidate_pdf` 为空时不生成 `result.pdf`，错误只写结构化报告。不得用原文或后端错误面板冒充结果页。

## 6. 下一步直接执行

1. 在测试中先覆盖：动作去重、同病因回滚、状态循环、候选耗尽。
2. 新增字号/行距病因标准化规则。
3. 新增字号恢复和行距恢复两个修复原子。
4. 在 `failure_dispatch_table.json` 注册这两个病因和工具。
5. 在 `engine.py` 接入“修复 -> 门禁 -> 复判 -> 接受/回滚”循环。
6. 跑完 multi 定向测试和完整测试。
7. 从第 12 轮 `source_pages` 建立第 13 轮，串行运行 7 页。
8. 汇总已有真实候选的 `result.pdf`、所有页面的最终裁决和修复记忆；缺少候选的失败页在汇总中显式记为无产品 PDF。

第 13 轮尚未创建，修复闭环代码也尚未开始写。新会话应从第 1 步开始。

## 7. 关键文件

```text
toolboxes/body/flow_text/multi/tools/engine.py
toolboxes/body/flow_text/multi/tools/layout_planner.py
toolboxes/body/flow_text/multi/tools/typography_adjudication.py
toolboxes/body/flow_text/multi/contracts/failure_dispatch_table.json
spikes/page_toolbox_engine_puncture_v1/tests/test_p5_body_flow_text_multi.py
spikes/page_toolbox_engine_puncture_v1/scripts/run_p5_seeded_case.py
spikes/page_toolbox_engine_puncture_v1/scripts/finalize_p5_multi_batch.py
```

自建千问配置：

```text
PAGE_TOOLBOX_QWEN_BASE_URL=<OpenAI-compatible endpoint>
PAGE_TOOLBOX_QWEN_MODEL=Qwen/Qwen3.6-35B-A3B
PAGE_TOOLBOX_QWEN_API_KEY=<只通过环境变量注入，不能写入仓库>
```

新会话建议使用 `pdf` 技能检查最终 PDF。当前工作树另有 `single` 的既有修改，续作不要清理或顺手改动。
