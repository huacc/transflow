"""提供文件 Adapter 共用的受控路径、哈希和原子写入原语。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from transflow.domain.errors import DomainContractError, ErrorCode

LOGGER = logging.getLogger("transflow.adapters.filesystem.common")
ADAPTERS_ROOT = Path(__file__).resolve().parent.parent
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class InjectedCrash(RuntimeError):
    """表示只由故障注入测试触发的受控进程崩溃窗口。"""


def inject_crash(requested_point: str | None, current_point: str) -> None:
    """在请求的协议边界抛出受控异常，不伪造正常返回。"""

    if requested_point == current_point:
        LOGGER.warning("触发故障注入，意图=验证崩溃恢复 point=%s", current_point)
        raise InjectedCrash(current_point)


def require_safe_identifier(value: str, field_name: str) -> str:
    """拒绝可能引入目录分隔、父目录或平台特殊路径的身份。"""

    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        raise DomainContractError(ErrorCode.INVALID_IDENTITY, f"{field_name} 不是安全路径身份")
    return value


def ensure_within(path: Path, root: Path, *, must_exist: bool = False) -> Path:
    """解析真实路径并确保最终目标位于指定允许根内。"""

    resolved_root = root.resolve(strict=must_exist)
    try:
        resolved = path.resolve(strict=must_exist)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise DomainContractError(
            ErrorCode.PATH_OUTSIDE_ALLOWED_ROOT,
            "解析后路径越出允许根或不存在",
        ) from error
    return resolved


def sha256_bytes(content: bytes) -> str:
    """计算内存字节的 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256，避免把大 PDF 一次性载入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """执行同目录 partial 写入、flush、fsync 和原子替换。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.partial")
    LOGGER.info("调用原子文件写入，意图=避免半写权威文件 path=%s", path)
    with partial.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    if partial.stat().st_dev != path.parent.stat().st_dev:
        raise OSError("partial 与 final 不在同一文件系统")
    partial.replace(path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """以规范 UTF-8 JSON 原子替换权威 manifest。"""

    content = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    atomic_write_bytes(path, content)


def load_json(path: Path) -> dict[str, Any]:
    """读取 UTF-8 JSON 权威文件。"""

    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    """展示仓库 tmp 下的原子写入调用。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    repository_root = ADAPTERS_ROOT.parent.parent.parent
    target = repository_root / "tmp" / "p3-common-main" / "example.json"
    atomic_write_json(target, {"status": "ok"})
    LOGGER.info("文件原语示例完成 path=%s", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
