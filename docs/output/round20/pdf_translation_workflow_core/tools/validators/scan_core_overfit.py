from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".yaml", ".yml"}
SKIP_DIRS = {"__pycache__", ".git"}

BLOCKING_DIRS = {"tools", "contracts", "prompts", "profiles"}


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


def load_tokens(token_file: Path | None, inline_tokens: list[str]) -> list[str]:
    tokens: list[str] = []
    if token_file:
        raw = json.loads(token_file.read_text(encoding="utf-8-sig"))
        if isinstance(raw, list):
            tokens.extend(str(item) for item in raw)
        elif isinstance(raw, dict) and isinstance(raw.get("tokens"), list):
            tokens.extend(str(item) for item in raw["tokens"])
        else:
            raise ValueError("token file must be a JSON list or an object with a tokens list")
    tokens.extend(inline_tokens)
    seen: set[str] = set()
    deduped: list[str] = []
    for token in tokens:
        normalized = token.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def classify_hit(path: Path, root: Path) -> str:
    parts = set(path.relative_to(root).parts)
    if parts & BLOCKING_DIRS:
        return "blocking"
    return "warning"


def scan(root: Path, tokens: list[str]) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    for path in iter_files(root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line_number, line in enumerate(lines, 1):
            for token in tokens:
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
    profile_hits = scan_profile_absolute_font_profiles(root)
    hits.extend(profile_hits)
    blocking = [hit for hit in hits if hit["classification"] == "blocking"]
    return {
        "tool": "scan_core_overfit",
        "root": str(root),
        "verdict": "FAIL" if blocking else "PASS",
        "token_count": len(tokens),
        "blocking_hit_count": len(blocking),
        "warning_hit_count": sum(1 for hit in hits if hit["classification"] == "warning"),
        "blocking_hits": blocking,
        "all_hits": hits,
        "policy": {
            "blocking_dirs": sorted(BLOCKING_DIRS),
            "rule": "Sample-specific tokens must be supplied from a run-local token file outside the core. Hits in tools/contracts/prompts/profiles fail anti-overfit validation. Reusable language profile role font profiles must use source-relative ratios/page quantiles, not sample-derived fixed min_pt/max_pt/min_insert_pt keys.",
        },
    }


def scan_profile_absolute_font_profiles(root: Path) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    profile_dir = root / "profiles"
    if not profile_dir.exists():
        return hits
    for path in sorted(profile_dir.glob("*.layout_profile.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            hits.append(
                {
                    "path": rel(path, root),
                    "line": 1,
                    "token": "invalid_profile_json",
                    "classification": "blocking",
                    "text": f"profile JSON could not be parsed: {exc}",
                }
            )
            continue
        font_profiles = data.get("policy_overrides", {}).get("font_profiles", {})
        if not isinstance(font_profiles, dict):
            continue
        for role, profile in font_profiles.items():
            if not isinstance(profile, dict):
                continue
            absolute_keys = [key for key in ["min_pt", "max_pt", "min_insert_pt"] if key in profile]
            if not absolute_keys:
                continue
            hits.append(
                {
                    "path": rel(path, root),
                    "line": 1,
                    "token": "absolute_reusable_font_profile",
                    "classification": "blocking",
                    "text": f"font_profiles.{role} contains fixed point-size keys {absolute_keys}; use source_relative ratios and current-page quantiles in reusable profiles",
                }
            )
    return hits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="pdf_translation_workflow_core")
    parser.add_argument("--out", required=True)
    parser.add_argument("--token-file", help="JSON list, or object with tokens list, stored outside the core directory")
    parser.add_argument("--token", action="append", default=[], help="Additional sample-sensitive token to scan for")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    token_file = Path(args.token_file).resolve() if args.token_file else None
    if token_file and root in token_file.parents:
        raise ValueError("token-file must live outside the scanned core directory")
    tokens = load_tokens(token_file, args.token)
    result = scan(root, tokens)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result["verdict"])
    print(out)
    return 1 if result["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
