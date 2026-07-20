"""声明二进制产物写入和读取的 ArtifactPort。"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from transflow.domain.artifacts import ArtifactPayload, ArtifactReference

LOGGER = logging.getLogger("transflow.ports.artifact")


@runtime_checkable
class ArtifactPort(Protocol):
    """隔离本地目录或对象存储实现，不向应用层泄漏存储细节。"""

    def put(self, payload: ArtifactPayload) -> ArtifactReference:
        """按 artifact_id 与 content_hash 幂等写入一个不可变产物。"""

        ...

    def get(self, artifact_id: str) -> bytes:
        """读取一个已存在产物；失败时实现应返回稳定 PortCallError。"""

        ...


def main() -> int:
    """记录 ArtifactPort 的边界用途。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("调用 Port 示例，意图=说明 ArtifactPort 只承担不可变产物边界")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
