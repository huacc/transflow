"""生成 SharedPdfKernel、字体、Facts 与 Preservation 的统一恢复指纹。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from transflow.domain.common import content_sha256
from transflow.pdf_kernel.facts import FACTS_SCHEMA_VERSION
from transflow.pdf_kernel.fonts import ControlledFontRegistry
from transflow.pdf_kernel.patch import PATCH_MANIFEST_VERSION, RENDER_CONFIG_HASH
from transflow.pdf_kernel.preservation import load_support_matrix

LOGGER = logging.getLogger("transflow.pdf_kernel.fingerprint")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
KERNEL_VERSION = "transflow.shared-pdf-kernel/v1"


@dataclass(frozen=True, slots=True)
class KernelFingerprint:
    """冻结所有会改变事实、字体、Patch 或保真判断的版本和哈希。"""

    kernel_version: str
    facts_schema_version: str
    patch_manifest_version: str
    render_config_hash: str
    font_manifest_hash: str
    preservation_matrix_hash: str
    fingerprint: str


def build_kernel_fingerprint(
    font_manifest_path: Path,
    preservation_support_path: Path,
) -> KernelFingerprint:
    """从集中 manifest 生成跨进程稳定、可写入 Checkpoint 的内核指纹。"""

    repository_root = PACKAGE_ROOT.parent.parent
    fonts = ControlledFontRegistry(font_manifest_path, repository_root)
    matrix = load_support_matrix(preservation_support_path)
    payload = {
        "facts_schema_version": FACTS_SCHEMA_VERSION,
        "font_manifest_hash": fonts.manifest_hash,
        "kernel_version": KERNEL_VERSION,
        "patch_manifest_version": PATCH_MANIFEST_VERSION,
        "preservation_matrix_hash": matrix.matrix_hash,
        "render_config_hash": RENDER_CONFIG_HASH,
    }
    LOGGER.info("调用内核指纹，意图=拒绝资源漂移后的旧 Checkpoint")
    return KernelFingerprint(**payload, fingerprint=content_sha256(payload))


def main() -> int:
    """记录统一指纹必须由调用方注入两个集中 manifest 路径。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("KernelFingerprint 示例，意图=冻结恢复兼容边界")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
