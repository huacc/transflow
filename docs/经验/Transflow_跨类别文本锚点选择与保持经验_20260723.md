# Transflow 跨类别文本锚点选择与保持经验

日期：2026-07-23

## 1. 结论

现有类别并不是“都锁定左侧锚点”，但可以归纳出同一个跨类别不变量：

> 先确定文字属于哪个 owner、承担什么语义角色、源页采用什么对齐关系，再保持该角色的语义锚点；源字形 bbox 是首轮排版槽位和锚点证据，不是目标语言的统一宽度牢笼。

因此可以把“类型化语义锚点保持”提升到引擎上层合同，但不能把具体锚点选择提升成一个全局分类器：

- 叶 Toolbox 负责依据当前页的 owner、语义角色、源几何和同组关系选择锚点；
- 叶 Template / `PageEffectiveLayout` 记录源锚点和安全区域，`PagePatch` 序列化对齐方式与目标写入槽位；
- 共享 Patch 解释器和 Judge 只负责按声明的锚点执行、测量和门禁，不把“正文”“标题”简单映射成固定对齐方式。

这一区分很重要。正文通常保持左侧起点，数值列通常保持右边界，居中的图片、表格或图表标题通常保持 owner 中心轴；但源页左对齐的标题仍应保持左锚，不能因为角色名叫 `TITLE` 就被强制居中。

## 2. 核查范围与结果

本次按冻结分类树核查了 15 个已知可翻译终态类别，并单独核查 `visual_only`：

- `cover`、`contents`、`end`；
- `body.flow_text.single`、`body.flow_text.multi`、`body.flow_text.visual_anchored`；
- `body.table`、`body.chart`、`body.diagram`、`body.anchored_blocks`；
- 五个 composite：`flow_text_table`、`anchored_blocks_chart`、`chart_table`、`flow_text_chart`、`flow_text_diagram`；
- `visual_only` 不做原生文字翻译回填，不参与本合同的锚点选择。

`body.freeform` 是已知分类失败后的有界恢复出口，不属于上述已知叶能力证明，不能拿它反向证明所有类别已经通过锚点产品验收。

| 类别 | 已有证据 | 应保持的锚点语义 | 核查结论 |
|---|---|---|---|
| `cover` | `toolboxes/cover/tools/template_builder.py` 已按同组左边、右边和中心轴推断 `LEFT/CENTER/RIGHT`，安全框按锚点方向展开 | 稀疏标题的源左边、右边或中心轴 | 明确支持 |
| `contents` | README 和实现规定条目文字保持同语义左锚，页码/范围作为固定导航锚点 | 条目左锚；页码及其列关系锁定 | 明确支持 |
| `end` | 模板和渲染显式支持 left/center/right，联系信息和品牌落款可保留原字形与锚点 | 源边缘或中心轴；受保护文字保持原锚 | 明确支持 |
| `body.flow_text.single` | P4 规定普通正文保持 `x0` 和栏道，右锚定短标题安全向左扩展但保持原 `x1` | 正文左锚；右对齐标题右锚；横向使用所属栏安全宽度 | 明确支持 |
| `body.flow_text.multi` | 多栏合同保持各栏 `x0`、栏宽和 gutter；右锚定页级标题保留右边界 | 每栏左锚/栏边界；局部右锚例外 | 明确支持 |
| `body.flow_text.visual_anchored` | P12 已确认文字与视觉叙事/owner 的语义关系优先于纯距离，但没有单独枚举全部对齐模式 | 正文流锚点和视觉 owner 相对关系分别继承 | 原则一致；独立锚点回归仍需补齐 |
| `body.table` | P6 要求标签、数值和同行对象保持列归属，不能把数值列的对齐证据污染到标签列 | 标签/文本列左锚，数值列右锚或源 cell 对齐，行列 owner 不变 | 明确支持 |
| `body.anchored_blocks` | P11 明确规定卡片居中文字保持语义槽位中心轴，而不是源字形 `x0` | left/right 保持对应边缘；center 保持 owner/槽位中心轴 | 明确支持 |
| `body.chart` | P13 明确规定左对齐保 `x0`、右对齐保 `x1`、居中保中心轴，图例/轴/图形标签保相对关系 | 边缘、中心轴或图文相对锚点 | 明确支持 |
| `body.diagram` | P14 使用 `owner -> 锚点 -> 安全框` 三层定位 | 左锚、中心、基线或与节点/图例的相对关系 | 明确支持 |
| `body.composite.flow_text_table` | 既有 engine 声明正文语义左锚、表格列边界和横向顺序不变，渲染分别复用正文与表格对齐逻辑 | 每个 owner 继承 flow/table 锚点 | 实现证据支持；缺少独立经验文档和当前形式 Gate |
| `body.composite.anchored_blocks_chart` | P15 先冻结 `anchored/chart/shared/protected` 唯一所有权，再让各叶计划合并为一次写入 | 各 owner 继承 P11/P13 锚点 | 明确支持继承，不产生新的全局对齐 |
| `body.composite.chart_table` | P16 以 table 对象/列和 chart/legend owner 建立唯一绑定，禁止跨 owner 借位 | 表格行列锚点与图表相对锚点分别保持 | 明确支持继承 |
| `body.composite.flow_text_chart` | P17 明确保持正文左锚；图表内部源标签接近 owner 中心时保持居中 | flow 左锚；chart 中心/相对锚点 | 明确支持 |
| `body.composite.flow_text_diagram` | P18 规定正文、节点、共享标题和 margin 先恢复 owner；节点内方向、分隔符和相邻槽位关系必须保留 | flow 边缘锚点；diagram 节点/基线/相对锚点 | 明确支持继承 |

核查结果不是“15 个类别都已经产品通过”。它只证明现有经验和实现没有支持“所有文字锁左侧”或“所有标题居中”，而共同支持“按类别和 owner 选择、按上层合同保持”的方向。尤其 `visual_anchored` 和 `flow_text_table` 仍需要在其迁移轮次补独立锚点回归。

## 3. 锚点选择顺序

锚点必须在翻译排版前确定，推荐固定为以下顺序：

1. **确定 owner**：正文栏、表格 cell/列、图表、图片、节点、卡片、图例、margin 或受保护对象。
2. **确定语义角色**：正文、标题、标签、数值、单位、注释、页眉页脚等。
3. **读取源对齐证据**：同组 `x0`、`x1`、中心轴、基线、缩进、同行/同列重复关系，以及文字与 owner 的相对位置。
4. **选择锚点模式**：`LEFT`、`RIGHT`、`CENTER`，或由 owner 表达的基线/相对关系。
5. **推导安全框**：从所属栏、cell、节点、图形、页面边界、相邻 owner 和真实障碍推导，不把源字形 `x1` 当成天然排版终点。
6. **排版并测量真实字形**：自然换行由目标字体实测宽度决定，不继承源 PDF 的视觉行断点。
7. **按声明锚点验收**：Judge 比较候选真实 glyph bbox 与源锚点/owner 关系，而不是只检查 textbox 或最大允许框。

纵向调整与横向锚点分开处理。只要不越过结构带、相邻 owner 和页面边界，目标语言变长可向下延展并推动同一阅读流，目标语言变短可在有证据的纵向留白中调整字号、行距或段距；纵向变化不能成为横向漂移的理由。

## 4. 各锚点的安全展开方式

| 锚点模式 | 必须保持 | 目标语言变长时 |
|---|---|---|
| `LEFT` | 源/owner 左侧起点 | 向右使用连续安全空间；达到所属栏、cell、图形或相邻 owner 边界后自然换行 |
| `RIGHT` | 源/owner 右边界 | 向左使用连续安全空间；渲染仍为右对齐 |
| `CENTER` | 源语义槽位或 owner 中心轴 | 优先围绕中心轴对称扩展；多行仍共享同一中心轴 |
| owner-relative / baseline | 与节点、图例、色块、轴线、行列或基线的绑定关系 | 只在 owner 安全框内调整，保持方向、顺序和对应对象 |

表格还必须额外满足行列语义绑定：某个标签、数值或单位不能为了视觉 fit 被移动到另一业务行或另一列。锚点门禁通过不能替代数据对象对应关系门禁。

## 5. 标题不能按角色名一刀切

标题的锚点至少同时依赖：

- 标题与哪个 owner 建立了 `ABOVE / BELOW / OVERLAY` 等关系；
- 标题中心与 owner 中心是否在当前字号和 owner 尺度推导的相对容差内；
- 同一标题的多行或同组兄弟标题是否共享中心轴、左边缘或右边缘；
- 横向两侧是否存在对称安全空间；
- 源页是否已经明确表现为左对齐或右对齐。

只有这些源页证据支持居中时，图片、表格、图表或卡片标题才选择 `CENTER`。普通注释位于 owner 上方并不自动成为居中标题；长表格标题若源页左对齐，也继续保持 `LEFT`。

可使用类 Drools 的窄规则表达这一点，但事实必须来自当前页。例如：

```text
WHEN role == TITLE
 AND relation IN {ABOVE, BELOW, OVERLAY}
 AND source_center_axis_matches(owner)
 AND peer_lines_share_center_axis
THEN anchor = CENTER

WHEN role IN {BODY, LABEL}
 AND source_left_edge_matches(reading_flow_or_column)
THEN anchor = LEFT

WHEN role IN {NUMERIC, PAGE_NUMBER}
 AND source_right_edge_matches(cell_or_margin_band)
THEN anchor = RIGHT
```

规则中的容差应由当前字号、owner 尺寸和页面比例推导。不能按标题文字、公司名、样本编号、页码或绝对坐标触发。

## 6. 门禁必须检查真实锚点漂移

机械门禁至少应检查：

- `LEFT`：候选真实字形的左边缘与声明锚点的差值；
- `RIGHT`：候选真实字形的右边缘与声明锚点的差值；
- `CENTER`：候选真实字形中心与声明中心轴的差值；
- owner-relative：候选与节点、图例、色块、轴线、表格行列或基线的关系是否仍成立；
- 实际 glyph bbox 是否越过页面、所属 owner、相邻槽位或受保护对象；
- 表格/图表数据是否仍属于源行、源列和源系列；
- 标题与正文、图注与图形之间的字体层级和留白比例是否仍可接受。

门禁失败仍应保留真实译后诊断 PDF、Patch、Finding 和锚点差值，不能因为不可交付而不产出问题证据；但失败候选不能冒充产品通过。

### 6.1 门禁不得强于类别真实合同

门禁复用共享几何能力时，不能把排版搜索使用的“优选安全框”自动升级成类别的最终合法边界。至少要区分三层事实：

1. `source_glyph_bbox`：源字形位置，是锚点和既有相互作用的证据；
2. `layout_search_region`：叶 Layout 优先搜索的安全排版区域，可用于控制字号、行距和换行；
3. `hard_legal_boundary`：该类别真实禁止越过的 owner 边界，只有它才能直接形成越界硬门禁。

`layout_search_region` 可以比 `hard_legal_boundary` 更保守，但“没有完全落在优选安全框内”不等于产品非法。共享 Judge 应执行叶声明的真实边界，不能自行推导一个更窄的全局边界。

TM4 `body.diagram` 前向重验暴露了一个典型反例：

- 节点文字的 owner 是原始节点，P14 已验证的硬约束是候选文字不得越过 `node.boundary_bbox`；
- 节点内部 `safe_text_bbox` 是优选排版搜索区，不是所有节点、地图标签和已有装饰结构统一适用的最终合法边界；
- 若 Judge 改用 `safe_text_bbox` 作硬门禁，会把仍在原始节点内、语义 owner 未变的合法候选误判为 `DIAGRAM_NODE_TEXT_OUTSIDE_NODE`。

连接线碰撞也必须采用“相对源页是否新增”的判断，而不是要求候选与连接线绝对零相交：

- 只对节点外的局部标签比较连接线相交数量；
- `output_connector_hits > source_connector_hits` 才表示候选引入了新碰撞；
- 节点内文字不参与这项局部标签门禁，因为连接线本来就可能接入节点边界；
- 源页已经存在的相交关系属于基线事实，不能在译后被重新解释成新增缺陷。

可以把这一边界表达为窄规则：

```text
WHEN owner_kind == NODE
THEN hard_legal_boundary = node.boundary_bbox
 AND connector_increment_gate = NOT_APPLICABLE

WHEN owner_kind == LOCAL_LABEL
THEN hard_legal_boundary = owner.allowed_boundary
 AND connector_increment_gate =
     output_connector_hits > source_connector_hits

WHEN candidate outside layout_search_region
 AND candidate inside hard_legal_boundary
 AND no_new_owner_or_protected_object_collision
THEN do_not_fail_only_for_search_region_escape
```

这一经验可以跨类别提升为合同形态，但具体边界仍由叶声明：

- 叶 Template/Layout 输出 `layout_search_region`、`hard_legal_boundary`、源交互基线和适用豁免；
- 共享 Judge 负责比较“候选相对源页是否新增违规”，不把自己的保守启发式变成类别事实；
- 表格仍以 cell/行列边界为硬合同，正文仍以所属栏/阅读流边界为硬合同，不能照搬 diagram 的节点或连接线规则；
- 若新增门禁比冲刺主体更严，必须先用真实样本和结构扰动负例证明新增约束确属类别不变量，否则应收回到已验证边界。

## 7. 反过拟合边界

允许固化的是类别能力和相对关系：

- `LEFT / RIGHT / CENTER` 的锚点含义；
- 字号、行距、段距的有限搜索步长，例如相对源值上下约 10% 或固定小步长；
- 基于字号、页宽、owner 尺寸、空余度和相邻障碍推导的容差；
- “左锚向右扩、右锚向左扩、中心轴对称扩”的通用几何行为。

禁止固化的是具体页面事实：

- 样本 ID、文件名、页码、公司名、已知标题或已知数字；
- 某一页的绝对坐标和对象排列数量；
- `TITLE -> CENTER`、`BODY -> LEFT` 这类忽略 owner 和源对齐证据的无条件映射；
- 为了让当前失败页通过而放宽所有类别的锚点容差。

测试应同时包含源居中标题、源左对齐标题、同样位于 owner 上方但角色为注释的负例，以及缩放、平移、改文字长度后的结构等价探针。

## 8. 上层与叶 Toolbox 的职责边界

可以提升到总体设计的只有以下不变量：

1. 每个可译文字 owner 必须声明可验证的锚点语义；
2. 源字形 bbox 是锚点事实和首轮槽位，不是统一最大宽度；
3. 安全框必须按锚点方向展开，并由当前页 owner/障碍事实约束；Patch 操作必须可追溯回相应源锚点证据；
4. 候选与最终文档使用同一 Patch 解释器执行相同锚点语义；
5. Judge 使用最终真实字形验证锚点，不用允许框代替产品字形；
6. 上层不得把所有文字默认成左对齐，也不得把所有标题默认成居中。

不能提升到上层的是某个叶的角色识别、owner 构建、中心轴容差和局部修复顺序。这些仍由分类私有 Template/Layout/Judge 持有，并在对应类别迁移时独立验收。
