from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import CADENCE_VERSION, SUPPORTED_TOOLBOX_KEYS
from .models import CadenceError


@dataclass(frozen=True)
class ScaffoldResult:
    toolbox_key: str
    package_root: str
    dry_run: bool
    planned_files: tuple[str, ...]


TEMPLATE_FILES = {
    "README.md": "README.template.md",
    "docs/分类边界与不变量.md": "分类边界与不变量.template.md",
    "docs/工具分类与调用流程.md": "工具分类与调用流程.template.md",
    "docs/裁决与修复规则.md": "裁决与修复规则.template.md",
    "stage_gate.json": "stage_gate.template.json",
}

EMPTY_FILES = (
    "samples/manifest.jsonl",
    "samples/development/.gitkeep",
    "samples/regression/.gitkeep",
    "samples/holdout/.gitkeep",
    "tools/.gitkeep",
    "tests/.gitkeep",
    "runs/.gitkeep",
    "reports/.gitkeep",
)


def validate_toolbox_key(toolbox_key: str) -> None:
    if toolbox_key not in SUPPORTED_TOOLBOX_KEYS:
        raise CadenceError(f"unsupported_toolbox_key:{toolbox_key}")


def package_path(project_root: Path, toolbox_key: str) -> Path:
    validate_toolbox_key(toolbox_key)
    return project_root / "toolboxes" / Path(*toolbox_key.split("."))


def scaffold_toolbox(project_root: Path, toolbox_key: str, *, dry_run: bool = False) -> ScaffoldResult:
    package_root = package_path(project_root, toolbox_key)
    planned = tuple(sorted(tuple(TEMPLATE_FILES) + EMPTY_FILES + ("toolbox_manifest.json",)))
    if dry_run:
        return ScaffoldResult(toolbox_key, package_root.relative_to(project_root).as_posix(), True, planned)
    if package_root.exists() and any(package_root.iterdir()):
        raise CadenceError(f"toolbox_package_already_exists:{toolbox_key}")

    template_root = project_root / "templates" / "toolbox"
    for relative, template_name in TEMPLATE_FILES.items():
        target = package_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        text = (template_root / template_name).read_text(encoding="utf-8").replace("{{TOOLBOX_KEY}}", toolbox_key)
        target.write_text(text, encoding="utf-8")
    for relative in EMPTY_FILES:
        target = package_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    manifest = {
        "schema_version": "toolbox-package/v1",
        "cadence_version": CADENCE_VERSION,
        "toolbox_key": toolbox_key,
        "maturity": "EXPERIMENTAL",
        "workflow_frozen": False,
        "promotion_manifest_present": False,
    }
    (package_root / "toolbox_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return ScaffoldResult(toolbox_key, package_root.relative_to(project_root).as_posix(), False, planned)

