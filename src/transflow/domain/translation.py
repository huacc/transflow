"""定义翻译单元、批次和严格对齐的翻译结果合同。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from transflow.domain.common import require_non_empty, require_unique
from transflow.domain.errors import DomainContractError, ErrorCode

LOGGER = logging.getLogger("transflow.domain.translation")


@dataclass(frozen=True, slots=True)
class TranslationUnit:
    """表示具有稳定身份和页内顺序的最小翻译单元。"""

    unit_id: str
    page_no: int
    ordinal: int
    source_text: str
    region_id: str

    def __post_init__(self) -> None:
        """校验身份、页面、顺序和真实源文本。"""

        require_non_empty(self.unit_id, "unit_id")
        require_non_empty(self.source_text, "source_text")
        require_non_empty(self.region_id, "region_id")
        if self.page_no < 0 or self.ordinal < 0:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "翻译单元页面或顺序无效")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TranslationUnit:
        """从 JSON 字典恢复翻译单元。"""

        return cls(**payload)


@dataclass(frozen=True, slots=True)
class TranslationBatch:
    """表示发给 TranslationPort 的有序翻译单元批次。"""

    batch_id: str
    source_language: str
    target_language: str
    units: tuple[TranslationUnit, ...]

    def __post_init__(self) -> None:
        """校验批次身份、语言和单元身份/顺序唯一性。"""

        require_non_empty(self.batch_id, "batch_id")
        require_non_empty(self.source_language, "source_language")
        require_non_empty(self.target_language, "target_language")
        if not self.units:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "翻译批次不得为空")
        require_unique(tuple(unit.unit_id for unit in self.units), "units.unit_id")
        ordinals = tuple(unit.ordinal for unit in self.units)
        if ordinals != tuple(sorted(ordinals)) or len(set(ordinals)) != len(ordinals):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "翻译单元顺序必须严格递增")

    @property
    def ordered_unit_ids(self) -> tuple[str, ...]:
        """返回调用方要求原样保持的翻译单元身份顺序。"""

        return tuple(unit.unit_id for unit in self.units)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TranslationBatch:
        """从 JSON 字典恢复翻译批次，并保持单元顺序不变。"""

        return cls(
            batch_id=payload["batch_id"],
            source_language=payload["source_language"],
            target_language=payload["target_language"],
            units=tuple(TranslationUnit.from_dict(item) for item in payload["units"]),
        )


@dataclass(frozen=True, slots=True)
class TranslatedUnit:
    """表示与输入 unit_id 一一对应的真实翻译文本。"""

    unit_id: str
    translated_text: str

    def __post_init__(self) -> None:
        """拒绝空身份和空翻译结果。"""

        require_non_empty(self.unit_id, "unit_id")
        require_non_empty(self.translated_text, "translated_text")


@dataclass(frozen=True, slots=True)
class TranslationBundle:
    """表示严格保持请求身份集合和顺序的翻译返回包。"""

    batch_id: str
    requested_unit_ids: tuple[str, ...]
    units: tuple[TranslatedUnit, ...]

    def __post_init__(self) -> None:
        """拒绝缺失、重复、新增、改写或重新排序的 unit_id。"""

        require_non_empty(self.batch_id, "batch_id")
        require_unique(self.requested_unit_ids, "requested_unit_ids")
        returned_ids = tuple(unit.unit_id for unit in self.units)
        try:
            require_unique(returned_ids, "units.unit_id")
        except DomainContractError as error:
            raise DomainContractError(
                ErrorCode.INVALID_TRANSLATION_BUNDLE,
                error.detail,
            ) from error
        if not self.requested_unit_ids or returned_ids != self.requested_unit_ids:
            raise DomainContractError(
                ErrorCode.INVALID_TRANSLATION_BUNDLE,
                "返回 unit_id 必须与请求身份和顺序完全一致",
            )

    @classmethod
    def from_batch(
        cls,
        batch: TranslationBatch,
        units: tuple[TranslatedUnit, ...],
    ) -> TranslationBundle:
        """使用原始批次身份构造并校验翻译结果。"""

        LOGGER.info("调用翻译返回对齐，意图=阻止身份漂移 batch_id=%s", batch.batch_id)
        return cls(batch.batch_id, batch.ordered_unit_ids, units)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TranslationBundle:
        """从 JSON 字典恢复翻译返回包并重新验证身份对齐。"""

        return cls(
            batch_id=payload["batch_id"],
            requested_unit_ids=tuple(payload["requested_unit_ids"]),
            units=tuple(TranslatedUnit(**item) for item in payload["units"]),
        )


def main() -> int:
    """展示翻译批次与结果严格对齐的调用方式。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    unit = TranslationUnit("unit-1", 0, 0, "Hello", "region-1")
    batch = TranslationBatch("batch-1", "en", "zh-CN", (unit,))
    bundle = TranslationBundle.from_batch(batch, (TranslatedUnit("unit-1", "你好"),))
    LOGGER.info("翻译合同示例完成 batch_id=%s unit_count=%s", bundle.batch_id, len(bundle.units))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
