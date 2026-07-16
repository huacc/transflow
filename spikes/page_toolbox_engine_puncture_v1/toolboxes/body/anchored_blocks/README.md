# body.anchored_blocks 工具箱

当前成熟度：`EVIDENCE_INSUFFICIENT`。

本目录只处理 `body.anchored_blocks`。分类、翻译 Provider 和公共 PDF 机械能力不在此目录实现。

执行顺序：画像与样本 → development 探索 → 流程冻结 → 固定译文 → 千问端到端 → regression → holdout → Gate → PromotionManifest。

当前实现已完成独立块 owner、块内翻译容器、保护对象、局部排版、渲染和双层裁决。最终页眉/页脚回归使用 `AB_EN_04_00434_p141`：语义页脚可翻译，纯数字页码保持原位；`runs/64_header_footer_single_recorded` 为 `PAGE_PASSED`。

原始冻结 holdout 只通过 4/6，后续修复证据已非盲，因此不生成 `promotion_manifest.json`。完整结论见 `stage_gate.json` 和 `reports/header_footer_regression.json`。
