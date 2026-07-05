from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".yaml", ".yml"}
SKIP_DIRS = {"__pycache__", ".git"}

SUSPECT_TOKENS = [
    "AIA_2020",
    "R2_AIA",
    "01_source",
    "pages_08_09_24_25",
    "1921",
    "1931",
    "VONB",
    "ANP",
    "Hong Kong",
    "Mainland China",
    "Tata AIA",
    "\u8d22\u52a1\u53ca\u8425\u8fd0\u56de\u987e",
    "\u4f01\u4e1a\u7ba1\u6cbb",
    "\u8d22\u52a1\u62a5\u8868",
]

BLOCKING_DIRS = {"tools", "contracts", "prompts"}
EVIDENCE_DIRS = {"regression"}


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("/", "\\")
    except ValueError:
        return str(path).replace("/", "\\")


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES


def iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and is_text_file(path):
            files.append(path)
    return sorted(files)


def classify_hit(path: Path, root: Path) -> str:
    if path.name == "scan_core_overfit.py":
        return "scanner_dictionary"
    parts = set(path.relative_to(root).parts)
    if parts & EVIDENCE_DIRS:
        return "evidence_only"
    if parts & BLOCKING_DIRS:
        return "blocking"
    return "warning"


def scan(root: Path) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    for path in iter_files(root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line_number, line in enumerate(lines, 1):
            for token in SUSPECT_TOKENS:
                if token in line:
                    hits.append(
                        {
                            "path": rel(path, root),
                            "line": line_number,
                            "token": token,
                            "classification": classify_hit(path, root),
                            "text": line.strip()[:240],
                        }
                    )
    blocking = [hit for hit in hits if hit["classification"] == "blocking"]
    return {
        "tool": "scan_core_overfit",
        "root": str(root),
        "verdict": "FAIL" if blocking else "PASS",
        "blocking_hit_count": len(blocking),
        "warning_hit_count": sum(1 for hit in hits if hit["classification"] == "warning"),
        "evidence_only_hit_count": sum(1 for hit in hits if hit["classification"] == "evidence_only"),
        "scanner_dictionary_hit_count": sum(1 for hit in hits if hit["classification"] == "scanner_dictionary"),
        "blocking_hits": blocking,
        "all_hits": hits,
        "policy": {
            "blocking_dirs": sorted(BLOCKING_DIRS),
            "evidence_dirs": sorted(EVIDENCE_DIRS),
            "rule": "Sample-specific tokens are allowed only in regression evidence. Hits in tools/contracts/prompts fail anti-overfit validation.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="pdf_translation_workflow_core")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    result = scan(root)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result["verdict"])
    print(out)
    return 1 if result["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
