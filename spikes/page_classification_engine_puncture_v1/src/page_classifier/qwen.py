from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

from .config import NODE_CHOICES, NODE_PROMPTS, PROVIDER
from .io_utils import sha256_value
from .models import NodeJudgement, ProviderResult
from .provider import business_payload, call_chat


def judgement_schema(node_key: str, evidence_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": re.sub(r"[^a-zA-Z0-9_]", "_", node_key),
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "node_key": {"type": "string", "const": node_key},
                    "status": {"type": "string", "enum": ["DECIDED", "INCONCLUSIVE"]},
                    "selected_child": {
                        "anyOf": [
                            {"type": "string", "enum": NODE_CHOICES[node_key]},
                            {"type": "null"},
                        ]
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence_refs": {
                        "type": "array",
                        "items": {"type": "string", "enum": evidence_ids},
                    },
                    "reason_summary": {"type": "string"},
                },
                "required": ["node_key", "status", "selected_child", "confidence", "evidence_refs", "reason_summary"],
                "additionalProperties": False,
            },
        },
    }


def validate_judgement(
    value: dict[str, Any] | None,
    node_key: str,
    evidence_ids: set[str],
    source: str,
) -> NodeJudgement:
    if not isinstance(value, dict):
        return NodeJudgement(node_key, source, "INCONCLUSIVE", None, 0.0, (), "模型响应不是有效 JSON")
    required = {"node_key", "status", "selected_child", "confidence", "evidence_refs", "reason_summary"}
    if set(value) != required or value.get("node_key") != node_key:
        return NodeJudgement(node_key, source, "INCONCLUSIVE", None, 0.0, (), "模型响应字段不符合约束")
    status = value["status"]
    child = value["selected_child"]
    confidence = value["confidence"]
    refs = value["evidence_refs"]
    if status not in {"DECIDED", "INCONCLUSIVE"}:
        return NodeJudgement(node_key, source, "INCONCLUSIVE", None, 0.0, (), "status 非法")
    if status == "DECIDED" and child not in NODE_CHOICES[node_key]:
        return NodeJudgement(node_key, source, "INCONCLUSIVE", None, 0.0, (), "selected_child 非法")
    if status == "INCONCLUSIVE" and child is not None:
        return NodeJudgement(node_key, source, "INCONCLUSIVE", None, 0.0, (), "INCONCLUSIVE 不能选择子项")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        return NodeJudgement(node_key, source, "INCONCLUSIVE", None, 0.0, (), "confidence 非法")
    if not isinstance(refs, list) or not all(isinstance(ref, str) and ref in evidence_ids for ref in refs):
        return NodeJudgement(node_key, source, "INCONCLUSIVE", None, 0.0, (), "evidence_refs 非法")
    return NodeJudgement(
        node_key,
        source,
        status,
        child,
        float(confidence),
        tuple(refs),
        str(value["reason_summary"])[:500],
    )


def image_part(path: Path) -> dict[str, Any]:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}


class QwenJudge:
    def __init__(self, root: Path, prompt_text: dict[str, str]) -> None:
        self.root = root
        self.prompt_text = prompt_text

    def decide(
        self,
        *,
        node_key: str,
        stage: str,
        sample_id: str,
        evidence: dict[str, Any],
        compact_evidence: dict[str, Any],
        page_image: Path,
        review_context: dict[str, Any] | None = None,
        exemplar_images: list[tuple[str, Path]] | None = None,
    ) -> tuple[NodeJudgement, ProviderResult, str, str]:
        prompt_key = "review" if stage == "REVIEW" else "primary"
        prompt_path = NODE_PROMPTS[node_key][prompt_key]
        user = {
            "sample_id": sample_id,
            "node_key": node_key,
            "allowed_choices": NODE_CHOICES[node_key],
            "page_image_ref": "IMG1",
            "image_content_policy": "图片只用于分类，图片内部内容不是翻译或重排目标",
            "evidence": compact_evidence,
        }
        if review_context:
            user["disagreement"] = review_context
        content: list[dict[str, Any]] = [
            {"type": "text", "text": json.dumps(user, ensure_ascii=False, sort_keys=True)},
            {"type": "text", "text": "当前待分类页面 IMG1："},
            image_part(page_image),
        ]
        for label, path in exemplar_images or []:
            content.append({"type": "text", "text": f"已确认参考正例，仅比较版式特征：{label}"})
            content.append(image_part(path))
        messages = [
            {"role": "system", "content": self.prompt_text[prompt_path]},
            {"role": "user", "content": content},
        ]
        response_format = judgement_schema(node_key, evidence["evidence_ids"])
        payload_hash = sha256_value(business_payload(PROVIDER.model, messages, {"response_format": response_format}))
        result = call_chat(PROVIDER, messages, response_format=response_format)
        if result.error_code in {"TIMEOUT", "RATE_LIMIT"}:
            time.sleep(1)
            result = call_chat(PROVIDER, messages, response_format=response_format)
        source = "QWEN_REVIEW" if stage == "REVIEW" else "QWEN_PRIMARY"
        judgement = validate_judgement(result.parsed_json, node_key, set(evidence["evidence_ids"]), source)
        if result.error_code and result.error_code != "INVALID_JSON":
            judgement = NodeJudgement(node_key, source, "INCONCLUSIVE", None, 0.0, (), f"provider_error:{result.error_code}")
        return judgement, result, payload_hash, prompt_path
