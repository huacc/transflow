"""检测 Route/owner 能力错配并形成只读离线反馈与安全回退。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from transflow.domain.common import require_non_empty, require_sha256
from transflow.domain.completeness import SemanticUnitMap
from transflow.domain.errors import ErrorCode
from transflow.domain.pages import PageOutcome
from transflow.domain.states import (
    ArtifactIntegrity,
    ArtifactProduced,
    Capability,
    Fallback,
    PagePipelineState,
    Quality,
    TranslationCoverage,
)
from transflow.pdf_kernel.facts import ExtractedPageFacts

LOGGER = logging.getLogger("transflow.application.route_capability")
APPLICATION_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class RouteCapabilityEvidence:
    """保存分类或结构审计给出的只读能力前提，不允许改写 Route。"""

    evidence_id: str
    required_owner: str
    reason_code: str
    audit_status: str

    def __post_init__(self) -> None:
        """校验证据身份、目标 owner、原因和状态。"""

        for value, name in (
            (self.evidence_id, "evidence_id"),
            (self.required_owner, "required_owner"),
            (self.reason_code, "reason_code"),
            (self.audit_status, "audit_status"),
        ):
            require_non_empty(value, name)


@dataclass(frozen=True, slots=True)
class RouteCapabilityMismatchFinding:
    """记录当前 Route 无法建立 owner/map 时的结构证据和安全出口。"""

    page_no: int
    selected_route: str
    required_owner: str
    reason_code: str
    facts_hash: str
    map_hash: str
    evidence_id: str
    code: str = ErrorCode.ROUTE_CAPABILITY_MISMATCH.value
    fallback: str = Fallback.PAGE_PASSTHROUGH.value

    def __post_init__(self) -> None:
        """校验页码、路由、owner、哈希和固定错误/回退代码。"""

        if self.page_no < 1:
            raise ValueError("Route mismatch 页码无效")
        for value, name in (
            (self.selected_route, "selected_route"),
            (self.required_owner, "required_owner"),
            (self.reason_code, "reason_code"),
            (self.evidence_id, "evidence_id"),
        ):
            require_non_empty(value, name)
        require_sha256(self.facts_hash, "facts_hash")
        require_sha256(self.map_hash, "map_hash")
        if self.code != ErrorCode.ROUTE_CAPABILITY_MISMATCH.value:
            raise ValueError("Route mismatch 错误码不可改写")
        if self.fallback != Fallback.PAGE_PASSTHROUGH.value:
            raise ValueError("Route mismatch 必须整页安全透传")

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入审计 Artifact 的纯 JSON 对象。"""

        return {
            "code": self.code,
            "evidence_id": self.evidence_id,
            "facts_hash": self.facts_hash,
            "fallback": self.fallback,
            "map_hash": self.map_hash,
            "page_no": self.page_no,
            "reason_code": self.reason_code,
            "required_owner": self.required_owner,
            "selected_route": self.selected_route,
        }


class RouteCapabilityGuard:
    """只比较冻结 Route 与结构前提；从不重分类、写 Catalog 或调用其他叶。"""

    def __init__(self) -> None:
        """初始化三个禁止旁路计数，合法实现始终保持为零。"""

        self._route_write_count = 0
        self._catalog_write_count = 0
        self._cross_leaf_private_call_count = 0

    @property
    def forbidden_operation_counts(self) -> dict[str, int]:
        """返回运行时改 Route/Catalog 与跨叶私有调用计数。"""

        return {
            "catalog_writes": self._catalog_write_count,
            "cross_leaf_private_calls": self._cross_leaf_private_call_count,
            "route_writes": self._route_write_count,
        }

    def evaluate(
        self,
        selected_route: str,
        facts: ExtractedPageFacts,
        semantic_map: SemanticUnitMap,
        evidence: RouteCapabilityEvidence | None = None,
    ) -> RouteCapabilityMismatchFinding | None:
        """在 owner/map 不成立时返回 finding，否则保持冻结 Route 原样。"""

        LOGGER.info(
            "调用 Route 能力检查，意图=错配时安全回退而不重分类 route=%s page_no=%s",
            selected_route,
            semantic_map.page_no,
        )
        required_owner: str | None = None
        reason_code: str | None = None
        evidence_id = semantic_map.map_hash
        if semantic_map.unresolved_unit_ids:
            required_owner = "unresolved.owner"
            reason_code = "semantic_unit_owner_unresolved"
        elif evidence is not None and evidence.required_owner != selected_route:
            required_owner = evidence.required_owner
            reason_code = evidence.reason_code
            evidence_id = evidence.evidence_id
        elif (
            selected_route in {"body.flow_text.single", "body.flow_text.multi"}
            and facts.table_objects
        ):
            required_owner = "body.table"
            reason_code = "detected_table_requires_table_owner"
        if required_owner is None or reason_code is None:
            return None
        return RouteCapabilityMismatchFinding(
            semantic_map.page_no,
            selected_route,
            required_owner,
            reason_code,
            facts.kernel_facts_hash,
            semantic_map.map_hash,
            evidence_id,
        )

    @staticmethod
    def fallback_outcome(finding: RouteCapabilityMismatchFinding) -> PageOutcome:
        """把错配 finding 投影为可最终化但产品失败的安全透传终态。"""

        return PageOutcome(
            page_no=finding.page_no,
            state=PagePipelineState.FINALIZED,
            artifact_produced=ArtifactProduced.YES,
            integrity=ArtifactIntegrity.PASS,
            translation_coverage=TranslationCoverage.NONE,
            capability=Capability.PARTIAL,
            quality=Quality.FAIL,
            fallback=Fallback.PAGE_PASSTHROUGH,
            finding_codes=(finding.code,),
        )


def load_classification_audit(path: Path) -> tuple[dict[str, Any], ...]:
    """逐行读取 457 条当前分类审计，不使用其中宿主机绝对路径。"""

    LOGGER.info("调用分类审计导入，意图=重算当前目录统计 path_name=%s", path.name)
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            raw = json.loads(line)
            rows.append(
                {
                    "audit_status": str(raw["audit_status"]),
                    "candidate_leaves": tuple(str(item) for item in raw["candidate_leaves"]),
                    "current_leaf": str(raw["current_leaf"]),
                    "reason_code": str(raw["reason_code"]),
                    "sample_id": str(raw["sample_id"]),
                    "suggested_leaf": (
                        str(raw["suggested_leaf"])
                        if raw.get("suggested_leaf") is not None
                        else None
                    ),
                }
            )
    return tuple(rows)


def audit_status_counts(rows: tuple[dict[str, Any], ...]) -> dict[str, int]:
    """从逐页记录重算正确、错误和歧义数量。"""

    counts = {"CORRECT": 0, "ERROR": 0, "AMBIGUOUS": 0}
    for row in rows:
        counts[str(row["audit_status"])] += 1
    return counts


def main() -> int:
    """记录 RouteCapabilityGuard 只有 finding 和安全回退两个出口。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("RouteCapabilityGuard 示例，意图=禁止运行时自动重分类旁路")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
