"""声明语义翻译专用的 TranslationPort。"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from transflow.domain.translation import TranslationBatch, TranslationBundle

LOGGER = logging.getLogger("transflow.ports.translation")


@runtime_checkable
class TranslationPort(Protocol):
    """只负责文本翻译，不承载分类、布局或质量判定。"""

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """按 batch_id 幂等翻译，并严格保持 unit_id 集合与顺序。"""

        ...


def main() -> int:
    """记录 TranslationPort 与模型判定边界分离的用途。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("调用 Port 示例，意图=说明 TranslationPort 只承担语义翻译")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
