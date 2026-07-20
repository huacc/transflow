"""实现 TranslationPort 与 ModelDecisionPort 共用的受控 HTTP Adapter。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from transflow.domain.classification import ModelDecision, ModelDecisionRequest
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.translation import TranslationBatch, TranslationBundle

LOGGER = logging.getLogger("transflow.adapters.ai.http")
ADAPTERS_ROOT = Path(__file__).resolve().parent.parent


class HttpAiCapabilityAdapter:
    """通过同一服务实现两个独立逻辑 Port，不暴露服务内部 Provider。"""

    def __init__(
        self,
        base_url: str,
        service_token: str,
        timeout_seconds: float,
        max_request_bytes: int,
    ) -> None:
        """保存无查询串基础 URL、内存令牌、超时和请求大小上限。"""

        if not base_url.startswith(("http://", "https://")):
            raise ValueError("AI capability base_url 无效")
        if not service_token:
            raise ValueError("AI capability service token 为空")
        if timeout_seconds <= 0 or max_request_bytes < 1:
            raise ValueError("AI capability HTTP 限制无效")
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout_seconds = timeout_seconds
        self._max_request_bytes = max_request_bytes

    def _post(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        """执行受控 JSON POST 并映射超时、限流、服务端和合同错误。"""

        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self._max_request_bytes:
            raise PortCallError(ErrorCode.AI_REQUEST_TOO_LARGE, False, "AI 请求超过本地上限")
        LOGGER.info("调用 AI capability，意图=执行结构化 HTTP 合同 route=%s", route)
        try:
            with httpx.Client(timeout=self._timeout_seconds, trust_env=False) as client:
                response = client.post(
                    f"{self._base_url}{route}",
                    content=encoded,
                    headers={
                        "Authorization": f"Bearer {self._service_token}",
                        "Content-Type": "application/json",
                        "X-Request-ID": str(payload.get("batch_id") or payload.get("decision_id")),
                    },
                )
        except httpx.TimeoutException as error:
            raise PortCallError(ErrorCode.AI_TIMEOUT, True, "AI capability 请求超时") from error
        except httpx.HTTPError as error:
            raise PortCallError(ErrorCode.PORT_UNAVAILABLE, True, type(error).__name__) from error
        if response.status_code == 429:
            raise PortCallError(ErrorCode.AI_RATE_LIMITED, True, "AI capability 限流")
        if response.status_code >= 500:
            raise PortCallError(ErrorCode.AI_SERVER_ERROR, True, "AI capability 服务错误")
        if response.status_code in {401, 403}:
            raise PortCallError(ErrorCode.AI_AUTH_FAILED, False, "AI capability 鉴权失败")
        if response.status_code == 413:
            raise PortCallError(ErrorCode.AI_REQUEST_TOO_LARGE, False, "AI capability 拒绝过大请求")
        if not 200 <= response.status_code < 300:
            raise PortCallError(
                ErrorCode.PORT_CONTRACT_VIOLATION,
                False,
                f"AI capability HTTP {response.status_code}",
            )
        try:
            parsed = response.json()
        except ValueError as error:
            raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "AI 响应不是 JSON") from error
        if not isinstance(parsed, dict):
            raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "AI 响应必须是对象")
        return parsed

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """调用翻译 HTTP 合同并严格校验 batch 与 unit 身份。"""

        response = self._post(
            "/v1/translation",
            {
                "batch_id": batch.batch_id,
                "schema_version": "transflow.translation-request/v1",
                "source_language": batch.source_language,
                "target_language": batch.target_language,
                "units": [
                    {"source_text": unit.source_text, "unit_id": unit.unit_id}
                    for unit in batch.units
                ],
            },
        )
        try:
            if (
                response["schema_version"] != "transflow.translation-bundle/v1"
                or response["batch_id"] != batch.batch_id
                or tuple(response["requested_unit_ids"]) != batch.ordered_unit_ids
            ):
                raise ValueError("翻译响应顶层身份不一致")
            return TranslationBundle.from_dict(response)
        except (DomainContractError, KeyError, TypeError, ValueError) as error:
            raise PortCallError(ErrorCode.AI_RESPONSE_INVALID, False, "翻译响应违反合同") from error

    def decide(self, request: ModelDecisionRequest) -> ModelDecision:
        """调用模型判定 HTTP 合同并校验请求身份与结构化结果。"""

        response = self._post(
            "/v1/model-decision",
            {
                "decision_id": request.decision_id,
                "decision_kind": request.decision_kind,
                "evidence_ids": list(request.evidence_ids),
                "schema_version": request.schema_version,
                "node_spec": request.node_spec,
                "typed_evidence": request.typed_evidence,
                "allowed_actions": list(request.allowed_actions),
                "attempt_budget": request.attempt_budget,
                "prompt_version": request.prompt_version,
            },
        )
        try:
            if response["schema_version"] != "transflow.model-decision/v1":
                raise ValueError("模型判定 Schema 版本不一致")
            decision = ModelDecision(
                decision_id=response["decision_id"],
                decision_kind=response["decision_kind"],
                result_code=response["result_code"],
                evidence_ids=tuple(response["evidence_ids"]),
                confidence=float(response.get("confidence", 1.0)),
                reason_summary=str(response.get("reason_summary", ""))[:500],
            )
            identity_mismatch = (
                decision.decision_id != request.decision_id
                or decision.decision_kind != request.decision_kind
            )
            if identity_mismatch:
                raise ValueError("模型判定身份不一致")
            if request.allowed_actions and decision.result_code not in {
                *request.allowed_actions,
                "INCONCLUSIVE",
            }:
                raise ValueError("模型判定结果越出 allow-list")
            if not set(decision.evidence_ids).issubset(request.evidence_ids):
                raise ValueError("模型判定引用未知证据")
            return decision
        except (DomainContractError, KeyError, TypeError, ValueError) as error:
            raise PortCallError(
                ErrorCode.AI_RESPONSE_INVALID,
                False,
                "模型判定响应违反合同",
            ) from error


def main() -> int:
    """记录 HTTP Adapter 必须由集中配置和环境令牌装配。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("HttpAiCapabilityAdapter 示例不读取或打印任何真实服务令牌")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
