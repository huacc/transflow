# body.flow.topology 正文工具拓扑初判

上两级已经确认 `page.role=body` 且 `body.layout_owner=flow_text`。你本次只判断正文需要哪一种排版工具拓扑，不判断内容语义、密度、语种或具体栏数。

你只能使用匿名页面截图和结构证据。禁止根据文件名、路径、人工标签或旧分类结果判断。

图片只用于分类。图片像素及图片内部内容不翻译、不重排；需要判断可编辑文字能否按普通栏道流动，还是被固定视觉区域约束。

## 当前节点

```text
node_key = body.flow.topology
allowed_choices = single | multi | visual_anchored
```

允许返回 `INCONCLUSIVE`。任何类别都不是兜底项。

## 分类定义

### single

主体可编辑文字只有一条稳定栏道，从上到下推进：

- 段落、项目符号、编号条款或日期事件都可以出现；
- 跨页宽标题、页眉、页脚、脚注不增加栏数；
- 工具可以在这一条栏道内执行换行、纵向扩容和下游推移。

### multi

主体可编辑文字形成两条或更多并列栏道：

- 每条栏道有稳定的 x 轴范围、纵向覆盖和栏间空白；
- 各栏必须独立维护，禁止跨栏串写和跨栏推移；
- 长段落、项目列表、日期事件或混合内容都可以属于本类；
- 上方跨栏标题、横跨页面的图片不改变主体栏道数量。

### visual_anchored

大面积固定图片、有色面板或主视觉决定版式，可编辑文字强绑定在特定视觉区域：

- 文字颜色、背景、边界或层叠关系不可脱离原视觉容器；
- 不能使用普通正文的全页纵向扩容和下游推移；
- 只能在原视觉槽位内进行字体、行距和 fit 调整；
- 图片内部内容不参与翻译和重排。

## 明确不参与路由的内容属性

以下只作为工具参数或翻译分组信息，不改变类别：

```text
content_pattern = prose | list | numbered | date_event | mixed
density_band = sparse | normal | dense
column_count = 具体正整数
```

## 具象化正例与强反例

### T1 single 正例

单栏主席报告包含长段落、编号议程或项目符号，但主体左右边界一致。判断 `single`。

### T2 multi 正例

两个页面都是三栏密集正文：一个以大量项目符号组织财务指标，另一个以较长段落和分组标题组织展望。两者工具操作相同，都判断 `multi`。

### T3 multi 正例

上方是横跨页面的固定照片，下方三栏排列日期和事件。忽略跨栏图片后，主体仍有三条栏道，判断 `multi`。

### T4 visual_anchored 正例

照片拼贴占页面大部分面积，右下角深色视觉区域中只有一组白色正文。判断 `visual_anchored`。

### T5 single 强反例

页面有多个上下排列的编号条款，但全部共享同一左右边界，仍是 `single`，不能因为“条目多”判多栏。

### T6 visual_anchored 强反例

页面上方有照片、下方是普通三栏白底正文，照片只是硬障碍，主体仍判断 `multi`。

## 输出

只返回一个 JSON 对象：

```json
{
  "node_key": "body.flow.topology",
  "status": "DECIDED",
  "selected_child": "multi",
  "confidence": 0.95,
  "evidence_refs": ["IMG1", "TEXT1", "B004"],
  "reason_summary": "主体文字形成三条并列且互不串写的栏道"
}
```

不得输出内容类型、具体栏数、密度、工具名或隐藏推理。证据不足时返回 `INCONCLUSIVE` 和 null。
