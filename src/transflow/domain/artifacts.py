"""定义 Artifact 和 Checkpoint 的纯领域数据合同。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from transflow.domain.common import require_non_empty, require_sha256
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.states import CheckpointCompatibility

LOGGER = logging.getLogger("transflow.domain.artifacts")


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    """表示待写入 ArtifactPort 的不可变二进制载荷。"""

    artifact_id: str
    media_type: str
    content: bytes
    content_hash: str

    def __post_init__(self) -> None:
        """校验产物身份、媒体类型、非空内容和声明哈希。"""

        require_non_empty(self.artifact_id, "artifact_id")
        require_non_empty(self.media_type, "media_type")
        require_sha256(self.content_hash, "content_hash")
        if not self.content:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Artifact 内容不得为空")


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """表示存储成功后返回的稳定 Artifact 引用。"""

    artifact_id: str
    media_type: str
    content_hash: str
    size_bytes: int
    relative_path: str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        """校验 Artifact 引用的身份、类型、哈希和大小。"""

        require_non_empty(self.artifact_id, "artifact_id")
        require_non_empty(self.media_type, "media_type")
        require_sha256(self.content_hash, "content_hash")
        if self.size_bytes <= 0:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Artifact 大小必须为正")
        if self.relative_path is not None:
            require_non_empty(self.relative_path, "relative_path")
        if self.label is not None:
            require_non_empty(self.label, "label")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ArtifactReference:
        """从 JSON 字典恢复 Artifact 引用。"""

        return cls(**payload)


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    """表示可由 CheckpointPort 原子提交的版本化运行快照。"""

    run_id: str
    version: int
    state_hash: str
    payload: bytes
    compatibility: CheckpointCompatibility
    artifact_refs: tuple[ArtifactReference, ...] = ()

    def __post_init__(self) -> None:
        """校验运行身份、正版本、状态哈希和非空快照内容。"""

        require_non_empty(self.run_id, "run_id")
        require_sha256(self.state_hash, "state_hash")
        if self.version < 1 or not self.payload:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "Checkpoint 版本或内容无效")
        artifact_ids = tuple(reference.artifact_id for reference in self.artifact_refs)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "Checkpoint Artifact 引用重复")


def main() -> int:
    """展示 Artifact 引用的最小构造方式。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    artifact = ArtifactReference("artifact-1", "application/pdf", "0" * 64, 1)
    LOGGER.info("调用 Artifact 示例，意图=展示稳定引用 artifact_id=%s", artifact.artifact_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
