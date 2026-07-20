"""提供可复现的 Fixed 与 Deterministic 测试翻译实现。"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from transflow.domain.errors import ErrorCode, PortCallError
from transflow.domain.translation import (
    TranslatedUnit,
    TranslationBatch,
    TranslationBundle,
)

LOGGER = logging.getLogger("transflow.adapters.ai.fixed")
ADAPTERS_ROOT = Path(__file__).resolve().parent.parent


class FixedTranslationAdapter:
    """按 unit_id 使用调用方提供的固定译文，适合纵向链路验收。"""

    def __init__(self, translations: dict[str, str]) -> None:
        """复制固定映射，防止调用方后续修改影响复现。"""

        self._translations = dict(translations)

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """严格按输入顺序返回固定译文，缺少任何 unit_id 即失败。"""

        LOGGER.info("调用固定翻译，意图=返回可复现译文 batch_id=%s", batch.batch_id)
        try:
            units = tuple(
                TranslatedUnit(unit.unit_id, self._translations[unit.unit_id])
                for unit in batch.units
            )
        except KeyError as error:
            raise PortCallError(
                ErrorCode.PORT_CONTRACT_VIOLATION,
                False,
                "固定译文缺少 unit_id",
            ) from error
        return TranslationBundle.from_batch(batch, units)


class DeterministicTranslationAdapter:
    """基于输入内容哈希生成跨运行稳定的非空测试译文。"""

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """逐单元计算稳定摘要，并保持输入 unit_id 与顺序。"""

        LOGGER.info("调用确定性翻译，意图=验证相同输入无结果差异 batch_id=%s", batch.batch_id)
        units = []
        for unit in batch.units:
            seed = "\0".join(
                (batch.source_language, batch.target_language, unit.unit_id, unit.source_text)
            ).encode("utf-8")
            digest = hashlib.sha256(seed).hexdigest()[:16]
            units.append(
                TranslatedUnit(
                    unit.unit_id,
                    f"[{batch.target_language}:{digest}] {unit.source_text}",
                )
            )
        return TranslationBundle.from_batch(batch, tuple(units))


def main() -> int:
    """展示确定性测试翻译的最小调用方式。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("Fixed/Deterministic Adapter 仅用于无真实 Provider 的链路验收")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
