"""检查 P9 新增面是否持久化连接秘密，并只报告命中文件名。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path

LOGGER = logging.getLogger("transflow.scripts.verify_p9_secrets")
REPO_ROOT = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = frozenset({".json", ".md", ".py", ".toml", ".txt", ".yaml", ".yml"})
P9_SCOPE = (
    REPO_ROOT / "src" / "transflow" / "toolboxes" / "leaves" / "ordinary.py",
    REPO_ROOT / "src" / "transflow" / "toolboxes" / "leaves" / "ordinary_policy.py",
    REPO_ROOT / "src" / "transflow" / "application" / "toolbox_page_coordinator.py",
    REPO_ROOT / "src" / "transflow" / "pdf_kernel" / "facts.py",
    REPO_ROOT / "scripts" / "build_p9_release.py",
    REPO_ROOT / "scripts" / "run_p9_acceptance.py",
    REPO_ROOT / "scripts" / "run_p9_real_samples.py",
    REPO_ROOT / "scripts" / "verify_p9.py",
    REPO_ROOT / "scripts" / "verify_p9_real_samples.py",
    REPO_ROOT / "scripts" / "verify_p9_secrets.py",
    REPO_ROOT / "scripts" / "write_p9_report.py",
    REPO_ROOT / "tests" / "test_p9.py",
    REPO_ROOT / "tests" / "migration" / "p9_qwen_translation_adapter.py",
    REPO_ROOT / "tests" / "migration" / "test_p9_real_samples.py",
    REPO_ROOT / "resources" / "evidence" / "p9",
    REPO_ROOT / "resources" / "manifests",
    REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v4.json",
    REPO_ROOT / "config" / "transflow.example.toml",
    REPO_ROOT / "docs" / "reports",
)
WORKSPACE_TEXT_ROOTS = tuple(REPO_ROOT / name for name in ("config", "docs", "resources", "src"))
SENSITIVE_PATTERNS = (
    re.compile(
        r"(?i)(?:api[_ -]?key|token)\s*[：:]\s*"
        r"(?!<|\$|env|TRANSFLOW_)[^\s'\"\]}]{16,}"
    ),
    re.compile(
        r"(?i)(?:api[_ -]?key|token)\s*=\s*['\"]"
        r"(?!<|\$|env|TRANSFLOW_)[^'\"]{16,}['\"]"
    ),
    re.compile(r"(?i)base\s*url\s*[：:=]\s*https?://(?:\d{1,3}\.){3}\d{1,3}:\d+"),
    re.compile(r"模型名\s*[：:=]\s*[^\s]+"),
)


def _iter_text_files(path: Path) -> Iterator[Path]:
    """枚举一个文件或目录中的受支持文本文件。"""

    if path.is_file():
        if path.suffix.lower() in TEXT_SUFFIXES:
            yield path
        return
    if path.is_dir():
        yield from (
            item
            for item in path.rglob("*")
            if item.is_file() and item.suffix.lower() in TEXT_SUFFIXES
        )


def _hits(paths: tuple[Path, ...]) -> set[Path]:
    """扫描文本但只返回命中文件，绝不打印匹配内容。"""

    hits: set[Path] = set()
    for root in paths:
        for path in _iter_text_files(root):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError, UnicodeError:
                continue
            if any(pattern.search(text) for pattern in SENSITIVE_PATTERNS):
                hits.add(path.resolve())
    return hits


def verify() -> dict[str, object]:
    """区分 P9 新增面和工作区其他文件的秘密风险。"""

    LOGGER.info("调用 P9 秘密扫描，意图=阻止连接参数进入新增代码、配置、报告和证据")
    scoped_hits = _hits(P9_SCOPE)
    workspace_hits = _hits(WORKSPACE_TEXT_ROOTS)
    external_hits = workspace_hits - scoped_hits
    return {
        "external_workspace_hit_files": sorted(
            path.relative_to(REPO_ROOT).as_posix() for path in external_hits
        ),
        "p9_scope_hit_files": sorted(
            path.relative_to(REPO_ROOT).as_posix() for path in scoped_hits
        ),
        "status": "PASS_WITH_EXTERNAL_WARNING" if external_hits else "PASS",
    }


def main() -> int:
    """打印不含秘密正文的机器可读结果，并以 P9 范围命中决定退出码。"""

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    result = verify()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not result["p9_scope_hit_files"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
