from __future__ import annotations

import json
import shutil
from pathlib import Path

from page_toolbox_puncture.contracts import write_json
from page_toolbox_puncture.sample_snapshot import sha256_file

from .run_package import write_artifact_index


def load_p4_manifest(path: Path, phase: str) -> list[dict[str, object]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = [row for row in rows if row["split"] == phase]
    if not selected:
        raise RuntimeError(f"p4_phase_has_no_samples:{phase}")
    return selected


def initialize_p4_batch(
    *,
    run_root: Path,
    toolbox_root: Path,
    run_id: str,
    phase: str,
    rows: list[dict[str, object]],
    model: str,
) -> None:
    run_root.mkdir(parents=True, exist_ok=False)
    for name in ("contracts", "docs", "input/source_pdfs", "input/prompts", "cases", "output", "previews", "reports"):
        (run_root / name).mkdir(parents=True, exist_ok=True)
    shutil.copy2(toolbox_root / "samples" / "p4_all_manifest.jsonl", run_root / "input" / "p4_all_manifest.jsonl")
    shutil.copy2(toolbox_root / "prompts" / "page_translation.en-zh.zh-CN.md", run_root / "input" / "prompts" / "page_translation.en-zh.zh-CN.md")
    shutil.copy2(toolbox_root / "prompts" / "page_translation.zh-en.zh-CN.md", run_root / "input" / "prompts" / "page_translation.zh-en.zh-CN.md")
    shutil.copy2(toolbox_root / "docs" / "P4_纵向流式排版与修复协议.md", run_root / "docs" / "P4_纵向流式排版与修复协议.md")
    for row in rows:
        source = Path(str(row["source_ref"]))
        target = run_root / "input" / "source_pdfs" / f"{row['sample_id']}_source.pdf"
        shutil.copy2(source, target)
        if sha256_file(target) != row["sha256"]:
            raise RuntimeError(f"p4_batch_source_hash_mismatch:{row['sample_id']}")
    (run_root / "README.md").write_text(
        "# P4 body.flow_text.single 批次运行包\n\n"
        f"阶段：`{phase}`；页数：{len(rows)}。本包携带原文、候选、合同、输入输出、修复轨迹和并排图。\n",
        encoding="utf-8",
    )
    (run_root / "EXECUTION.md").write_text(
        "# P4 执行顺序\n\n"
        "```text\n原文快照 -> 事实 -> 模板 -> 千问翻译 -> 纵向自然流 -> 确定性规则 -> 静态派发 RepairPatch -> 候选 -> 候选级图形规则 -> 重新渲染 -> 机械裁决 -> 聚焦视觉复核\n```\n\n"
        "同一页一轮只修一个病因并立即复判；已满足的规则必须 no-op。普通正文保持单列横向边界，只调纵向；标题和独立短行只有在右侧空白证据充分时才可有限扩展。\n",
        encoding="utf-8",
    )
    write_json(
        run_root / "contracts" / "batch_run_contract.json",
        {
            "schema_version": "p4-batch-run/v1",
            "run_id": run_id,
            "toolbox_key": "body.flow_text.single",
            "phase": phase,
            "model": model,
            "page_ids": [row["sample_id"] for row in rows],
            "page_count": len(rows),
            "horizontal_rule": "normal flow column width invariant; exceptional title or short line only",
            "vertical_rule": "vertical reflow, paragraph gap, line height and font size within page/footer bounds",
            "holdout_accessed": phase in {"holdout", "all-single"},
        },
    )


def publish_p4_case(*, run_root: Path, page_id: str, case_root: Path) -> None:
    candidate = case_root / "output" / "candidate.pdf"
    comparison = case_root / "previews" / "comparison.png"
    if candidate.is_file():
        shutil.copy2(candidate, run_root / "output" / f"{page_id}_candidate.pdf")
    if comparison.is_file():
        shutil.copy2(comparison, run_root / "previews" / f"{page_id}_comparison.png")


def finalize_p4_batch(run_root: Path, summary: dict[str, object]) -> None:
    write_json(run_root / "reports" / "batch_summary.json", summary)
    write_artifact_index(run_root)
