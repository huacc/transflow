"""定义 Kernel 在 Toolbox 与 Provider 前冻结的页面文字清单。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from transflow.domain.common import content_sha256, require_sha256, require_unique
from transflow.domain.errors import DomainContractError, ErrorCode

LOGGER = logging.getLogger("transflow.domain.text_inventory")
DOMAIN_ROOT = Path(__file__).resolve().parent.parent


class InventoryDisposition(StrEnum):
    """表示 Provider 调用前已经批准的唯一文字处置。"""

    TRANSLATE = "TRANSLATE"
    KEEP_SOURCE = "KEEP_SOURCE"
    PROTECTED = "PROTECTED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class PageTextInventoryItem:
    """记录一个原生文字对象的稳定身份、内容哈希和预授权处置。"""

    object_id: str
    source_hash: str
    bbox: tuple[float, float, float, float]
    disposition: InventoryDisposition
    keep_source_reason: str | None = None
    disposition_reason: str | None = None

    def __post_init__(self) -> None:
        """拒绝空身份、坏哈希和未经理由批准的原文保留。"""

        if not self.object_id:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "文字对象身份不得为空")
        require_sha256(self.source_hash, "source_hash")
        if self.disposition is InventoryDisposition.KEEP_SOURCE and not self.keep_source_reason:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "KEEP_SOURCE 必须预先给出理由")
        if (
            self.disposition is not InventoryDisposition.KEEP_SOURCE
            and self.keep_source_reason is not None
        ):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "非 KEEP_SOURCE 不得携带保留理由")
        if self.disposition in {
            InventoryDisposition.PROTECTED,
            InventoryDisposition.UNSUPPORTED,
        }:
            if not self.disposition_reason:
                raise DomainContractError(
                    ErrorCode.INVALID_CONTRACT,
                    "PROTECTED/UNSUPPORTED 必须携带结构化原因",
                )
        elif self.disposition_reason is not None:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "TRANSLATE/KEEP_SOURCE 不得携带能力原因",
            )


@dataclass(frozen=True, slots=True)
class PageTextInventory:
    """表示一页全部原生文字在任何 Toolbox/Provider 调用前的内容寻址分母。"""

    page_no: int
    page_identity: str
    kernel_facts_hash: str
    items: tuple[PageTextInventoryItem, ...]

    def __post_init__(self) -> None:
        """校验页面、Kernel 指纹及对象身份唯一性。"""

        if self.page_no < 1:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "page_no 必须从 1 开始")
        require_sha256(self.page_identity, "page_identity")
        require_sha256(self.kernel_facts_hash, "kernel_facts_hash")
        require_unique(tuple(item.object_id for item in self.items), "items.object_id")

    @property
    def inventory_hash(self) -> str:
        """计算不含派生哈希字段的规范内容指纹。"""

        return content_sha256(self)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可跨进程恢复的严格 JSON 字典。"""

        return {
            "page_no": self.page_no,
            "page_identity": self.page_identity,
            "kernel_facts_hash": self.kernel_facts_hash,
            "inventory_hash": self.inventory_hash,
            "items": [
                {
                    "object_id": item.object_id,
                    "source_hash": item.source_hash,
                    "bbox": list(item.bbox),
                    "disposition": item.disposition.value,
                    "disposition_reason": item.disposition_reason,
                    "keep_source_reason": item.keep_source_reason,
                }
                for item in self.items
            ],
        }


def main() -> int:
    """记录该合同必须由 Kernel 构建，避免示例伪造文字事实。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("PageTextInventory 示例，意图=说明文字分母必须先于 Toolbox 冻结")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
