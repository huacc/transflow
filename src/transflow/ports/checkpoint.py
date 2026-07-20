"""声明版本化运行快照的 CheckpointPort。"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from transflow.domain.artifacts import CheckpointRecord

LOGGER = logging.getLogger("transflow.ports.checkpoint")


@runtime_checkable
class CheckpointPort(Protocol):
    """隔离 Checkpoint 持久化实现并保留乐观版本语义。"""

    def load(self, run_id: str) -> CheckpointRecord | None:
        """读取指定 Run 的最新快照；不存在时返回 ``None``。"""

        ...

    def save(self, record: CheckpointRecord, expected_version: int) -> CheckpointRecord:
        """按期望旧版本原子保存；重复同版本载荷必须幂等。"""

        ...


def main() -> int:
    """记录 CheckpointPort 的边界用途。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("调用 Port 示例，意图=说明 CheckpointPort 只承担版本化快照边界")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
