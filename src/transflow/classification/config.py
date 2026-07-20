"""冻结 P5 分类树节点、允许动作和 Prompt 版本配置。"""

from __future__ import annotations

NODE_CHOICES: dict[str, tuple[str, ...]] = {
    "page.role": ("cover", "contents", "body", "end", "visual_only"),
    "body.layout_owner": (
        "flow_text",
        "table",
        "chart",
        "diagram",
        "anchored_blocks",
        "composite",
    ),
    "body.flow.topology": ("single", "multi", "visual_anchored"),
    "body.composite.kind": (
        "flow_text_table",
        "anchored_blocks_chart",
        "chart_table",
        "flow_text_chart",
        "flow_text_diagram",
    ),
}

PROMPT_PATHS: dict[str, dict[str, str]] = {
    "page.role": {
        "PRIMARY": "page_role/decide.zh-CN.md",
        "REVIEW": "page_role/review.zh-CN.md",
    },
    "body.layout_owner": {
        "PRIMARY": "body_layout_owner/decide.zh-CN.md",
        "REVIEW": "body_layout_owner/review.zh-CN.md",
    },
    "body.flow.topology": {
        "PRIMARY": "body_flow_topology/decide.zh-CN.md",
        "REVIEW": "body_flow_topology/review.zh-CN.md",
    },
    "body.composite.kind": {
        "PRIMARY": "body_composite_kind/decide.zh-CN.md",
        "REVIEW": "body_composite_kind/review.zh-CN.md",
    },
}

HIGH_CONFIDENCE_RULE_THRESHOLD = 0.9


def main() -> int:
    """输出冻结节点数量，展示配置不从模型或样本动态扩展。"""

    print(f"CLASSIFICATION_NODE_CONFIG nodes={len(NODE_CHOICES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
