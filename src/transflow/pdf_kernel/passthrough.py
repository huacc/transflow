"""实现源 PDF 整文按字节透传并校验原子发布副本。"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from transflow.domain.errors import ErrorCode, PortCallError
from transflow.pdf_kernel.workspace import require_under

LOGGER = logging.getLogger("transflow.pdf_kernel.passthrough")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _sha256_file(path: Path) -> str:
    """流式计算文件哈希，避免把大型年报整体加载到内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class PassthroughEvidence:
    """记录透传前后字节哈希、页数和发布目标。"""

    source_hash: str
    target_hash: str
    page_count: int
    target_path: Path


def publish_source_passthrough(
    source_path: Path,
    target_path: Path,
    allowed_target_root: Path,
) -> PassthroughEvidence:
    """把完整源文件复制到临时文件，校验后原子替换允许根内目标。"""

    if not source_path.is_file():
        raise PortCallError(ErrorCode.SOURCE_NOT_REGULAR_FILE, False, "透传源不是常规文件")
    target = require_under(target_path, allowed_target_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = require_under(target.with_suffix(target.suffix + ".partial"), allowed_target_root)
    LOGGER.info("调用整文透传，意图=按字节复制源 PDF target=%s", target.name)
    shutil.copyfile(source_path, partial)
    source_hash = _sha256_file(source_path)
    target_hash = _sha256_file(partial)
    if source_hash != target_hash:
        partial.unlink(missing_ok=True)
        raise PortCallError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, False, "透传副本哈希不一致")
    try:
        # 以显式 PDF 类型和内存字节验证，避免 Windows 上失败构造器占用 .partial 文件。
        with pymupdf.open("pdf", partial.read_bytes()) as document:
            page_count = document.page_count
    except Exception as error:
        partial.unlink(missing_ok=True)
        raise PortCallError(ErrorCode.SOURCE_NOT_READABLE, False, "透传副本无法发布") from error
    partial.replace(target)
    return PassthroughEvidence(source_hash, target_hash, page_count, target)


def main() -> int:
    """记录整文透传只接受显式源、目标和允许目标根。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("Passthrough 示例，意图=证明源与发布副本字节一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
