from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str = "http://112.30.139.26:19400/v1"
    model: str = "Qwen/Qwen3.6-35B-A3B"
    api_key_env: str = "PAGE_CLASSIFIER_QWEN_API_KEY"

    def api_key(self) -> str:
        value = os.environ.get(self.api_key_env, "").strip()
        if not value:
            raise RuntimeError(f"missing_environment_variable:{self.api_key_env}")
        return value


PROVIDER = ProviderConfig()
GENERATION_PARAMS = {"temperature": 0, "top_p": 1, "stream": False}

NODE_CHOICES = {
    "page.role": ["cover", "contents", "body", "end", "visual_only"],
    "body.layout_owner": ["flow_text", "table", "chart", "diagram", "anchored_blocks", "composite"],
    "body.flow.topology": ["single", "multi", "visual_anchored"],
    "body.composite.kind": [
        "flow_text_table",
        "anchored_blocks_chart",
        "chart_table",
        "flow_text_chart",
        "flow_text_diagram",
    ],
}

NODE_PROMPTS = {
    "page.role": {
        "primary": "prompts/page_role/decide.zh-CN.md",
        "review": "prompts/page_role/review.zh-CN.md",
    },
    "body.layout_owner": {
        "primary": "prompts/body_layout_owner/decide.zh-CN.md",
        "review": "prompts/body_layout_owner/review.zh-CN.md",
    },
    "body.flow.topology": {
        "primary": "prompts/body_flow_topology/decide.zh-CN.md",
        "review": "prompts/body_flow_topology/review.zh-CN.md",
    },
    "body.composite.kind": {
        "primary": "prompts/body_composite_kind/decide.zh-CN.md",
        "review": "prompts/body_composite_kind/review.zh-CN.md",
    },
}
