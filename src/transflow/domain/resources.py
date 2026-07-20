"""定义运行资源指纹及其确定性组合规则。"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from transflow.domain.common import require_sha256

LOGGER = logging.getLogger("transflow.domain.resources")


def fingerprint_bytes(namespace: str, content: bytes) -> str:
    """使用域分隔和长度前缀计算单项资源的稳定 SHA-256。"""

    encoded_namespace = namespace.encode("utf-8")
    payload = (
        len(encoded_namespace).to_bytes(4, "big")
        + encoded_namespace
        + len(content).to_bytes(8, "big")
        + content
    )
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeResourceFingerprints:
    """冻结 Prompt、Schema、字体和 Toolbox Catalog 的独立及组合指纹。"""

    prompt_hash: str
    schema_hash: str
    font_hash: str
    toolbox_catalog_hash: str
    combined_hash: str

    def __post_init__(self) -> None:
        """校验每一个资源指纹都是精确 SHA-256。"""

        for field_name in (
            "prompt_hash",
            "schema_hash",
            "font_hash",
            "toolbox_catalog_hash",
            "combined_hash",
        ):
            require_sha256(getattr(self, field_name), field_name)


def build_runtime_fingerprints(
    prompt_bytes: bytes,
    schema_bytes: bytes,
    font_bytes: bytes,
    catalog_bytes: bytes,
) -> RuntimeResourceFingerprints:
    """计算四类资源指纹，并以固定字段顺序生成组合指纹。"""

    LOGGER.info("调用资源指纹计算，意图=冻结 Prompt/Schema/字体/Catalog 兼容边界")
    prompt_hash = fingerprint_bytes("prompt", prompt_bytes)
    schema_hash = fingerprint_bytes("schema", schema_bytes)
    font_hash = fingerprint_bytes("font", font_bytes)
    catalog_hash = fingerprint_bytes("toolbox_catalog", catalog_bytes)
    combined = fingerprint_bytes(
        "runtime_resources",
        "\n".join((prompt_hash, schema_hash, font_hash, catalog_hash)).encode("ascii"),
    )
    return RuntimeResourceFingerprints(
        prompt_hash,
        schema_hash,
        font_hash,
        catalog_hash,
        combined,
    )


def main() -> int:
    """展示四类运行资源组合指纹的计算方式。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    fingerprints = build_runtime_fingerprints(b"prompt", b"schema", b"font", b"catalog")
    LOGGER.info("资源指纹示例完成 combined_hash=%s", fingerprints.combined_hash)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
