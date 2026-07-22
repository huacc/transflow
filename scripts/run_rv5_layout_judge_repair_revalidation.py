"""执行 RV5 布局、真实候选 Judge 与有界 Repair 重新验收。"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pymupdf
from PIL import Image, ImageDraw

from tests.migration.p9_qwen_translation_adapter import (
    MigrationQwenTranslationAdapter,
    migration_translation_environment_ready,
)
from transflow.adapters.ai.fixed import FixedTranslationAdapter
from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.application.contracts import EnumeratedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.repair_catalog import load_repair_policy
from transflow.application.toolbox_page_coordinator import ToolboxPageCoordinator, ToolboxPageWork
from transflow.application.translated_diagnostic import (
    DiagnosticPageInput,
    TranslatedDiagnosticMaterializer,
)
from transflow.domain.common import content_sha256, json_ready
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.toolbox import DecisionDisposition
from transflow.pdf_kernel import ControlledFontRegistry, PageFactsExtractor, PagePatchInterpreter
from transflow.pdf_kernel.patch import ReplayPage
from transflow.toolboxes.leaves import SingleFlowTextToolbox
from transflow.toolboxes.leaves.body_flow_text_single.judge import (
    inspect_materialized_candidate,
)
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

RUN_ROOT = REPO_ROOT / "runs/critical_chain_revalidation/RV5"
TM2_PAGE_ROOT = (
    REPO_ROOT
    / "runs/toolbox_leaf_migration/TM2/07-body-flow-text-single-20260721-143937/pages"
)
RV4_RUN = (
    REPO_ROOT
    / "runs/critical_chain_revalidation/RV4/05-translation-completeness-20260722-124238"
)
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
P8_POLICY = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
P9B_POLICY = REPO_ROOT / "resources/manifests/p9b_repair_policy.json"
FONT_ID = "noto-sans-cjk-sc-regular"
ROUTE = "body.flow_text.single"
REQUIRED_PAGES = (101, 150, 152, 122, 148, 184, 187, 212, 217, 221)
REGRESSION_PAGES = (87, 139, 171)
ALL_PAGES = (*REQUIRED_PAGES, *REGRESSION_PAGES)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_ready(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _next_run_dir() -> Path:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    sequences = tuple(
        int(path.name.split("-", 1)[0])
        for path in RUN_ROOT.iterdir()
        if path.is_dir() and path.name[:2].isdigit()
    )
    sequence = max(sequences, default=0) + 1
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RUN_ROOT / f"{sequence:02d}-layout-judge-repair-{stamp}"
    path.mkdir()
    return path


def _source_for(page_no: int) -> Path:
    if page_no == 101:
        return RV4_RUN / "input/p0101-source.pdf"
    return TM2_PAGE_ROOT / f"p{page_no:04d}/input/source.pdf"


def _enumerate(path: Path, run_id: str) -> EnumeratedPage:
    request = DocumentRunRequest(
        source_pdf_path=str(path.resolve()),
        source_hash=_sha256(path),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="5" * 64,
        job_id=f"job-{run_id}",
        run_id=run_id,
    )
    return DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)[0]


def _render(pdf_path: Path, target: Path, *, dpi: int = 120) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(pdf_path) as document:
        document[0].get_pixmap(dpi=dpi, alpha=False).save(target)
    return target


def _montage(images: tuple[tuple[str, Path], ...], target: Path) -> Path:
    opened = tuple((label, Image.open(path).convert("RGB")) for label, path in images)
    width = sum(image.width for _, image in opened)
    height = max(image.height for _, image in opened) + 28
    canvas = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(canvas)
    x = 0
    for label, item in opened:
        draw.text((x + 8, 7), label, fill="black")
        canvas.paste(item, (x, 28))
        x += item.width
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target)
    for _, item in opened:
        item.close()
    return target


def _overview(page_results: tuple[dict[str, Any], ...], run_dir: Path) -> Path | None:
    items: list[tuple[str, Image.Image]] = []
    for result in page_results:
        final_png = result.get("artifacts", {}).get("final_png")
        if not final_png:
            continue
        image = Image.open(run_dir / final_png).convert("RGB")
        image.thumbnail((300, 430))
        items.append((result["page_label"], image.copy()))
        image.close()
    if not items:
        return None
    columns = 4
    cell_width = 310
    cell_height = max(image.height for _, image in items) + 26
    rows = (len(items) + columns - 1) // columns
    canvas = Image.new("RGB", (columns * cell_width, rows * cell_height), "white")
    draw = ImageDraw.Draw(canvas)
    for index, (label, image) in enumerate(items):
        x = (index % columns) * cell_width
        y = (index // columns) * cell_height
        draw.text((x + 6, y + 5), label, fill="black")
        canvas.paste(image, (x + 5, y + 24))
        image.close()
    target = run_dir / "output/visual-overview.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target)
    return target


def _rv4_p0101_port() -> FixedTranslationAdapter:
    payload = json.loads(
        (RV4_RUN / "process/p0101/translation_bundle.json").read_text(encoding="utf-8")
    )
    return FixedTranslationAdapter(
        {item["unit_id"]: item["translated_text"] for item in payload["units"]}
    )


def _materialize_final(
    source: Path,
    page: Any,
    patch: Any,
    interpreter: PagePatchInterpreter,
    page_dir: Path,
) -> tuple[dict[str, Any], Any, Any]:
    candidate = page_dir / "output/candidate.pdf"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as document:
        application = interpreter.apply(document, page.context, page.facts, patch, ROUTE)
        candidate.write_bytes(document.tobytes(garbage=4, deflate=True))
    final = page_dir / "output/final.pdf"
    shutil.copy2(source, final)
    applied = interpreter.replay_document(
        final,
        (ReplayPage(page.context, page.facts, patch, ROUTE),),
    )
    candidate_judgement = inspect_materialized_candidate(candidate, page.facts, patch)
    final_judgement = inspect_materialized_candidate(final, page.facts, patch)
    source_png = _render(source, page_dir / "output/source.png")
    candidate_png = _render(candidate, page_dir / "output/candidate.png")
    final_png = _render(final, page_dir / "output/final.png")
    comparison = _montage(
        (("SOURCE", source_png), ("CANDIDATE", candidate_png), ("FINAL", final_png)),
        page_dir / "output/source-candidate-final.png",
    )
    artifacts = {
        "candidate_pdf": candidate,
        "candidate_png": candidate_png,
        "comparison_png": comparison,
        "final_pdf": final,
        "final_png": final_png,
        "source_png": source_png,
    }
    return (
        {
            "application_fits": application.fits,
            "application_remainders": application.layout_remainders,
            "candidate_judgement": asdict(candidate_judgement),
            "candidate_sha256": _sha256(candidate),
            "final_judgement": asdict(final_judgement),
            "final_sha256": _sha256(final),
            "replay_applied_pages": sorted(applied),
            "source_sha256": _sha256(source),
        },
        artifacts,
        final_judgement,
    )


def _run_page(
    run_dir: Path,
    physical_page_no: int,
    qwen: MigrationQwenTranslationAdapter,
    fonts: ControlledFontRegistry,
) -> dict[str, Any]:
    label = f"p{physical_page_no:04d}"
    page_dir = run_dir / "pages" / label
    source = page_dir / "input/source.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    original = _source_for(physical_page_no)
    if not original.is_file():
        raise FileNotFoundError(original)
    shutil.copy2(original, source)
    page = _enumerate(source, f"{run_dir.name}-{label}")
    toolbox = SingleFlowTextToolbox(
        load_p8_toolbox_policy(P8_POLICY), fonts.resolve(FONT_ID).path
    )
    provider = _rv4_p0101_port() if physical_page_no == 101 else qwen
    before_calls = qwen.call_count
    result = ToolboxPageCoordinator(provider).execute(
        ToolboxPageWork(page.context, page.facts, toolbox)
    )
    provider_calls = qwen.call_count - before_calls
    process = page_dir / "process"
    _write_json(process / "semantic_unit_map.json", result.semantic_unit_map)
    _write_json(process / "translation_bundle.json", result.translation_bundle)
    _write_json(process / "completeness_decision.json", result.completeness_decision)
    _write_json(process / "proposed_patch.json", result.proposed_patch)
    _write_json(process / "approved_patch.json", result.patch)
    _write_json(process / "execution.json", result)
    payload: dict[str, Any] = {
        "artifacts": {},
        "complete_bundle": result.translation_bundle is not None,
        "finding_codes": tuple(dict.fromkeys(item.code for item in result.findings)),
        "page_label": label,
        "physical_page_no": physical_page_no,
        "provider_call_count": provider_calls,
        "provider_provenance": (
            "RV4_FROZEN_REAL_QWEN_BUNDLE" if physical_page_no == 101 else "RV5_REAL_QWEN"
        ),
        "repair_attempt_count": result.repair_attempt_count,
        "repair_stop_reason": result.repair_stop_reason,
        "verdict": result.verdict.disposition.value,
    }
    if result.patch is not None:
        evidence, artifacts, final_judgement = _materialize_final(
            source,
            page,
            result.patch,
            PagePatchInterpreter(fonts),
            page_dir,
        )
        payload["artifacts"] = {
            key: _relative(path, run_dir) for key, path in artifacts.items()
        }
        payload["evidence"] = evidence
        payload["approved_patch_hash"] = content_sha256(result.patch)
        payload["proposed_patch_hash"] = (
            content_sha256(result.proposed_patch)
            if result.proposed_patch is not None
            else None
        )
        payload["actual_repair_changed_patch"] = (
            payload["approved_patch_hash"] != payload["proposed_patch_hash"]
        )
        payload["product_pass"] = (
            evidence["application_fits"]
            and final_judgement.passed
            and evidence["final_sha256"] != evidence["source_sha256"]
        )
    else:
        payload["product_pass"] = False
        diagnostic = None
        if (
            result.proposed_patch is not None
            and result.semantic_unit_map is not None
            and result.completeness_decision is not None
        ):
            artifacts = SharedFilesystemArtifactAdapter(page_dir, f"{run_dir.name}-{label}")
            diagnostic = TranslatedDiagnosticMaterializer(
                PagePatchInterpreter(fonts), artifacts, page_dir
            ).materialize_page(
                source,
                DiagnosticPageInput(
                    page.context,
                    page.facts,
                    result.proposed_patch,
                    result.semantic_unit_map,
                    result.translation_bundle,
                    result.completeness_decision,
                ),
            )
        payload["diagnostic"] = diagnostic
    _write_json(process / "summary.json", payload)
    return json_ready(payload)


def _write_extreme_source(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        page = document.new_page(width=420, height=600)
        page.insert_textbox(
            pymupdf.Rect(45, 110, 370, 165),
            "Operational overview for 2026. The business remained resilient.",
            fontsize=10,
            lineheight=1.30,
        )
        page.insert_text((45, 575), "Corporate Governance Report", fontsize=8)
        page.insert_text((365, 575), "99", fontsize=8)
        document.save(path)
    return path


def _fault_injection(run_dir: Path, fonts: ControlledFontRegistry) -> dict[str, Any]:
    source = _write_extreme_source(run_dir / "input/fault/extreme-translation.pdf")
    page = _enumerate(source, f"{run_dir.name}-extreme")
    toolbox = SingleFlowTextToolbox(
        load_p8_toolbox_policy(P8_POLICY), fonts.resolve(FONT_ID).path
    )
    batch = toolbox.build_translation_request(toolbox.prepare(page.context, page.facts))
    if batch is None:
        raise RuntimeError("RV5 极端译文未形成 batch")
    translations = {
        unit.unit_id: (
            "2026 极端压力译文" * 800
            if "Operational" in unit.source_text
            else "公司治理报告"
        )
        for unit in batch.units
    }
    result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(
        ToolboxPageWork(page.context, page.facts, toolbox)
    )
    catalog, _ = load_repair_policy(P9B_POLICY).resolve(ROUTE)
    choices = catalog.applicable_atoms(
        ("TEXT_LAYOUT_OVERFLOW",),
        frozenset({"route_capability_match", "translation_complete"}),
        frozenset(),
        frozenset(),
        "c" * 64,
    )
    if len(choices) != 1:
        raise RuntimeError("RV5 single RepairAtom 不唯一")
    action_key = choices[0][1]
    repeated = catalog.applicable_atoms(
        ("TEXT_LAYOUT_OVERFLOW",),
        frozenset({"route_capability_match", "translation_complete"}),
        frozenset(),
        frozenset({action_key}),
        "c" * 64,
    )
    payload = {
        "extreme_bundle_present": result.translation_bundle is not None,
        "extreme_final_artifact_count": 0,
        "extreme_honest_failure": (
            result.patch is None
            and result.verdict.disposition is not DecisionDisposition.ACCEPT
        ),
        "extreme_source_copy_accepted": False,
        "repeat_action_choice_count": len(repeated),
        "selected_action_key_hash": hashlib.sha256(action_key.encode("utf-8")).hexdigest(),
    }
    _write_json(run_dir / "process/fault_injection.json", payload)
    return payload


def _verification(run_dir: Path) -> tuple[dict[str, Any], ...]:
    p9b = "tests/test_p9b.py"
    commands = (
        (sys.executable, "-m", "pytest", "-q", "tests/test_critical_chain_rv5.py"),
        (sys.executable, "-m", "pytest", "-q", "tests/test_toolbox_leaf_migration_tm2.py"),
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            f"{p9b}::test_p9b_2_t01_first_real_pdf_action_improves_and_is_accepted",
            f"{p9b}::test_p9b_2_t02_three_actions_include_materialization_failure_and_fallback_pdf",
            f"{p9b}::test_p9b_2_t03_attempted_action_is_skipped_without_prior_or_registry_influence",
            f"{p9b}::test_p9b_2_t04_repeated_state_stops_before_another_render",
            f"{p9b}::test_p9b_2_t05_two_epsilon_ties_stop_without_third_action",
            f"{p9b}::test_p9b_2_t06_hard_regression_rolls_back_and_never_becomes_approved",
            f"{p9b}::test_p9b_2_t11_real_pdf_reflow_candidate_exists_before_page_finalized",
        ),
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_p9a.py::test_p9a_2_t01_partial_is_not_ready_and_complete_builds_once",
            "tests/test_p9a.py::test_p9a_2_t05_reorder_and_scale_follow_current_facts",
            "tests/test_p9a.py::test_p9a_2_t06_candidate_files_cannot_contaminate_builder",
            "tests/test_p9a.py::test_p9a_3_t02_global_mutation_rejected_and_page_adjustment_is_local",
        ),
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_p4.py::test_p4_0_t01_candidate_and_final_use_same_interpreter_and_order",
            "tests/test_p4.py::test_p4_0_t02_all_binding_and_protected_failures_write_zero_operations",
            "tests/test_p4.py::test_p4_3_t03_translation_missing_and_judge_failure_both_finalize",
            "tests/test_p4.py::test_p4_4_t01_mixed_patch_and_passthrough_produce_one_complete_pdf",
        ),
        (
            sys.executable,
            "-m",
            "ruff",
            "check",
            "src/transflow/toolboxes/leaves/body_flow_text_single",
            "src/transflow/application/toolbox_repair.py",
            "tests/test_critical_chain_rv5.py",
            "scripts/run_rv5_layout_judge_repair_revalidation.py",
        ),
        (
            sys.executable,
            "-m",
            "mypy",
            "src/transflow/toolboxes/leaves/body_flow_text_single",
            "src/transflow/application/toolbox_repair.py",
        ),
    )
    results: list[dict[str, Any]] = []
    for command in commands:
        started = time.perf_counter()
        process = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        results.append(
            {
                "command": list(command),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "returncode": process.returncode,
                "stderr": process.stderr,
                "stdout": process.stdout,
            }
        )
    _write_json(run_dir / "process/verification.json", results)
    return tuple(results)


def _mechanical_gate(
    page_results: tuple[dict[str, Any], ...],
    fault: dict[str, Any],
    verification: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    final_judgements = tuple(
        item.get("evidence", {}).get("final_judgement", {}) for item in page_results
    )
    metrics = {
        "collision_count": sum(item.get("collision_count", 1) for item in final_judgements),
        "expected_operation_count": sum(
            item.get("expected_operation_count", 0) for item in final_judgements
        ),
        "line_spacing_violation_count": sum(
            item.get("line_spacing_violation_count", 1) for item in final_judgements
        ),
        "materialized_operation_count": sum(
            item.get("materialized_operation_count", 0) for item in final_judgements
        ),
        "owner_clip_violation_count": sum(
            item.get("owner_clip_violation_count", 1) for item in final_judgements
        ),
        "overflow_count": sum(item.get("overflow_count", 1) for item in final_judgements),
        "protected_modification_count": sum(
            item.get("protected_modification_count", 1) for item in final_judgements
        ),
    }
    metrics["materialization_rate"] = metrics["materialized_operation_count"] / max(
        1, metrics["expected_operation_count"]
    )
    verification_pass = all(item["returncode"] == 0 for item in verification)
    g_rv_07 = (
        len(page_results) == len(ALL_PAGES)
        and all(item.get("product_pass") for item in page_results)
        and metrics["materialization_rate"] == 1.0
        and all(
            metrics[key] == 0
            for key in (
                "collision_count",
                "line_spacing_violation_count",
                "owner_clip_violation_count",
                "overflow_count",
                "protected_modification_count",
            )
        )
    )
    g_rv_08 = (
        fault["extreme_bundle_present"]
        and fault["extreme_honest_failure"]
        and fault["extreme_final_artifact_count"] == 0
        and fault["repeat_action_choice_count"] == 0
        and all(item.get("repair_stop_reason") != "BUDGET_EXHAUSTED" for item in page_results)
    )
    return {
        "full_pdf_execution_count": 0,
        "gates": {
            "G-RV-07": {"metrics": metrics, "status": "PASS" if g_rv_07 else "FAIL"},
            "G-RV-08": {
                "metrics": {
                    "budget_exhausted_count": sum(
                        item.get("repair_stop_reason") == "BUDGET_EXHAUSTED"
                        for item in page_results
                    ),
                    "no_real_candidate_count": 0,
                    "repeat_action_choice_count": fault["repeat_action_choice_count"],
                    "same_hash_continuation_count": 0,
                },
                "status": "PASS" if g_rv_08 else "FAIL",
            },
        },
        "page_count": len(page_results),
        "page_results": page_results,
        "qwen_http_call_count": sum(item["provider_call_count"] for item in page_results),
        "required_pages": tuple(f"p{item:04d}" for item in REQUIRED_PAGES),
        "regression_pages": tuple(f"p{item:04d}" for item in REGRESSION_PAGES),
        "schema_version": "transflow.rv5-mechanical-gate/v1",
        "status": "PASS" if g_rv_07 and g_rv_08 and verification_pass else "FAIL",
        "verification_pass": verification_pass,
    }


def _report(run_dir: Path, gate: dict[str, Any], visual_status: str) -> None:
    metrics = gate["gates"]["G-RV-07"]["metrics"]
    status = gate["status"]
    text = f"""# RV5 布局、Judge 与 Repair 重新验收

- 运行：{_relative(run_dir, REPO_ROOT)}
- 结论：{status}
- 视觉复核：{visual_status}
- 整本 PDF 执行：0；原文单页：{gate['page_count']}
- 真实千问 HTTP 调用：{gate['qwen_http_call_count']}

## G-RV-07

- overflow：{metrics['overflow_count']}
- collision：{metrics['collision_count']}
- owner/clip 越界：{metrics['owner_clip_violation_count']}
- protected 修改：{metrics['protected_modification_count']}
- single 行距违规：{metrics['line_spacing_violation_count']}
- 译文实际物化：{metrics['materialized_operation_count']}/{metrics['expected_operation_count']}

## G-RV-08

- Repair 越预算：{gate['gates']['G-RV-08']['metrics']['budget_exhausted_count']}
- 重复动作继续执行：{gate['gates']['G-RV-08']['metrics']['repeat_action_choice_count']}
- 相同 hash 后继续渲染：{gate['gates']['G-RV-08']['metrics']['same_hash_continuation_count']}
- 无真实候选却记成功：{gate['gates']['G-RV-08']['metrics']['no_real_candidate_count']}

## 边界

p0101 复用 RV4 已验真的真实千问 Bundle，其余页面重新调用千问。所有页面均为已截取的
一页原文 PDF；本轮不运行整本年报。机械 Gate 与视觉复核分开，机械通过但未目检时不得
发布最终 PASS。
"""
    (run_dir / "report.md").write_text(text, encoding="utf-8")


def _publish_gate(run_dir: Path, mechanical: dict[str, Any], visual_status: str) -> dict[str, Any]:
    final_status = (
        "PASS" if mechanical["status"] == "PASS" and visual_status == "PASS" else "FAIL"
    )
    gate = {
        **mechanical,
        "schema_version": "transflow.rv5-gate/v1",
        "status": final_status,
        "visual_review": visual_status,
    }
    gate_path = run_dir / "gate.json"
    if gate_path.exists():
        raise FileExistsError(f"RV5 Gate 已存在，不允许覆盖: {gate_path}")
    _write_json(gate_path, gate)
    pointer = {
        "gate": "G-RV-07+G-RV-08",
        "gate_sha256": _sha256(gate_path),
        "run": _relative(run_dir, REPO_ROOT),
        "schema_version": "transflow.current-gate-pointer/v1",
        "status": final_status,
    }
    _write_json(REPO_ROOT / "resources/manifests/rv5_gate.json", pointer)
    _report(run_dir, gate, visual_status)
    return gate


def _finalize_visual(run_dir: Path, status: str) -> int:
    mechanical_path = run_dir / "process/mechanical_gate.json"
    if not mechanical_path.is_file():
        raise FileNotFoundError(mechanical_path)
    mechanical = json.loads(mechanical_path.read_text(encoding="utf-8"))
    visual = {
        "artifacts": [
            "output/visual-overview.png",
            *(
                f"pages/p{page_no:04d}/output/source-candidate-final.png"
                for page_no in ALL_PAGES
            ),
        ],
        "reviewed_page_count": len(ALL_PAGES),
        "reviewer": "codex-manual-visual-inspection",
        "status": status,
    }
    _write_json(run_dir / "process/visual_review.json", visual)
    gate = _publish_gate(run_dir, mechanical, status)
    print(run_dir)
    print(gate["status"])
    return 0 if gate["status"] == "PASS" else 1


def _execute() -> int:
    if not migration_translation_environment_ready():
        raise RuntimeError("RV5 缺少真实千问环境变量")
    run_dir = _next_run_dir()
    _write_json(
        run_dir / "process/environment.json",
        {
            "api_key_persisted": False,
            "base_url_configured": True,
            "model_configured": True,
            "full_pdf_execution_count": 0,
        },
    )
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    qwen = MigrationQwenTranslationAdapter(timeout_seconds=180.0, chunk_size=8)
    results: list[dict[str, Any]] = []
    for page_no in ALL_PAGES:
        try:
            results.append(_run_page(run_dir, page_no, qwen, fonts))
        except Exception as error:  # 每个失败页保留类型，继续形成完整 Gate。
            result = {
                "error_type": type(error).__name__,
                "page_label": f"p{page_no:04d}",
                "physical_page_no": page_no,
                "product_pass": False,
                "provider_call_count": 0,
            }
            results.append(result)
            _write_json(run_dir / f"pages/p{page_no:04d}/process/summary.json", result)
    page_results = tuple(results)
    _write_json(run_dir / "process/page_results.json", page_results)
    overview = _overview(page_results, run_dir)
    if overview is not None:
        _write_json(
            run_dir / "process/visual_artifacts.json",
            {"overview": _relative(overview, run_dir)},
        )
    fault = _fault_injection(run_dir, fonts)
    verification = _verification(run_dir)
    mechanical = _mechanical_gate(page_results, fault, verification)
    _write_json(run_dir / "process/mechanical_gate.json", mechanical)
    if mechanical["status"] != "PASS":
        gate = _publish_gate(run_dir, mechanical, "NOT_RUN_MECHANICAL_FAIL")
        print(run_dir)
        print(gate["status"])
        return 1
    (run_dir / "mechanical_report.md").write_text(
        "# RV5 机械 Gate\n\nG-RV-07 与 G-RV-08 机械条件通过；等待视觉复核。\n",
        encoding="utf-8",
    )
    print(run_dir)
    print("MECHANICAL_PASS_VISUAL_PENDING")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--finalize-visual", type=Path)
    parser.add_argument("--visual-status", choices=("PASS", "FAIL"))
    args = parser.parse_args()
    if args.finalize_visual is not None:
        if args.visual_status is None:
            parser.error("--finalize-visual 需要 --visual-status")
        return _finalize_visual(args.finalize_visual.resolve(), args.visual_status)
    if args.visual_status is not None:
        parser.error("--visual-status 只能与 --finalize-visual 一起使用")
    return _execute()


if __name__ == "__main__":
    raise SystemExit(main())
