"""提供纯领域合同共用的确定性序列化和基础校验。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from transflow.domain.errors import DomainContractError, ErrorCode

LOGGER = logging.getLogger("transflow.domain.common")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def require_non_empty(value: object, field_name: str) -> str:
    """校验字符串字段非空并返回去除首尾空白后的值。"""

    if not isinstance(value, str) or not value.strip():
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, f"{field_name} 必须是非空字符串")
    return value.strip()


def require_sha256(value: object, field_name: str) -> str:
    """校验字段是小写十六进制 SHA-256。"""

    normalized = require_non_empty(value, field_name)
    if not SHA256_PATTERN.fullmatch(normalized):
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, f"{field_name} 必须是 SHA-256")
    return normalized


def require_unique(values: tuple[str, ...], field_name: str) -> None:
    """校验有序身份列表不包含空值或重复项。"""

    if any(not isinstance(value, str) or not value for value in values):
        raise DomainContractError(ErrorCode.INVALID_IDENTITY, f"{field_name} 含空身份")
    if len(set(values)) != len(values):
        raise DomainContractError(ErrorCode.INVALID_IDENTITY, f"{field_name} 含重复身份")


def json_ready(value: Any) -> Any:
    """递归把 dataclass、Enum 和 tuple 转换为稳定 JSON 值。"""

    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: json_ready(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple | list):
        return [json_ready(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """以键排序、无多余空白的 UTF-8 JSON 形成跨进程稳定字节。"""

    return json.dumps(
        json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def content_sha256(value: Any) -> str:
    """计算领域值规范 JSON 的 SHA-256 指纹。"""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def main() -> int:
    """展示规范 JSON 与稳定哈希的调用方式。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    example = {"b": 2, "a": 1}
    LOGGER.info("调用领域哈希，意图=展示稳定序列化 sha256=%s", content_sha256(example))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
