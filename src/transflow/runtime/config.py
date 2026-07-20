"""集中读取并校验 Transflow 无秘密运行配置。"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

LOGGER = logging.getLogger("transflow.runtime.config")
# 所有仓库文件路径都从当前文件定位，不依赖调用进程的工作目录。
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE_ROOT.parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "transflow.example.toml"
CONFIG_ENVIRONMENT_VARIABLE = "TRANSFLOW_CONFIG"
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|client[_-]?secret|password|bearer\s+[a-z0-9._-]+)"
)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """保存 P1/P3 运行边界需要的、已经统一解析的无秘密配置。"""

    schema_version: str
    workspace: Path
    font_manifest: Path
    preservation_support_matrix: Path
    toolbox_catalog: Path
    margin_policy: Path
    leaf_evidence_schema: Path
    toolbox_policy: Path
    ordinary_leaf_policy: Path
    ai_capability_url: str
    log_level: str
    document_concurrency: int
    page_concurrency: int
    pdf_processes: int
    source_roots: tuple[Path, ...]
    ai_timeout_seconds: float
    ai_max_request_bytes: int


def resolve_repository_path(raw_path: str | Path, root: Path = REPO_ROOT) -> Path:
    """把相对路径解析为指定受控根内路径，并拒绝绝对路径和目录逃逸。"""

    candidate = Path(raw_path)
    if candidate.is_absolute():
        raise ValueError(f"配置路径必须相对仓库根: {raw_path}")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(f"配置路径越出仓库根: {raw_path}") from error
    return resolved


def _require_string(payload: dict[str, Any], key: str) -> str:
    """读取必填字符串，并对缺失或空值给出明确配置错误。"""

    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"配置项必须是非空字符串: {key}")
    return value.strip()


def _require_positive_integer(payload: dict[str, Any], key: str) -> int:
    """读取必填正整数并拒绝布尔值，避免并发占位被静默误读。"""

    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"配置项必须是正整数: {key}")
    return value


def _require_positive_number(payload: dict[str, Any], key: str) -> float:
    """读取必填正数并拒绝布尔值，供 HTTP 超时配置使用。"""

    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"配置项必须是正数: {key}")
    return float(value)


def _require_source_roots(payload: dict[str, Any], runtime_root: Path) -> tuple[Path, ...]:
    """读取至少一个仓库相对允许源目录并统一解析。"""

    values = payload.get("source_roots")
    if not isinstance(values, list) or not values:
        raise ValueError("source_roots 必须是非空字符串数组")
    roots: list[Path] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("source_roots 必须是非空字符串数组")
        roots.append(resolve_repository_path(value, runtime_root))
    return tuple(roots)


def _validate_http_url(raw_url: str) -> str:
    """校验内部 capability URL 只包含普通 HTTP 定位信息和零秘密。"""

    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("ai_capability_url 必须是有效的 HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("ai_capability_url 不得携带凭据、查询串或片段")
    if SECRET_PATTERN.search(raw_url):
        raise ValueError("ai_capability_url 疑似包含秘密")
    return raw_url


def load_runtime_config(config_path: Path | None = None) -> RuntimeConfig:
    """从唯一入口加载配置；环境变量仅定位文件，不承载或回写秘密。"""

    selected = config_path
    if selected is None:
        configured = os.environ.get(CONFIG_ENVIRONMENT_VARIABLE)
        selected = Path(configured).resolve() if configured else DEFAULT_CONFIG_PATH
    elif not selected.is_absolute():
        selected = resolve_repository_path(selected)
    LOGGER.info("调用配置读取，意图=构造健康探针运行参数 path=%s", selected)
    # 部署配置固定放在 <runtime-root>/config 下，因此可从配置文件自身定位部署根。
    runtime_root = selected.resolve().parent.parent
    payload = tomllib.loads(selected.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "transflow.runtime-config/v1":
        raise ValueError("运行配置 schema_version 不受支持")
    serialized = selected.read_text(encoding="utf-8")
    if SECRET_PATTERN.search(serialized):
        raise ValueError("运行配置疑似包含明文秘密")
    log_level = _require_string(payload, "log_level").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("log_level 不受支持")
    return RuntimeConfig(
        schema_version="transflow.runtime-config/v1",
        workspace=resolve_repository_path(_require_string(payload, "workspace"), runtime_root),
        font_manifest=resolve_repository_path(
            _require_string(payload, "font_manifest"), runtime_root
        ),
        preservation_support_matrix=resolve_repository_path(
            _require_string(payload, "preservation_support_matrix"), runtime_root
        ),
        toolbox_catalog=resolve_repository_path(
            _require_string(payload, "toolbox_catalog"), runtime_root
        ),
        margin_policy=resolve_repository_path(
            _require_string(payload, "margin_policy"), runtime_root
        ),
        leaf_evidence_schema=resolve_repository_path(
            _require_string(payload, "leaf_evidence_schema"), runtime_root
        ),
        toolbox_policy=resolve_repository_path(
            _require_string(payload, "toolbox_policy"), runtime_root
        ),
        ordinary_leaf_policy=resolve_repository_path(
            _require_string(payload, "ordinary_leaf_policy"), runtime_root
        ),
        ai_capability_url=_validate_http_url(_require_string(payload, "ai_capability_url")),
        log_level=log_level,
        document_concurrency=_require_positive_integer(payload, "document_concurrency"),
        page_concurrency=_require_positive_integer(payload, "page_concurrency"),
        pdf_processes=_require_positive_integer(payload, "pdf_processes"),
        source_roots=_require_source_roots(payload, runtime_root),
        ai_timeout_seconds=_require_positive_number(payload, "ai_timeout_seconds"),
        ai_max_request_bytes=_require_positive_integer(payload, "ai_max_request_bytes"),
    )


def find_plaintext_secrets(text: str) -> list[str]:
    """返回文本中的秘密类型命中，不回显实际秘密内容。"""

    return sorted({match.group(1).lower() for match in SECRET_PATTERN.finditer(text)})


def main() -> int:
    """演示集中读取模板配置，并只记录无秘密的配置摘要。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    config = load_runtime_config()
    LOGGER.info(
        "配置读取完成，意图=展示 P1 配置入口 workspace=%s pdf_processes=%s",
        config.workspace,
        config.pdf_processes,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
