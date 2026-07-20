"""定义分类路由和模型决策的纯领域合同。"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from transflow.domain.common import require_non_empty, require_unique
from transflow.domain.errors import DomainContractError, ErrorCode

LOGGER = logging.getLogger("transflow.domain.classification")


@dataclass(frozen=True, slots=True)
class ClassificationRoute:
    """表示分类器选中的唯一稳定路由及其证据引用。"""

    route: str
    confidence: float
    evidence_ids: tuple[str, ...]
    complete_to_leaf: bool = True
    failed_node: str | None = None
    taxonomy_fallback: bool = False

    def __post_init__(self) -> None:
        """校验路由名、置信度范围和有序证据身份。"""

        require_non_empty(self.route, "route")
        require_unique(self.evidence_ids, "evidence_ids")
        if not 0 <= self.confidence <= 1:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "confidence 必须位于 [0, 1]")
        if self.complete_to_leaf and self.route == "unclassified":
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "未分类路由不能声明到达叶子")
        if not self.complete_to_leaf and not self.failed_node:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "未完成分类必须记录 failed_node")

    def as_dict(self) -> dict[str, Any]:
        """输出稳定、可审计的分类路由字典。"""

        return {
            "complete_to_leaf": self.complete_to_leaf,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
            "failed_node": self.failed_node,
            "route": self.route,
            "taxonomy_fallback": self.taxonomy_fallback,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ClassificationRoute:
        """从 JSON 字典恢复分类路由。"""

        return cls(
            payload["route"],
            payload["confidence"],
            tuple(payload["evidence_ids"]),
            payload.get("complete_to_leaf", True),
            payload.get("failed_node"),
            payload.get("taxonomy_fallback", False),
        )


@dataclass(frozen=True, slots=True)
class NodeJudgement:
    """表示规则、模型或 Resolver 对一个分类树节点的裁决。"""

    node_key: str
    source: str
    status: str
    selected_child: str | None
    confidence: float
    evidence_refs: tuple[str, ...]
    reason_summary: str

    def __post_init__(self) -> None:
        """校验节点裁决的状态、子项、置信度和证据引用。"""

        require_non_empty(self.node_key, "node_key")
        require_non_empty(self.source, "source")
        if self.status not in {"DECIDED", "INCONCLUSIVE"}:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "status 不受支持")
        if self.status == "DECIDED" and not self.selected_child:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "DECIDED 必须选择子项")
        if self.status == "INCONCLUSIVE" and self.selected_child is not None:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "INCONCLUSIVE 不得选择子项")
        if not 0 <= self.confidence <= 1:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "confidence 必须位于 [0, 1]")
        require_unique(self.evidence_refs, "evidence_refs")

    def as_dict(self) -> dict[str, Any]:
        """输出稳定且可 JSON 序列化的节点裁决。"""

        payload = asdict(self)
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload


@dataclass(frozen=True, slots=True)
class NodeResolution:
    """记录一个节点从规则、主判、复核到最终归约的完整审计。"""

    node_key: str
    rule: NodeJudgement
    primary: NodeJudgement
    review: NodeJudgement | None
    resolution: str
    final: NodeJudgement

    def as_dict(self) -> dict[str, Any]:
        """输出包含每个裁决来源的稳定审计字典。"""

        return {
            "final": self.final.as_dict(),
            "node_key": self.node_key,
            "primary": self.primary.as_dict(),
            "resolution": self.resolution,
            "review": self.review.as_dict() if self.review else None,
            "rule": self.rule.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ModelDecisionRequest:
    """表示非翻译类模型判定的结构化输入。"""

    decision_id: str
    decision_kind: str
    schema_version: str
    evidence_ids: tuple[str, ...]
    node_spec: dict[str, Any] = field(default_factory=dict)
    typed_evidence: dict[str, Any] = field(default_factory=dict)
    allowed_actions: tuple[str, ...] = ()
    attempt_budget: int = 1
    prompt_version: str = "transflow.classification-prompt/v1"

    def __post_init__(self) -> None:
        """校验判定身份、类型、Schema 版本和证据顺序。"""

        require_non_empty(self.decision_id, "decision_id")
        require_non_empty(self.decision_kind, "decision_kind")
        require_non_empty(self.schema_version, "schema_version")
        require_unique(self.evidence_ids, "evidence_ids")
        require_unique(self.allowed_actions, "allowed_actions")
        if self.attempt_budget != 1:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "单个分类判定预算必须为 1")
        require_non_empty(self.prompt_version, "prompt_version")


@dataclass(frozen=True, slots=True)
class ModelDecision:
    """表示经 Schema 校验后的非翻译类模型判定结果。"""

    decision_id: str
    decision_kind: str
    result_code: str
    evidence_ids: tuple[str, ...]
    confidence: float = 1.0
    reason_summary: str = ""

    def __post_init__(self) -> None:
        """校验输出与请求可通过稳定身份对齐。"""

        require_non_empty(self.decision_id, "decision_id")
        require_non_empty(self.decision_kind, "decision_kind")
        require_non_empty(self.result_code, "result_code")
        require_unique(self.evidence_ids, "evidence_ids")
        if not 0 <= self.confidence <= 1:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "confidence 必须位于 [0, 1]")


def main() -> int:
    """展示结构化分类路由合同的调用方式。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    route = ClassificationRoute("body.table", 1.0, ("evidence-1",))
    LOGGER.info("调用分类路由示例，意图=展示结构化判定 route=%s", route.route)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
