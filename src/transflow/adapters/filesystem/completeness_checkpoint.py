"""实现 run 私有、原子且可验证的翻译完整性安全点。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from transflow.adapters.filesystem.common import atomic_write_json, load_json
from transflow.domain.completeness import CompletenessCheckpoint
from transflow.domain.errors import DomainContractError, ErrorCode

LOGGER = logging.getLogger("transflow.adapters.filesystem.completeness_checkpoint")
FILESYSTEM_ROOT = Path(__file__).resolve().parent.parent


class FilesystemCompletenessCheckpointAdapter:
    """按页面保存 map/Bundle/Decision 聚合安全点，禁止覆盖不同内容。"""

    def __init__(self, run_root: Path) -> None:
        """绑定当前 run 根，不使用宿主机绝对配置。"""

        self._root = run_root.resolve() / "job" / "translation_completeness"
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, page_no: int) -> Path:
        """返回合法 1-based 页面的固定安全点路径。"""

        if page_no < 1:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "完整性安全点页码无效")
        return self._root / f"page-{page_no:04d}.json"

    def load(self, page_no: int) -> CompletenessCheckpoint | None:
        """读取并重新验证 map、Bundle、Decision 和聚合哈希。"""

        path = self._path(page_no)
        if not path.is_file():
            return None
        LOGGER.info("调用完整性安全点读取，意图=恢复已通过译文 page_no=%s", page_no)
        payload = load_json(path)
        return CompletenessCheckpoint.from_dict(payload)

    def commit(self, page_no: int, checkpoint: CompletenessCheckpoint) -> None:
        """首次原子提交；同内容幂等，不同内容拒绝覆盖。"""

        path = self._path(page_no)
        LOGGER.info("调用完整性安全点提交，意图=避免重复翻译 page_no=%s", page_no)
        if path.is_file():
            existing = CompletenessCheckpoint.from_dict(load_json(path))
            if existing.checkpoint_hash != checkpoint.checkpoint_hash:
                raise DomainContractError(
                    ErrorCode.CHECKPOINT_CONFLICT,
                    "完整性安全点已绑定不同内容",
                )
            return
        atomic_write_json(path, checkpoint.to_dict())
        # 写后从真实文件重新解析，避免把内存对象当作落盘成功证据。
        restored = CompletenessCheckpoint.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if restored.checkpoint_hash != checkpoint.checkpoint_hash:
            raise DomainContractError(
                ErrorCode.ARTIFACT_INTEGRITY_FAILED,
                "完整性安全点写后校验失败",
            )


def main() -> int:
    """记录完整性安全点只能绑定一个 run 私有目录。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("FilesystemCompletenessCheckpointAdapter 示例，意图=原子保存翻译安全点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
