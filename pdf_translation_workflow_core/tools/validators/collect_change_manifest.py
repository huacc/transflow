"""Collect file hashes and optional before/after deltas for round change auditing.

tool_name: collect_change_manifest
category: validators
input_contract: workspace root, output JSON path, optional baseline JSON
output_contract: JSON manifest of tracked files and optional delta
failure_signals: unreadable files, invalid baseline JSON
fallback: record manual file list in the round change log
anti_overfit_statement: records filesystem facts only; no sample-specific behavior
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_INCLUDE_DIRS = [
    "pdf_translation_workflow_core",
    "docs/业务流程",
    "docs/测试提示词",
    "README.md",
    "PACKAGE_MANIFEST.md",
]

DEFAULT_EXCLUDE_PARTS = {
    "__pycache__",
    ".git",
    "docs/reports",
    "docs/output",
}


def sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def norm(path: Path) -> str:
    return path.as_posix()


def is_excluded(rel_path: Path) -> bool:
    text = norm(rel_path)
    return any(text == part or text.startswith(part + "/") for part in DEFAULT_EXCLUDE_PARTS)


def iter_files(root: Path, includes: list[str]) -> list[Path]:
    files: list[Path] = []
    for item in includes:
        path = root / item
        if not path.exists():
            continue
        if path.is_file():
            rel_path = path.relative_to(root)
            if not is_excluded(rel_path):
                files.append(path)
            continue
        for candidate in path.rglob("*"):
            if not candidate.is_file():
                continue
            rel_path = candidate.relative_to(root)
            if is_excluded(rel_path):
                continue
            files.append(candidate)
    return sorted(set(files), key=lambda p: norm(p.relative_to(root)))


def collect(root: Path, includes: list[str]) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    for path in iter_files(root, includes):
        rel_path = norm(path.relative_to(root))
        stat = path.stat()
        entries[rel_path] = {
            "sha256": sha256_file(path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    return {
        "tool": "collect_change_manifest",
        "root": str(root),
        "tracked_file_count": len(entries),
        "files": entries,
    }


def build_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_files = before.get("files", {})
    after_files = after.get("files", {})
    before_keys = set(before_files)
    after_keys = set(after_files)
    added = sorted(after_keys - before_keys)
    removed = sorted(before_keys - after_keys)
    modified = sorted(
        key for key in before_keys & after_keys if before_files[key].get("sha256") != after_files[key].get("sha256")
    )
    unchanged = sorted(key for key in before_keys & after_keys if key not in modified)
    return {
        "tool": "collect_change_manifest",
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged_count": len(unchanged),
        "modification_count": len(added) + len(removed) + len(modified),
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--out", required=True)
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--delta-out", default=None)
    parser.add_argument("--include", action="append", default=None)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    includes = args.include or DEFAULT_INCLUDE_DIRS
    manifest = collect(root, includes)
    write_json(Path(args.out), manifest)

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        delta = build_delta(baseline, manifest)
        write_json(Path(args.delta_out) if args.delta_out else Path(args.out).with_name("change_manifest_delta.json"), delta)

    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
