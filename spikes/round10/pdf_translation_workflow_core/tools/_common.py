"""Shared helpers for the PDF translation workflow tools.

tool_name: _common
category: shared
input_contract: filesystem paths and JSON-compatible data
output_contract: normalized paths, JSON files, JSONL files
failure_signals: exceptions are allowed to fail fast for caller handling
fallback: caller records failure in operation log
anti_overfit_statement: this module does not branch on sample names, page numbers, text, or coordinates
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


TOOL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = TOOL_ROOT.parent


ASCII_RE = re.compile(r"[A-Za-z][A-Za-z0-9&()/_.'+-]*")


def now_local() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    lines = [json.dumps(item, ensure_ascii=False) for item in records]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def rel(path: Path, base: Path = WORKSPACE_ROOT) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)


def resolve_workspace_path(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return WORKSPACE_ROOT / path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ascii_tokens(text: str) -> list[str]:
    return sorted(set(ASCII_RE.findall(text)))


def median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2
