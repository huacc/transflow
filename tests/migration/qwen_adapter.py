"""实现仅供 P5 迁移质量测试使用的真实千问 OpenAI 兼容适配器。"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
from typing import Any

import httpx

from transflow.domain.classification import ModelDecision, ModelDecisionRequest
from transflow.domain.errors import ErrorCode, PortCallError

LOGGER = logging.getLogger("transflow.tests.migration.qwen_adapter")
BASE_URL_ENV = "TRANSFLOW_MIGRATION_QWEN_BASE_URL"
API_KEY_ENV = "TRANSFLOW_MIGRATION_QWEN_API_KEY"
MODEL_ENV = "TRANSFLOW_MIGRATION_QWEN_MODEL"


def _required_environment(name: str) -> str:
    """读取必填迁移测试环境变量，错误中只显示变量名。"""

    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing_environment_variable:{name}")
    return value


def migration_environment_ready() -> bool:
    """只返回三个迁移环境变量是否齐全，不读取或打印秘密内容。"""

    return all(os.environ.get(name, "").strip() for name in (BASE_URL_ENV, API_KEY_ENV, MODEL_ENV))


def _parse_json_content(content: object) -> dict[str, Any] | None:
    """解析普通或 Markdown 围栏中的单一 JSON 对象。"""

    if not isinstance(content, str):
        return None
    candidate = content.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        candidate,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        candidate = fenced.group(1)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _response_schema(request: ModelDecisionRequest) -> dict[str, Any]:
    """生成当前节点 allow-list 和证据引用均封闭的响应 Schema。"""

    node_key = str(request.node_spec["node_key"])
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
                            {"type": "string", "enum": list(request.allowed_actions)},
                            {"type": "null"},
                        ]
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence_refs": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(request.evidence_ids)},
                    },
                    "reason_summary": {"type": "string"},
                },
                "required": [
                    "node_key",
                    "status",
                    "selected_child",
                    "confidence",
                    "evidence_refs",
                    "reason_summary",
                ],
                "additionalProperties": False,
            },
        },
    }


class MigrationQwenDecisionAdapter:
    """通过环境变量装配真实模型，并实现 ModelDecisionPort 测试合同。"""

    def __init__(self, timeout_seconds: float = 180.0) -> None:
        """读取非持久化连接参数并初始化线程安全调用计数。"""

        self._base_url = _required_environment(BASE_URL_ENV).rstrip("/")
        self._api_key = _required_environment(API_KEY_ENV)
        self._model = _required_environment(MODEL_ENV)
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._call_count = 0

    @property
    def call_count(self) -> int:
        """返回真实 HTTP 调用次数，不包含任何请求或响应正文。"""

        with self._lock:
            return self._call_count

    def decide(self, request: ModelDecisionRequest) -> ModelDecision:
        """调用真实多模态模型并把严格 JSON 归一为领域 ModelDecision。"""

        evidence = copy.deepcopy(request.typed_evidence)
        page_image = evidence["page_image"]
        data_url = str(page_image.pop("data_url"))
        user_payload = {
            "allowed_choices": list(request.allowed_actions),
            "evidence": evidence,
            "image_content_policy": "图片只用于分类，不产生翻译或重排输入",
            "node_key": request.node_spec["node_key"],
            "stage": request.node_spec["stage"],
        }
        messages = [
            {"role": "system", "content": request.node_spec["prompt"]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(user_payload, ensure_ascii=False, sort_keys=True),
                    },
                    {"type": "text", "text": "当前待分类匿名页面 IMG1："},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        payload = {
            "model": self._model,
            "messages": messages,
            "response_format": _response_schema(request),
            "stream": False,
            "temperature": 0,
            "top_p": 1,
        }
        LOGGER.info(
            "调用真实迁移模型，意图=生成匿名分类质量证据 node=%s stage=%s",
            request.node_spec["node_key"],
            request.node_spec["stage"],
        )
        started = time.perf_counter()
        try:
            with self._lock:
                self._call_count += 1
            with httpx.Client(timeout=self._timeout_seconds, trust_env=False) as client:
                response = client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            response.raise_for_status()
            body = response.json()
            parsed = _parse_json_content(body["choices"][0]["message"]["content"])
        except httpx.TimeoutException as error:
            raise PortCallError(ErrorCode.AI_TIMEOUT, True, "真实迁移模型超时") from error
        except httpx.HTTPStatusError as error:
            code = (
                ErrorCode.AI_RATE_LIMITED
                if error.response.status_code == 429
                else ErrorCode.AI_SERVER_ERROR
            )
            raise PortCallError(
                code, error.response.status_code >= 500, "真实迁移模型 HTTP 失败"
            ) from error
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            raise PortCallError(
                ErrorCode.AI_RESPONSE_INVALID, False, type(error).__name__
            ) from error
        LOGGER.info(
            "真实迁移模型返回，意图=记录延迟而不记录正文 latency_ms=%s",
            round((time.perf_counter() - started) * 1000),
        )
        return self._normalize(parsed, request)

    @staticmethod
    def _normalize(
        value: dict[str, Any] | None,
        request: ModelDecisionRequest,
    ) -> ModelDecision:
        """严格校验节点、状态、动作、置信度和证据引用。"""

        required = {
            "confidence",
            "evidence_refs",
            "node_key",
            "reason_summary",
            "selected_child",
            "status",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "真实模型响应字段非法")
        if value["node_key"] != request.node_spec["node_key"]:
            raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "真实模型节点不一致")
        status = value["status"]
        selected = value["selected_child"]
        if status not in {"DECIDED", "INCONCLUSIVE"}:
            raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "真实模型状态非法")
        if status == "DECIDED" and selected not in request.allowed_actions:
            raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "真实模型动作越界")
        if status == "INCONCLUSIVE" and selected is not None:
            raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "不确定响应不得选择动作")
        confidence = value["confidence"]
        refs = value["evidence_refs"]
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "真实模型置信度非法")
        if not 0 <= float(confidence) <= 1:
            raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "真实模型置信度越界")
        if not isinstance(refs, list) or not all(ref in request.evidence_ids for ref in refs):
            raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "真实模型证据引用非法")
        result_code = str(selected) if status == "DECIDED" else "INCONCLUSIVE"
        return ModelDecision(
            decision_id=request.decision_id,
            decision_kind=request.decision_kind,
            result_code=result_code,
            evidence_ids=tuple(str(ref) for ref in refs),
            confidence=float(confidence),
            reason_summary=str(value["reason_summary"])[:500],
        )


def main() -> int:
    """只报告迁移环境是否就绪，不打印端点、模型或密钥。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    print(f"P5_MIGRATION_QWEN_ENV ready={migration_environment_ready()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
