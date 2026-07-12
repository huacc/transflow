from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable

from page_toolbox_puncture.contracts import write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from toolbox_cadence.models import SampleSplit, ToolboxSampleRecord


RUN_PACKAGE_VERSION = "p3-audit-run-package/v1"


def initialize_batch_package(
    *,
    run_root: Path,
    toolbox_root: Path,
    run_id: str,
    records: Iterable[ToolboxSampleRecord],
    prompt_path: Path,
    model: str,
) -> None:
    run_root.mkdir(parents=True, exist_ok=False)
    for name in ("contracts", "docs", "input/source_pdfs", "input/prompts", "cases", "output", "previews", "reports"):
        (run_root / name).mkdir(parents=True, exist_ok=True)
    selected = tuple(record for record in records if record.split is SampleSplit.DEVELOPMENT)
    shutil.copy2(toolbox_root / "samples" / "manifest.jsonl", run_root / "input" / "sample_manifest.jsonl")
    shutil.copy2(prompt_path, run_root / "input" / "prompts" / prompt_path.name)
    for record in selected:
        source = toolbox_root / record.source_ref
        target = run_root / "input" / "source_pdfs" / f"{record.sample_id}_source.pdf"
        shutil.copy2(source, target)
        if sha256_file(target) != record.sha256:
            raise RuntimeError(f"batch_source_snapshot_hash_mismatch:{record.sample_id}")

    (run_root / "README.md").write_text(
        "# P3 body.flow_text.single 运行包\n\n"
        "本目录是可独立核查的批次运行包。原文、候选、协议、过程报告和并排图均随运行保存。\n\n"
        "- `input/source_pdfs/`：批次原文快照；\n"
        "- `cases/<page_id>/`：逐页完整运行包；\n"
        "- `output/`：逐页候选 PDF 快捷副本；\n"
        "- `previews/`：逐页并排图快捷副本；\n"
        "- `reports/`：批次汇总和文件索引。\n",
        encoding="utf-8",
    )
    (run_root / "EXECUTION.md").write_text(
        "# 执行顺序\n\n"
        "```text\n"
        "原文快照 -> PageFacts -> PageTemplate -> 千问页级翻译 -> LayoutPlan\n"
        "-> Candidate PDF -> 机械裁决 -> 源候选并排图 -> 最终产品结论\n"
        "```\n\n"
        "过程成功与产品质量分别记录；每个页面必须能从 `input/source.pdf` 对应到 `output/candidate.pdf`。\n",
        encoding="utf-8",
    )
    shutil.copy2(toolbox_root / "docs" / "单列正文工具箱调度流程.md", run_root / "docs" / "单列正文工具箱调度流程.md")
    write_json(
        run_root / "contracts" / "batch_run_contract.json",
        {
            "schema_version": "batch-run-package/v1",
            "run_package_version": RUN_PACKAGE_VERSION,
            "run_id": run_id,
            "toolbox_key": "body.flow_text.single",
            "model": model,
            "split": "development",
            "page_ids": [record.sample_id for record in selected],
            "source_snapshot_pattern": "input/source_pdfs/<page_id>_source.pdf",
            "page_package_pattern": "cases/<page_id>",
            "candidate_pattern": "output/<page_id>_candidate.pdf",
            "comparison_pattern": "previews/<page_id>_comparison.png",
            "holdout_accessed": False,
        },
    )


def publish_case_outputs(*, run_root: Path, page_id: str, case_root: Path) -> None:
    candidate = case_root / "output" / "candidate.pdf"
    comparison = case_root / "previews" / "comparison.png"
    if candidate.is_file():
        shutil.copy2(candidate, run_root / "output" / f"{page_id}_candidate.pdf")
    if comparison.is_file():
        shutil.copy2(comparison, run_root / "previews" / f"{page_id}_comparison.png")


def write_artifact_index(run_root: Path) -> None:
    rows = [
        {
            "path": path.relative_to(run_root).as_posix(),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(run_root.rglob("*"))
        if path.is_file() and path != run_root / "reports" / "artifact_index.json"
    ]
    write_json(run_root / "reports" / "artifact_index.json", rows)
