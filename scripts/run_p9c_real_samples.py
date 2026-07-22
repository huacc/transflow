"""生成 P9C 真实千问安全 final、隔离诊断候选和内容寻址证据。"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pymupdf

from tests.migration.p9_qwen_translation_adapter import MigrationQwenTranslationAdapter
from transflow.adapters.ai.fixed import FixedTranslationAdapter
from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.adapters.filesystem.common import atomic_write_bytes, atomic_write_json
from transflow.application.contracts import EnumeratedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.translated_diagnostic import (
    DiagnosticPageInput,
    TranslatedDiagnosticMaterializer,
)
from transflow.application.translation_completeness import (
    TranslationCompletenessGate,
    build_semantic_unit_map,
)
from transflow.domain.artifacts import ArtifactPayload
from transflow.domain.completeness import (
    CompletenessStatus,
    SemanticUnitMap,
    TranslationCompletenessDecision,
)
from transflow.domain.delivery import (
    DiagnosticStatus,
    FinalDeliveryArtifact,
    TranslatedDiagnosticCandidate,
)
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.pages import PageOutcome
from transflow.domain.result_axes import ThreeAxisResult, project_page_result
from transflow.domain.states import (
    ArtifactIntegrity,
    ArtifactProduced,
    Capability,
    Fallback,
    PagePipelineState,
    Quality,
    TranslationCoverage,
)
from transflow.domain.toolbox import PagePatch
from transflow.domain.translation import TranslationBatch
from transflow.pdf_kernel import ControlledFontRegistry, PageFactsExtractor
from transflow.pdf_kernel.patch import PagePatchInterpreter
from transflow.toolboxes.contracts import PageTemplate, PageToolbox, TranslationDispatch
from transflow.toolboxes.leaves import (
    AnchoredBlocksToolbox,
    ContentsToolbox,
    CoverToolbox,
    EndToolbox,
    MultiFlowTextToolbox,
    TableToolbox,
)
from transflow.toolboxes.leaves.ordinary_policy import load_p9_ordinary_leaf_policy

LOGGER = logging.getLogger("transflow.scripts.run_p9c_real_samples")
REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_ROOT = REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "分类结果"
RUNS_ROOT = REPO_ROOT / "runs"
OUTPUT_ROOT = REPO_ROOT / "output" / "pdf" / "P9C_real_samples"
PRODUCTION_ROOT = OUTPUT_ROOT / "production_safe"
DIAGNOSTIC_ROOT = OUTPUT_ROOT / "diagnostic_candidates"
SUMMARY_PATH = OUTPUT_ROOT / "P9C_real_samples_summary.json"
EVIDENCE_PATH = REPO_ROOT / "resources" / "evidence" / "p9c" / "p9c_real_regression.json"
P9_POLICY = REPO_ROOT / "resources" / "manifests" / "p9_ordinary_leaf_policy.json"
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
FONT_ID = "noto-sans-cjk-sc-regular"
ROUTES = (
    "cover",
    "contents",
    "end",
    "body.flow_text.multi",
    "body.table",
    "body.anchored_blocks",
)


@dataclass(frozen=True, slots=True)
class SelectedCandidate:
    """保存按结构事实筛出的真实页及其完整翻译合同。"""

    route: str
    source_path: Path
    page: EnumeratedPage
    toolbox: PageToolbox
    template: PageTemplate
    batch: TranslationBatch
    semantic_map: SemanticUnitMap


def _sha256_file(path: Path) -> str:
    """流式计算真实 PDF 内容哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    """把证据路径收敛为仓库相对 POSIX 路径。"""

    return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()


def _toolbox(route: str) -> PageToolbox:
    """按冻结 P9 策略和受控字体构造一个普通叶。"""

    policy = load_p9_ordinary_leaf_policy(P9_POLICY)
    font_path = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    factories: dict[str, type[PageToolbox]] = {
        "cover": CoverToolbox,
        "contents": ContentsToolbox,
        "end": EndToolbox,
        "body.flow_text.multi": MultiFlowTextToolbox,
        "body.table": TableToolbox,
        "body.anchored_blocks": AnchoredBlocksToolbox,
    }
    return factories[route](policy, font_path)  # type: ignore[call-arg]


def _enumerate_page(path: Path, run_id: str) -> EnumeratedPage:
    """通过生产 DocumentCoordinator 从真实单页 PDF 冻结 PageFacts。"""

    source_hash = _sha256_file(path)
    request = DocumentRunRequest(
        source_pdf_path=str(path.resolve()),
        source_hash=source_hash,
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="9" * 64,
        job_id=f"job-{run_id}",
        run_id=run_id,
    )
    return DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)[0]


def _probe_translation(semantic_map: SemanticUnitMap) -> dict[str, str]:
    """构造只用于筛选写入结构的完整输入，不把它登记为真实模型结果。"""

    entries = {item.unit_id: item for item in semantic_map.entries}
    return {
        unit_id: " ".join(("诊断译文", *entries[unit_id].required_literals))
        for unit_id in semantic_map.translated_unit_ids
    }


def _patch_contract_safe(candidate: SelectedCandidate, patch: PagePatch) -> bool:
    """只读预检 owner、CropBox 和 protected 区，避免把预期拒绝打印成异常。"""

    facts = candidate.page.facts
    crop_box = pymupdf.Rect(facts.crop_box)
    owned = set(facts.owned_object_ids)
    protected = set(facts.protected_object_ids)
    for operation in patch.operations:
        if (
            operation.kind != "replace_text"
            or operation.rect is None
            or operation.replacement_text is None
            or operation.font_id is None
            or operation.font_size is None
        ):
            return False
        targets = set(operation.target_object_ids)
        rectangle = pymupdf.Rect(operation.rect)
        if not targets <= owned or targets & protected or not crop_box.contains(rectangle):
            return False
        if any(
            rectangle.intersects(pymupdf.Rect(region))
            for region in facts.protected_regions
        ):
            return False
    return True


def _probe_materializable(candidate: SelectedCandidate, work_root: Path) -> bool:
    """用真实 Kernel 验证 owner、字体、写入和逐 unit 提取均可闭合。"""

    gate = TranslationCompletenessGate(maximum_targeted_retries=0).execute(
        candidate.semantic_map,
        candidate.batch,
        FixedTranslationAdapter(_probe_translation(candidate.semantic_map)),
    )
    if gate.bundle is None:
        return False
    plan = candidate.toolbox.consume_translation_bundle(
        candidate.template,
        TranslationDispatch(batch=candidate.batch, bundle=gate.bundle),
    )
    if plan.patch is None or not _patch_contract_safe(candidate, plan.patch):
        return False
    with tempfile.TemporaryDirectory(prefix="p9c-probe-", dir=work_root) as directory:
        probe_root = Path(directory)
        artifacts = SharedFilesystemArtifactAdapter(probe_root, "run-p9c-probe")
        diagnostic = TranslatedDiagnosticMaterializer(
            PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)),
            artifacts,
            probe_root,
        ).materialize_page(
            candidate.source_path,
            DiagnosticPageInput(
                candidate.page.context,
                candidate.page.facts,
                plan.patch,
                candidate.semantic_map,
                gate.bundle,
                gate.decision,
            ),
        )
    return diagnostic.status is DiagnosticStatus.TRANSLATED_DIAGNOSTIC_READY


def _select_candidate(run_id: str, work_root: Path) -> SelectedCandidate:
    """跨 P9 六叶按 map 覆盖和实际物化事实选择首个安全诊断结构。"""

    LOGGER.info("调用 P9C 真实样本选择，意图=拒绝文件名和公司身份分支")
    candidate_no = 0
    for route in ROUTES:
        route_root = SAMPLE_ROOT.joinpath(*route.split("."))
        for source_path in sorted(route_root.glob("*.pdf")):
            candidate_no += 1
            page = _enumerate_page(source_path, f"{run_id}-scan-{candidate_no:04d}")
            toolbox = _toolbox(route)
            template = toolbox.prepare(page.context, page.facts)
            batch = toolbox.build_translation_request(template)
            semantic_map = build_semantic_unit_map(template, batch, page.facts)
            if (
                batch is None
                or not semantic_map.translated_unit_ids
                or semantic_map.unresolved_unit_ids
            ):
                continue
            candidate = SelectedCandidate(
                route,
                source_path,
                page,
                toolbox,
                template,
                batch,
                semantic_map,
            )
            if _probe_materializable(candidate, work_root):
                return candidate
    raise RuntimeError("P9C_REAL_SAMPLE_NOT_MATERIALIZABLE")


def _publish_source_final(
    artifacts: SharedFilesystemArtifactAdapter,
    source_path: Path,
) -> FinalDeliveryArtifact:
    """把完整源 PDF 作为安全工程 final 内容寻址发布。"""

    content = source_path.read_bytes()
    content_hash = hashlib.sha256(content).hexdigest()
    artifact_id = f"p9c-final-{content_hash[:20]}"
    reference = artifacts.put_atomic(
        ArtifactPayload(artifact_id, "application/pdf", content, content_hash),
        f"artifacts/final/{artifact_id}-{content_hash}.pdf",
        "final",
    )
    artifacts.publish_final(reference)
    return FinalDeliveryArtifact(reference, True)


def _axes(
    candidate: SelectedCandidate,
    final: FinalDeliveryArtifact,
    decision: TranslationCompletenessDecision,
    diagnostic: TranslatedDiagnosticCandidate,
) -> ThreeAxisResult:
    """把安全源文 final 与翻译诊断投影为互不混淆的三轴结论。"""

    outcome = PageOutcome(
        page_no=1,
        state=PagePipelineState.FINALIZED,
        artifact_produced=ArtifactProduced.YES,
        integrity=ArtifactIntegrity.PASS,
        translation_coverage=TranslationCoverage.FULL,
        capability=Capability.PARTIAL,
        quality=Quality.FAIL,
        fallback=Fallback.PAGE_PASSTHROUGH,
        finding_codes=("P9C_DIAGNOSTIC_QUALITY_FAIL",),
    )
    return project_page_result(
        f"{candidate.route}-p1",
        outcome,
        final_available=final.artifact.label == "final",
        completeness=decision,
        diagnostic=diagnostic,
    )


def run() -> dict[str, object]:
    """执行真实千问、双轨 Artifact、投影副本和机器可验收证据。"""

    now = datetime.now().astimezone()
    run_id = f"run-p9c-real-{now.strftime('%Y%m%d-%H%M%S')}"
    run_root = RUNS_ROOT / run_id
    work_root = run_root / "work"
    work_root.mkdir(parents=True, exist_ok=True)
    candidate = _select_candidate(run_id, work_root)
    adapter = MigrationQwenTranslationAdapter(timeout_seconds=180.0, chunk_size=48)
    gate = TranslationCompletenessGate(maximum_targeted_retries=1).execute(
        candidate.semantic_map,
        candidate.batch,
        adapter,
    )
    if gate.decision.status is not CompletenessStatus.PASS or gate.bundle is None:
        raise RuntimeError("P9C_REAL_QWEN_COMPLETENESS_FAILED")
    plan = candidate.toolbox.consume_translation_bundle(
        candidate.template,
        TranslationDispatch(batch=candidate.batch, bundle=gate.bundle),
    )
    artifacts = SharedFilesystemArtifactAdapter(run_root, run_id)
    final = _publish_source_final(artifacts, candidate.source_path)
    diagnostic = TranslatedDiagnosticMaterializer(
        PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)),
        artifacts,
        run_root,
    ).materialize_page(
        candidate.source_path,
        DiagnosticPageInput(
            candidate.page.context,
            candidate.page.facts,
            plan.patch,
            candidate.semantic_map,
            gate.bundle,
            gate.decision,
        ),
    )
    if (
        diagnostic.status is not DiagnosticStatus.TRANSLATED_DIAGNOSTIC_READY
        or diagnostic.artifact is None
    ):
        raise RuntimeError("P9C_REAL_DIAGNOSTIC_NOT_READY")
    axes = _axes(candidate, final, gate.decision, diagnostic)
    final_projection = PRODUCTION_ROOT / f"p9c-final-{final.artifact.content_hash}.pdf"
    diagnostic_projection = DIAGNOSTIC_ROOT / (
        f"p9c-diagnostic-{diagnostic.artifact.content_hash}.pdf"
    )
    atomic_write_bytes(final_projection, artifacts.get(final.artifact.artifact_id))
    atomic_write_bytes(
        diagnostic_projection,
        artifacts.get(diagnostic.artifact.artifact_id),
    )
    evidence: dict[str, object] = {
        "artifact_manifest_path": _relative(run_root / "job" / "artifact_manifest.json"),
        "axes": axes.to_dict(),
        "bundle_hash": gate.decision.bundle_hash,
        "decision_hash": gate.decision.decision_hash,
        "diagnostic": diagnostic.to_dict(),
        "diagnostic_projection_path": _relative(diagnostic_projection),
        "final": {
            "artifact_id": final.artifact.artifact_id,
            "content_hash": final.artifact.content_hash,
            "projection_path": _relative(final_projection),
            "relative_path": final.artifact.relative_path,
            "source_passthrough": final.source_passthrough,
        },
        "final_manifest_path": _relative(run_root / "job" / "final_manifest.json"),
        "generated_at": now.isoformat(timespec="seconds"),
        "historical_gate_reexecution_count": 0,
        "map_hash": candidate.semantic_map.map_hash,
        "mock_result_count": 0,
        "qwen_http_calls": adapter.call_count,
        "route": candidate.route,
        "run_id": run_id,
        "run_root": _relative(run_root),
        "schema_version": "transflow.p9c-real-regression/v1",
        "source_hash": _sha256_file(candidate.source_path),
        "source_path": _relative(candidate.source_path),
        "translated_unit_count": len(candidate.semantic_map.translated_unit_ids),
    }
    atomic_write_json(EVIDENCE_PATH, evidence)
    atomic_write_json(SUMMARY_PATH, evidence)
    return evidence


def main() -> int:
    """运行并打印不含秘密的真实 P9C 产物摘要。"""

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    evidence = run()
    print(
        "P9C_REAL_SAMPLES PASS "
        f"run_id={evidence['run_id']} route={evidence['route']} "
        f"qwen_http_calls={evidence['qwen_http_calls']} "
        f"diagnostic_status={evidence['diagnostic']['status']}"  # type: ignore[index]
    )
    print(
        "P9C_REAL_OUTPUTS "
        f"production_safe={evidence['final']['projection_path']} "  # type: ignore[index]
        f"diagnostic={evidence['diagnostic_projection_path']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
