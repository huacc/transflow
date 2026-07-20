"""定义页面区域、Toolbox、Patch、发现项和裁决合同。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from transflow.domain.common import require_non_empty, require_sha256, require_unique
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.pages import PageExecutionContext

LOGGER = logging.getLogger("transflow.domain.toolbox")


@dataclass(frozen=True, slots=True)
class Region:
    """表示由唯一所有者管理且绑定页面几何的矩形区域。"""

    region_id: str
    page_no: int
    x0: float
    y0: float
    x1: float
    y1: float
    owner: str

    def __post_init__(self) -> None:
        """校验区域身份、页面、几何边界和所有者。"""

        require_non_empty(self.region_id, "region_id")
        require_non_empty(self.owner, "owner")
        invalid_geometry = (
            self.page_no < 0
            or self.x0 < 0
            or self.y0 < 0
            or self.x1 <= self.x0
            or self.y1 <= self.y0
        )
        if invalid_geometry:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "区域页面或几何无效")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Region:
        """从 JSON 字典恢复页面区域。"""

        return cls(**payload)


@dataclass(frozen=True, slots=True)
class ToolboxDescriptor:
    """表示可登记到 Catalog 的页面 Toolbox 合同元数据。"""

    toolbox_id: str
    route: str
    contract_version: str
    owner: str

    def __post_init__(self) -> None:
        """校验 Toolbox 的稳定身份、路由、版本和所有者。"""

        for field_name in ("toolbox_id", "route", "contract_version", "owner"):
            require_non_empty(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class PatchOperation:
    """表示 PagePatch 中与区域身份绑定的一个声明式修改。"""

    operation_id: str
    region_id: str
    kind: str
    payload_hash: str
    owner: str | None = None
    target_object_ids: tuple[str, ...] = ()
    rect: tuple[float, float, float, float] | None = None
    replacement_text: str | None = None
    font_id: str | None = None
    font_size: float | None = None

    def __post_init__(self) -> None:
        """校验操作身份、区域、类型和内容指纹。"""

        require_non_empty(self.operation_id, "operation_id")
        require_non_empty(self.region_id, "region_id")
        require_non_empty(self.kind, "kind")
        require_sha256(self.payload_hash, "payload_hash")
        if self.owner is not None:
            require_non_empty(self.owner, "owner")
        require_unique(self.target_object_ids, "target_object_ids")
        if self.rect is not None:
            x0, y0, x1, y1 = self.rect
            if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
                raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Patch 操作矩形无效")
        if self.replacement_text is not None and not self.replacement_text:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Patch 替换文本不得为空")
        if self.font_id is not None:
            require_non_empty(self.font_id, "font_id")
        if self.font_size is not None and self.font_size <= 0:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Patch 字号必须为正")


@dataclass(frozen=True, slots=True)
class PagePatch:
    """表示只能应用到指定源、页面、几何和所有者的页面补丁。"""

    patch_id: str
    source_hash: str
    page_no: int
    geometry_hash: str
    owner: str
    operations: tuple[PatchOperation, ...]

    def __post_init__(self) -> None:
        """校验 Patch 自身的稳定身份和操作唯一性。"""

        require_non_empty(self.patch_id, "patch_id")
        require_sha256(self.source_hash, "source_hash")
        require_sha256(self.geometry_hash, "geometry_hash")
        require_non_empty(self.owner, "owner")
        if self.page_no < 0 or not self.operations:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Patch 页面或操作无效")
        operation_ids = tuple(item.operation_id for item in self.operations)
        require_unique(operation_ids, "operations.operation_id")

    def validate_binding(self, context: PageExecutionContext, expected_owner: str) -> None:
        """在应用前核对源、页码、几何和所有者四个不可绕过的绑定。"""

        LOGGER.info("调用 Patch 绑定校验，意图=阻止跨页或跨所有者应用 patch_id=%s", self.patch_id)
        expected = (
            context.source_hash,
            context.page_no,
            context.geometry_hash,
            require_non_empty(expected_owner, "expected_owner"),
        )
        actual = (self.source_hash, self.page_no, self.geometry_hash, self.owner)
        if actual != expected:
            raise DomainContractError(
                ErrorCode.PATCH_BINDING_MISMATCH,
                "Patch 绑定与页面上下文不一致",
            )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PagePatch:
        """从 JSON 字典恢复页面补丁。"""

        return cls(
            patch_id=payload["patch_id"],
            source_hash=payload["source_hash"],
            page_no=payload["page_no"],
            geometry_hash=payload["geometry_hash"],
            owner=payload["owner"],
            operations=tuple(
                PatchOperation(
                    operation_id=item["operation_id"],
                    region_id=item["region_id"],
                    kind=item["kind"],
                    payload_hash=item["payload_hash"],
                    owner=item.get("owner"),
                    target_object_ids=tuple(item.get("target_object_ids", ())),
                    rect=tuple(item["rect"]) if item.get("rect") is not None else None,
                    replacement_text=item.get("replacement_text"),
                    font_id=item.get("font_id"),
                    font_size=item.get("font_size"),
                )
                for item in payload["operations"]
            ),
        )


class DecisionDisposition(StrEnum):
    """表示发现项被接受、要求修复或触发降级。"""

    ACCEPT = "ACCEPT"
    REPAIR = "REPAIR"
    FALLBACK = "FALLBACK"


@dataclass(frozen=True, slots=True)
class Finding:
    """表示质量检查发现的可追溯结构化问题。"""

    finding_id: str
    code: str
    severity: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        """校验发现项身份、错误码、级别和证据唯一性。"""

        require_non_empty(self.finding_id, "finding_id")
        require_non_empty(self.code, "code")
        require_non_empty(self.severity, "severity")
        require_unique(self.evidence_ids, "evidence_ids")


@dataclass(frozen=True, slots=True)
class Decision:
    """表示对一组发现项作出的确定性处置。"""

    decision_id: str
    disposition: DecisionDisposition
    finding_ids: tuple[str, ...]
    reason_code: str

    def __post_init__(self) -> None:
        """校验裁决身份、发现项集合和稳定原因码。"""

        require_non_empty(self.decision_id, "decision_id")
        require_non_empty(self.reason_code, "reason_code")
        require_unique(self.finding_ids, "finding_ids")


def main() -> int:
    """展示区域和页面补丁的最小构造方式。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    region = Region("region-1", 0, 0, 0, 100, 100, "body.table")
    LOGGER.info(
        "调用区域示例，意图=展示唯一所有权 region_id=%s owner=%s",
        region.region_id,
        region.owner,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
