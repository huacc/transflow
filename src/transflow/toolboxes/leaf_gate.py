"""定义逐叶迁移证据、三态判定、证明哈希和 Catalog 发布规则。"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import Any

from transflow.domain.common import canonical_json_bytes, require_non_empty, require_sha256
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.toolboxes.catalog import ToolboxCatalogEntry

LOGGER = logging.getLogger("transflow.toolboxes.leaf_gate")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
NON_PROMOTABLE_ORIGINAL_STATES = frozenset({"PASS_NON_BLIND", "NOT_EVALUATED", "FAIL"})
COMPONENT_HASH_FIELDS = (
    "code_hash",
    "schema_hash",
    "catalog_hash",
    "font_hash",
    "threshold_hash",
)


class LeafGateConclusion(StrEnum):
    """列出叶级 Gate 唯一允许的三个结论。"""

    PASS_ENABLE = "PASS_ENABLE"
    PASS_DISABLED_WITH_FALLBACK = "PASS_DISABLED_WITH_FALLBACK"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class LeafMigrationEvidence:
    """保存一个叶从原始穿刺来源到独立文档 E2E 的完整证据。"""

    schema_version: str
    route: str
    source_path: str
    source_hash: str
    original_state: str
    target_toolbox_key: str
    target_version: str
    allowed_changes: tuple[str, ...]
    migration_differences: tuple[str, ...]
    fixture_refs: tuple[str, ...]
    gold_refs: tuple[str, ...]
    threshold_refs: tuple[str, ...]
    fallback: str
    limitations: tuple[str, ...]
    owner: str
    contract_passed: bool
    equivalence_passed: bool
    blind_passed: bool
    anti_overfit_passed: bool
    failure_passed: bool
    document_e2e_passed: bool
    fallback_has_page_outcome: bool
    fallback_has_complete_pdf: bool
    new_evidence: bool
    code_hash: str
    schema_hash: str
    catalog_hash: str
    font_hash: str
    threshold_hash: str
    evidence_hash: str

    def __post_init__(self) -> None:
        """校验必填字符串、集合、全部组件哈希和证据内容哈希。"""

        if self.schema_version != "transflow.leaf-migration-evidence/v1":
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "叶证据 schema_version 无效")
        for field_name in (
            "route",
            "source_path",
            "original_state",
            "target_toolbox_key",
            "target_version",
            "fallback",
            "owner",
        ):
            require_non_empty(getattr(self, field_name), field_name)
        require_sha256(self.source_hash, "source_hash")
        require_sha256(self.evidence_hash, "evidence_hash")
        for field_name in COMPONENT_HASH_FIELDS:
            require_sha256(getattr(self, field_name), field_name)
        if not self.allowed_changes or not self.fixture_refs or not self.threshold_refs:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "叶证据缺少改造、fixture 或阈值")
        if self.evidence_hash != compute_leaf_evidence_hash(self):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "叶 evidence_hash 不匹配")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LeafMigrationEvidence:
        """从 JSON 对象恢复证据，并把数组冻结为 tuple。"""

        converted = dict(payload)
        tuple_fields = (
            "allowed_changes",
            "migration_differences",
            "fixture_refs",
            "gold_refs",
            "threshold_refs",
            "limitations",
        )
        for field_name in tuple_fields:
            converted[field_name] = tuple(converted[field_name])
        if not converted.get("evidence_hash"):
            hash_payload = {
                key: value for key, value in converted.items() if key != "evidence_hash"
            }
            converted["evidence_hash"] = hashlib.sha256(
                canonical_json_bytes(hash_payload)
            ).hexdigest()
        return cls(**converted)


def _evidence_payload(evidence: LeafMigrationEvidence) -> dict[str, Any]:
    """提取除自引用 evidence_hash 外的全部证据字段。"""

    return {
        field.name: getattr(evidence, field.name)
        for field in fields(evidence)
        if field.name != "evidence_hash"
    }


def compute_leaf_evidence_hash(evidence: LeafMigrationEvidence) -> str:
    """计算叶证据规范内容哈希，作为本地可复算的签名式证明。"""

    return hashlib.sha256(canonical_json_bytes(_evidence_payload(evidence))).hexdigest()


def compute_invalidation_hash(evidence: LeafMigrationEvidence) -> str:
    """组合代码、Schema、Catalog、字体和阈值哈希，检测证据过期。"""

    payload = {field_name: getattr(evidence, field_name) for field_name in COMPONENT_HASH_FIELDS}
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class LeafGateAttestation:
    """记录叶结论、版本、证据和失效指纹的不可变本地证明。"""

    route: str
    target_version: str
    conclusion: LeafGateConclusion
    evidence_hash: str
    invalidation_hash: str
    attestation_hash: str

    def __post_init__(self) -> None:
        """校验三个哈希并复算证明内容。"""

        for field_name in ("evidence_hash", "invalidation_hash", "attestation_hash"):
            require_sha256(getattr(self, field_name), field_name)
        payload = {
            "route": self.route,
            "target_version": self.target_version,
            "conclusion": self.conclusion,
            "evidence_hash": self.evidence_hash,
            "invalidation_hash": self.invalidation_hash,
        }
        expected = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        if expected != self.attestation_hash:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "叶 attestation_hash 不匹配")


@dataclass(frozen=True, slots=True)
class LeafGateBatchResult:
    """聚合逐叶结论，任何 FAIL 都使阶段不可发布。"""

    attestations: tuple[LeafGateAttestation, ...]
    stage_passed: bool


class LeafGateEvaluator:
    """逐叶独立执行硬 Gate，不允许其他叶成功掩盖单叶失败。"""

    def evaluate(self, evidence: LeafMigrationEvidence) -> LeafGateAttestation:
        """按全部硬证据和 fallback 完整性返回唯一三态结论。"""

        LOGGER.info("调用叶级 Gate，意图=独立评估迁移证据 route=%s", evidence.route)
        hard_checks = (
            evidence.contract_passed,
            evidence.equivalence_passed,
            evidence.blind_passed,
            evidence.anti_overfit_passed,
            evidence.failure_passed,
            evidence.document_e2e_passed,
        )
        reliable_fallback = (
            evidence.fallback_has_page_outcome and evidence.fallback_has_complete_pdf
        )
        original_requires_new_evidence = evidence.original_state in NON_PROMOTABLE_ORIGINAL_STATES
        promotable = all(hard_checks) and (
            evidence.new_evidence or not original_requires_new_evidence
        )
        if promotable:
            conclusion = LeafGateConclusion.PASS_ENABLE
        elif reliable_fallback:
            conclusion = LeafGateConclusion.PASS_DISABLED_WITH_FALLBACK
        else:
            conclusion = LeafGateConclusion.FAIL
        invalidation_hash = compute_invalidation_hash(evidence)
        payload = {
            "route": evidence.route,
            "target_version": evidence.target_version,
            "conclusion": conclusion,
            "evidence_hash": evidence.evidence_hash,
            "invalidation_hash": invalidation_hash,
        }
        return LeafGateAttestation(
            evidence.route,
            evidence.target_version,
            conclusion,
            evidence.evidence_hash,
            invalidation_hash,
            hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        )

    def evaluate_all(
        self,
        evidence_items: tuple[LeafMigrationEvidence, ...],
    ) -> LeafGateBatchResult:
        """逐叶计算并仅在没有 FAIL 时允许阶段继续。"""

        attestations = tuple(self.evaluate(item) for item in evidence_items)
        return LeafGateBatchResult(
            attestations,
            all(item.conclusion is not LeafGateConclusion.FAIL for item in attestations),
        )


def validate_catalog_publication(
    entry: ToolboxCatalogEntry,
    attestation: LeafGateAttestation,
) -> None:
    """拒绝 Catalog enabled、版本或证明哈希与叶结论不一致。"""

    LOGGER.info("调用 Catalog 发布校验，意图=阻止无证据启用 route=%s", entry.route)
    expected_enabled = attestation.conclusion is LeafGateConclusion.PASS_ENABLE
    if (
        entry.route != attestation.route
        or entry.toolbox_version != attestation.target_version
        or entry.enabled != expected_enabled
        or entry.evidence_state != attestation.conclusion.value
        or entry.evidence_attestation_hash != attestation.attestation_hash
    ):
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Catalog 与叶 Gate 证明不一致")


def evidence_is_current(
    attestation: LeafGateAttestation,
    component_hashes: dict[str, str],
) -> bool:
    """比较当前五类组件哈希，任一变化都使旧证据过期。"""

    if set(component_hashes) != set(COMPONENT_HASH_FIELDS):
        raise ValueError("component_hashes 必须完整提供五类组件")
    for field_name, value in component_hashes.items():
        require_sha256(value, field_name)
    current = hashlib.sha256(canonical_json_bytes(component_hashes)).hexdigest()
    return current == attestation.invalidation_hash


def main() -> int:
    """记录叶 Gate 只允许三态结论且 enabled 必须有匹配证明。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("LeafGateEvaluator 示例，意图=禁止无证据启用与过期证据复用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
