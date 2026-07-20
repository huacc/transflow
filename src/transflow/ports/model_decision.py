"""声明分类、策略和质量等结构化模型判定的 ModelDecisionPort。"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from transflow.domain.classification import ModelDecision, ModelDecisionRequest

LOGGER = logging.getLogger("transflow.ports.model_decision")


@runtime_checkable
class ModelDecisionPort(Protocol):
    """只负责 Schema 约束的非翻译类模型判定。"""

    def decide(self, request: ModelDecisionRequest) -> ModelDecision:
        """按 decision_id 幂等判定并返回结构化结果。"""

        ...


def main() -> int:
    """记录 ModelDecisionPort 与语义翻译边界分离的用途。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("调用 Port 示例，意图=说明 ModelDecisionPort 只承担结构化判定")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
