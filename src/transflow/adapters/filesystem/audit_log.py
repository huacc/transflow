"""实现字段完整、受控截断且不泄漏秘密的 JSONL 审计日志。"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from transflow.adapters.filesystem.common import ensure_within
from transflow.domain.errors import DomainContractError, ErrorCode

LOGGER = logging.getLogger("transflow.adapters.filesystem.audit_log")
FILESYSTEM_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FIELDS = frozenset(
    {
        "artifact_ref",
        "attempt",
        "classification_path",
        "duration_ms",
        "error_code",
        "fallback",
        "job_id",
        "outcome",
        "page_no",
        "run_id",
        "service",
        "stage",
        "state",
        "toolbox_key_version",
        "unit_or_region_id",
    }
)
SECRET_KEY_PATTERN = re.compile(
    r"api[_-]?key|token|secret|password|authorization|credential",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


class StructuredAuditLogger:
    """把受控结构化事件追加到一个 Run 私有 JSONL 文件。"""

    def __init__(
        self,
        run_root: Path,
        relative_path: str = "logs/audit.jsonl",
        limit: int = 512,
    ) -> None:
        """绑定 Run 根、日志相对路径和单字符串硬上限。"""

        if limit < 32:
            raise ValueError("日志字段上限过小")
        self._run_root = run_root.resolve()
        self._path = ensure_within(self._run_root / relative_path, self._run_root)
        self._limit = limit

    def _sanitize(self, key: str, value: Any) -> Any:
        """按字段名脱敏，并递归截断字符串或容器内容。"""

        if SECRET_KEY_PATTERN.search(key):
            return "[REDACTED]"
        if isinstance(value, str):
            sanitized = value
            for pattern in SECRET_VALUE_PATTERNS:
                sanitized = pattern.sub("[REDACTED]", sanitized)
            if len(sanitized) > self._limit:
                return f"{sanitized[: self._limit]}...[TRUNCATED]"
            return sanitized
        if isinstance(value, dict):
            return {
                str(item_key): self._sanitize(str(item_key), item)
                for item_key, item in value.items()
                if not SECRET_KEY_PATTERN.search(str(item_key))
            }
        if isinstance(value, list | tuple):
            return [self._sanitize(key, item) for item in value]
        return value

    def write(self, event: dict[str, Any]) -> dict[str, Any]:
        """验证必填字段、脱敏后真实追加一条 JSONL 事件并 fsync。"""

        missing = sorted(REQUIRED_FIELDS - set(event))
        if missing:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, f"日志缺少字段: {missing}")
        sanitized = {
            key: self._sanitize(key, value)
            for key, value in event.items()
            if not SECRET_KEY_PATTERN.search(key)
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        LOGGER.info("调用结构化日志写入，意图=保存可审计运行事件 stage=%s", event["stage"])
        with self._path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(sanitized, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return sanitized

    def read_events(self) -> tuple[dict[str, Any], ...]:
        """读取当前日志中的全部结构化事件。"""

        if not self._path.is_file():
            return ()
        return tuple(
            json.loads(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )


def main() -> int:
    """记录结构化日志必须绑定 Run 工作区的调用意图。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("StructuredAuditLogger 示例需提供已验证 run workspace 与完整事件字段")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
