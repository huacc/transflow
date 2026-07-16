# body.flow_text.visual_anchored 工具箱

当前成熟度：`EXPERIMENTAL`。P12 已形成真实 Gate 结论：`FAIL`，不得晋级。

本目录只处理“连续正文与固定图片、有色面板或主视觉强绑定”的单页 PDF。固定视觉层不可移动；独立原生文字只能在所属 `VisualTextSlot` 内翻译和适配，图片像素内文字不进入请求。

执行顺序：冻结边界与三分区 → 单页 development tracer → 2 页 development 扩展 → 固定译文与真实千问 → regression → 冻结后 holdout → 视觉复核 → Gate。只有完整验收包通过才创建 `promotion_manifest.json`。

最终证据：固定译文 development `3/3`，真实千问 regression `20/20`，recorded 重放 `20/20`，P12 单测 `30/30`；冻结 holdout 自动裁决 `7/8`，人工产品复核仅 `3/8` 可接受。失败包括一页源签名区对比度低于 `1.5` 硬下限，以及四页双语重复或普通大写词误作缩写保留。详见 [执行摘要](reports/p12_execution_summary.md)、[视觉复核](reports/p12_visual_review.md) 和 [独立验收](reports/p12_independent_acceptance.md)。
