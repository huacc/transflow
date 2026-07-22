# body.composite.kind 复合页工具组合初判

上一级已经确认 `page.role=body` 且 `body.layout_owner=composite`。你本次只判断哪两类主体结构共同拥有页面的翻译排版权，不重新判断页面角色、栏数、密度、语种或内容主题。

你只能使用匿名页面截图和原始结构证据。禁止根据文件名、路径、人工标签或旧分类结果判断。

图片只用于分类。图片像素及图片内部文字、数字、图例和标签不翻译、不重排；独立 PDF 文字对象仍可处理。

## 当前节点

```text
node_key = body.composite.kind
allowed_choices = flow_text_table | anchored_blocks_chart | chart_table | flow_text_chart | flow_text_diagram
```

允许返回 `INCONCLUSIVE`，但不能把它当成普通类别。

## 先识别五种结构

- `flow_text`：具有阅读顺序的连续段落、条款或说明文字；不是标题、图注或短脚注。
- `table`：具有明确行列、表头、项目行和单元格数据。
- `chart`：用柱、线、点、扇区、环形、坐标轴、图例或数据标签表达数量关系。
- `diagram`：用节点、箭头、连线、层级或时间顺序表达流程、组织或关系，不依赖数据坐标表达数值。
- `anchored_blocks`：多个相互独立的 KPI、业务卡片或信息块，各自拥有标题、边界、锚点和说明文字。

只有两种结构都占显著区域、都含不可忽略信息、后续必须调度不同工具箱时，才选择复合类别。标题、短图注、来源、脚注、小图标、装饰线和照片附件不能成为第二主体。

## 容器归属与候选证据边界

- 按视觉容器和实际工具所有权判断结构，不按原生文字总量投票。卡片内的说明文字归属于 anchored_blocks，不能因为说明较长就再次计为 `flow_text`。
- `TABLE1` 或 `BTABLE1` 的表格检测框只是候选几何证据；对齐的卡片网格也可能被检测为表格。只有截图中存在语义行列、表头、项目行和单元格数据时，才能确认 `table`。
- 多个独立信息块与主体数据图表共同占据显著区域时，优先识别为 `anchored_blocks_chart`；页面中的标题、引言或区域说明不能覆盖这两个需要独立工具处理的主体。
- 只有卡片不存在或只是附属元素，且卡片外的连续正文与图表共同主导页面时，才选择 `flow_text_chart`。三种结构确实同等主导时返回 `INCONCLUSIVE`。
- 多个图表面板仍属于 chart，不因为每个面板有标题、注释或边界就变成 anchored_blocks_chart。
- 如果上一级所谓 composite 实际只识别出 `flow_text + anchored_blocks`，或只存在一个 owner，返回
  `INCONCLUSIVE`；不能为凑允许类别虚构 chart、table 或 diagram。
- 散点图、矩阵图旁或下方的分类议题清单，若包含许多可独立翻译的议题项并占据显著区域，不只是
  颜色图例或短索引；这些议题项属于 anchored_blocks，与主体图表组合为 `anchored_blocks_chart`。

## 五个允许类别

### flow_text_table

实质连续正文与主体表格并存。正文流工具和表格工具必须分别处理各自区域。

### anchored_blocks_chart

多个独立信息块与主体数据图表并存。卡片工具处理独立块，图表区域保持固定。
大数字、百分比、状态圆点、装饰图标和卡片边界本身都不是数据图表；必须另外看到柱、线、点、扇区、
环形、坐标轴、图例或数据标签关系。
分类议题清单中的编号可与散点对应，但编号对应关系不自动把整份长清单降为图例；各议题项需要
独立翻译和局部 fit 时，清单仍是 anchored_blocks owner。

### chart_table

主体数据图表与主体表格并存。只有表格标题、图注或几行来源说明时，不属于本类。

### flow_text_chart

实质连续正文与主体数据图表并存。正文旁只有照片或装饰图形时，不属于本类。

### flow_text_diagram

实质连续正文与主体流程图、组织图、层级图或关系图并存。柱状图、折线图、饼图属于 `chart`，不能判成本类。

## 具象化正例与强反例

### C1 flow_text_table 正例

页面上半部是跨多列财务表，下半部有两栏长段分析文字；表格与正文都占据显著面积。判断 `flow_text_table`。

强反例：页面几乎全部是财务表，表外只有标题和两行脚注。上一级应判 `table`，不应进入本节点。

### C2 anchored_blocks_chart 正例

页面有四个带边界的业务卡片和一个占据约三分之一页面的环形收入图；卡片与图表都不可忽略。判断 `anchored_blocks_chart`。

强反例：四宫格 KPI 只有大数字和说明，没有柱、线、扇区、环形或坐标轴。上一级应判 `anchored_blocks`。

### C3 chart_table 正例

页面上半部是带坐标轴和多组柱形的数据图，下半部是对应年度、项目和数值的明细表；两部分面积接近。判断 `chart_table`。

强反例：整页是财务表，只在标题旁放了一个装饰性圆形图标。判断 `table`，不是 `chart_table`。

### C4 flow_text_chart 正例

页面左侧为多段经营分析正文，右侧为占据近半页的折线图，正文和图表共同解释经营趋势。判断 `flow_text_chart`。

强反例：正文旁只有一张人物照片；照片内部不处理，正文仍是唯一排版主体。判断 `flow_text`，不是 `flow_text_chart`。

### C5 flow_text_diagram 正例

页面上半部是多段流程说明，下半部是由多个节点和箭头组成的审批流程图，两部分都不可忽略。判断 `flow_text_diagram`。

强反例：正文旁是带坐标轴的柱状图。那是 `flow_text_chart`，不是 `flow_text_diagram`。

## 冲突裁决

1. 先排除标题、图注、脚注、装饰图和照片附件。
2. 再判断两个真正主导页面的结构。
3. `chart` 与 `diagram` 以“数据坐标关系”还是“节点连接关系”区分。
4. 如果三种结构同等主导，或无法确定哪两种需要独立工具箱，返回 `INCONCLUSIVE`，不要创造新类别。
5. `selected_child` 必须与 reason_summary 中识别出的两类主体一致；理由没有确认语义表格时不得选择含 `table` 的类别。

## 输出

只返回一个 JSON 对象：

```json
{
  "node_key": "body.composite.kind",
  "status": "DECIDED",
  "selected_child": "chart_table",
  "confidence": 0.92,
  "evidence_refs": ["IMG1", "TEXT1", "TABLE1", "DRAWING1"],
  "reason_summary": "主体数据图表与主体明细表都不可忽略"
}
```

证据不足时返回 `INCONCLUSIVE` 和 null，不得输出其他组合名称或隐藏推理。
