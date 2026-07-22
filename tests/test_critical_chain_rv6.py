"""覆盖 RV6 完整 PDF Gate 的纯合同与冻结边界。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path

import pymupdf
import pytest

from scripts.run_rv6_full_pdf_revalidation import (
    INPUT_SPECS,
    _choose_visual_pages,
    _document_run_id,
    _document_runtime_root,
    _evaluate_gates,
)
from tests.test_critical_chain_rv4 import (
    FONT_ID,
    FONT_MANIFEST,
    P0151,
    P8_POLICY,
    REPO_ROOT,
    _enumerate,
    _single,
    _valid_translations,
)
from tests.test_critical_chain_rv5 import _container
from transflow.adapters.ai.fixed import FixedTranslationAdapter
from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.application.route_capability import RouteCapabilityGuard
from transflow.application.toolbox_page_coordinator import (
    ToolboxPageCoordinator,
    ToolboxPageWork,
)
from transflow.application.translation_completeness import (
    build_semantic_unit_map,
    extract_required_literals,
)
from transflow.domain.artifacts import ArtifactPayload
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.toolbox import PagePatch, PatchOperation
from transflow.pdf_kernel import (
    ControlledFontRegistry,
    PageFactsExtractor,
    PagePatchInterpreter,
    patch_operation_hash,
)
from transflow.toolboxes.contracts import PageTemplate
from transflow.toolboxes.leaves.body_flow_text_single.judge import (
    _normalized,
    inspect_materialized_candidate,
    judge_placements,
)
from transflow.toolboxes.leaves.body_flow_text_single.layout import plan_placements
from transflow.toolboxes.leaves.body_flow_text_single.models import SinglePlacement
from transflow.toolboxes.leaves.body_flow_text_single.template import build_containers
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

RV6_RUN = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV6"
    / "06-full-pdf-current-chain-20260722-150135"
    / "documents"
)
BLIND_P0005 = RV6_RUN / "blind-annual-08210" / "pages" / "p0005" / "input" / "source.pdf"
BLIND_P0006 = RV6_RUN / "blind-annual-08210" / "pages" / "p0006" / "input" / "source.pdf"
BLIND_P0023 = RV6_RUN / "blind-annual-08210" / "pages" / "p0023" / "input" / "source.pdf"
BLIND_P0079 = RV6_RUN / "blind-annual-08210" / "pages" / "p0079" / "input" / "source.pdf"
TM2_P0111 = RV6_RUN / "tm2-baseline" / "pages" / "p0111" / "input" / "source.pdf"
RV6_RUN_07 = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV6"
    / "07-full-pdf-current-chain-20260722-171153"
    / "documents"
)
TM2_P0106 = RV6_RUN_07 / "tm2-baseline" / "pages" / "p0106" / "input" / "source.pdf"
RV6_RUN_08 = (
    REPO_ROOT
    / "runs"
    / "critical_chain_revalidation"
    / "RV6"
    / "08-full-pdf-current-chain-20260722-183354"
)


def _document(document_id: str, *, blind: bool = False) -> dict[str, object]:
    return {
        "all_pages_finalized": True,
        "blind_input": blind,
        "degradation_disclosed_count": 2,
        "disabled_page_count": 2,
        "document_id": document_id,
        "input_process_output_trace_complete": True,
        "openable": True,
        "page_count": 4,
        "page_count_preserved": True,
        "page_order_preserved": True,
        "preservation_passed": True,
        "source_passthrough_masquerade_count": 0,
        "target_page_count": 1,
        "target_pass_count": 1,
        "unregistered_page_count": 0,
        "visual_only_page_count": 1,
        "visual_only_pass_count": 1,
    }


def test_rv6_input_contract_freezes_tm2_and_one_new_blind_complete_pdf() -> None:
    assert len(INPUT_SPECS) == 2
    assert INPUT_SPECS[0].role == "CURRENT_TM2_UNSPLIT_FULL_PDF"
    assert INPUT_SPECS[1].role == "NEW_BLIND_UNSPLIT_FULL_PDF"
    assert all(item.source.suffix.lower() == ".pdf" for item in INPUT_SPECS)


def test_rv6_gates_require_both_full_documents_and_complete_delivery() -> None:
    gate = _evaluate_gates(
        (_document("tm2"), _document("blind", blind=True)),
        verification_pass=True,
    )
    assert gate["status"] == "PASS"
    assert gate["gates"]["G-RV-09"]["status"] == "PASS"
    assert gate["gates"]["G-RV-10"]["status"] == "PASS"


def test_rv6_target_passthrough_or_undisclosed_disabled_page_blocks_gate() -> None:
    failed_target = dict(_document("tm2"))
    failed_target["target_pass_count"] = 0
    failed_target["source_passthrough_masquerade_count"] = 1
    undisclosed = dict(_document("blind", blind=True))
    undisclosed["degradation_disclosed_count"] = 1
    gate = _evaluate_gates((failed_target, undisclosed), verification_pass=True)
    assert gate["gates"]["G-RV-09"]["status"] == "PASS"
    assert gate["gates"]["G-RV-10"]["status"] == "FAIL"
    assert gate["status"] == "FAIL"


def test_rv6_single_keeps_native_pdf_text_over_a_page_background_image() -> None:
    page = _enumerate(BLIND_P0005, "rv6-background-text")
    containers = build_containers(page.facts, load_p8_toolbox_policy(P8_POLICY))

    assert page.facts.image_objects
    assert containers
    assert any("Chairman" in item.source_text for item in containers)


def test_rv6_single_claims_a_large_title_above_the_body_margin() -> None:
    page = _enumerate(TM2_P0111, "rv6-title-boundary")
    containers = build_containers(page.facts, load_p8_toolbox_policy(P8_POLICY))

    assert any(item.source_text == "Audit Committee Report" for item in containers)


def test_rv6_unclaimed_text_uses_its_own_bbox_for_owner_assignment() -> None:
    page = _enumerate(TM2_P0111, "rv6-owner-bbox")
    semantic_map = build_semantic_unit_map(
        PageTemplate(
            "rv6-owner-bbox",
            page.context,
            page.facts.kernel_facts_hash,
            "body.flow_text.single",
            (),
        ),
        None,
        page.facts,
    )

    title = next(
        item
        for item in semantic_map.entries
        if item.source_text == "Audit Committee Report"
    )
    assert title.owner == "body.flow_text.single"


def test_rv6_classification_prompt_keeps_semantic_tables_out_of_single_flow() -> None:
    prompt_root = REPO_ROOT / "resources" / "prompts" / "classification" / "body_layout_owner"
    required_contract = "带可翻译的行标题、列标题或项目标签"
    one_row_contract = "即使只有一行数据"
    missed_table_contract = "TABLE1.count=0"
    paired_rows_contract = "制度、准则或修订项目—对应说明/生效信息"
    standards_list_contract = "会计准则、制度或修订项目清单不是普通正文列表"

    assert required_contract in (prompt_root / "decide.zh-CN.md").read_text(encoding="utf-8")
    assert required_contract in (prompt_root / "review.zh-CN.md").read_text(encoding="utf-8")
    assert one_row_contract in (prompt_root / "decide.zh-CN.md").read_text(encoding="utf-8")
    assert one_row_contract in (prompt_root / "review.zh-CN.md").read_text(encoding="utf-8")
    assert missed_table_contract in (prompt_root / "decide.zh-CN.md").read_text(
        encoding="utf-8"
    )
    assert missed_table_contract in (prompt_root / "review.zh-CN.md").read_text(
        encoding="utf-8"
    )
    assert paired_rows_contract in (prompt_root / "decide.zh-CN.md").read_text(
        encoding="utf-8"
    )
    assert paired_rows_contract in (prompt_root / "review.zh-CN.md").read_text(
        encoding="utf-8"
    )
    assert standards_list_contract in (prompt_root / "decide.zh-CN.md").read_text(
        encoding="utf-8"
    )
    assert standards_list_contract in (prompt_root / "review.zh-CN.md").read_text(
        encoding="utf-8"
    )


def test_rv6_route_guard_ignores_tiny_link_box_but_keeps_real_table_boundary() -> None:
    guard = RouteCapabilityGuard()
    tiny_link_box = _enumerate(TM2_P0106, "rv6-tiny-link-box").facts
    real_table = _enumerate(P0151, "rv6-real-table").facts

    assert tiny_link_box.table_objects
    assert guard.evaluate_facts("body.flow_text.single", tiny_link_box) is None

    finding = guard.evaluate_facts("body.flow_text.single", real_table)
    assert finding is not None
    assert finding.required_owner == "body.table"


def test_rv6_single_treats_full_page_image_as_text_underlay() -> None:
    container = _container("body", "body", (20.0, 20.0, 100.0, 50.0), 0)
    placement = SinglePlacement(
        "body",
        "译文",
        (20.0, 20.0, 180.0, 50.0),
        10.0,
        1.30,
        0,
        True,
    )
    codes = {
        item.code
        for item in judge_placements(
            "rv6-full-page-underlay",
            (container,),
            (placement,),
            clip_box=(0.0, 0.0, 200.0, 200.0),
            image_rects=((0.0, 0.0, 200.0, 200.0),),
        )
    }

    assert "PROTECTED_OBJECT_COLLISION" not in codes


def test_rv6_single_still_protects_independent_small_image() -> None:
    container = _container("body", "body", (20.0, 20.0, 100.0, 50.0), 0)
    placement = SinglePlacement(
        "body",
        "译文",
        (20.0, 20.0, 180.0, 50.0),
        10.0,
        1.30,
        0,
        True,
    )
    codes = {
        item.code
        for item in judge_placements(
            "rv6-small-image-obstacle",
            (container,),
            (placement,),
            clip_box=(0.0, 0.0, 200.0, 200.0),
            image_rects=((120.0, 20.0, 160.0, 50.0),),
        )
    }

    assert "PROTECTED_OBJECT_COLLISION" in codes


def test_rv6_kernel_writes_native_text_over_full_page_image_without_changing_image(
    tmp_path: Path,
) -> None:
    page, toolbox, _, _, semantic_map = _single(BLIND_P0005, "rv6-kernel-underlay")
    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(_valid_translations(semantic_map))
    ).execute(ToolboxPageWork(page.context, page.facts, toolbox))
    assert result.patch is not None

    candidate = tmp_path / "background-text-candidate.pdf"
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    with pymupdf.open(BLIND_P0005) as document:
        application = PagePatchInterpreter(fonts).apply(
            document,
            page.context,
            page.facts,
            result.patch,
            "body.flow_text.single",
        )
        assert application.fits
        document.save(candidate)

    candidate_page = _enumerate(candidate, "rv6-kernel-underlay-candidate")
    assert candidate_page.facts.locked_objects_hash == page.facts.locked_objects_hash
    assert tuple(
        (item.bbox, item.width, item.height, item.content_hash)
        for item in candidate_page.facts.image_objects
    ) == tuple(
        (item.bbox, item.width, item.height, item.content_hash)
        for item in page.facts.image_objects
    )


def test_rv6_kernel_still_rejects_output_over_independent_small_image(
    tmp_path: Path,
) -> None:
    source = tmp_path / "small-image-obstacle.pdf"
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 16, 16), False)
    pixmap.clear_with(0x3377AA)
    with pymupdf.open() as document:
        pdf_page = document.new_page(width=200, height=200)
        pdf_page.insert_text((20, 35), "Native source text", fontsize=10)
        pdf_page.insert_image(
            pymupdf.Rect(120, 20, 160, 50),
            stream=pixmap.tobytes("png"),
        )
        document.save(source)

    page = _enumerate(source, "rv6-kernel-small-image")
    target = page.facts.text_spans[0]
    output_rect = (20.0, 20.0, 180.0, 50.0)
    payload_hash = patch_operation_hash(
        owner="body.flow_text.single",
        target_object_ids=(target.object_id,),
        rect=output_rect,
        replacement_text="译文",
        font_id=FONT_ID,
        font_size=10.0,
        redaction_rects=(target.bbox,),
        preserve_drawing_overlap=True,
    )
    operation = PatchOperation(
        operation_id="rv6-small-image-operation",
        region_id="rv6-small-image-region",
        kind="replace_text",
        payload_hash=payload_hash,
        owner="body.flow_text.single",
        target_object_ids=(target.object_id,),
        rect=output_rect,
        replacement_text="译文",
        font_id=FONT_ID,
        font_size=10.0,
        redaction_rects=(target.bbox,),
        preserve_drawing_overlap=True,
    )
    patch = PagePatch(
        patch_id="rv6-small-image-patch",
        source_hash=page.context.source_hash,
        page_no=page.context.page_no,
        geometry_hash=page.context.geometry_hash,
        owner="body.flow_text.single",
        operations=(operation,),
    )

    with pymupdf.open(source) as document:
        with pytest.raises(DomainContractError) as captured:
            PagePatchInterpreter(
                ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
            ).apply(
                document,
                page.context,
                page.facts,
                patch,
                "body.flow_text.single",
            )
    assert captured.value.code is ErrorCode.PATCH_PROTECTED_OBJECT


def test_rv6_p0106_materialized_glyph_edges_are_not_reported_as_collisions() -> None:
    source = RV6_RUN_08 / "input" / "documents" / "tm2-baseline" / "source.pdf"
    candidate = RV6_RUN_08 / "documents" / "tm2-baseline" / "output" / "final.pdf"
    checkpoint_path = next(
        (
            RV6_RUN_08
            / "_runtime"
            / "tm2-baseline"
            / "pages"
            / "0106"
            / "checkpoints"
        ).glob("*.json")
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    payload = json.loads(base64.b64decode(checkpoint["payload_base64"]).decode("utf-8"))
    patch = PagePatch.from_dict(payload["patch"])
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    facts = PageFactsExtractor().extract_page(source, source_hash, 106)

    judgement = inspect_materialized_candidate(candidate, facts, patch)

    assert judgement.collision_count == 0
    assert judgement.passed


def test_rv6_materialized_text_normalizes_pdf_compatibility_hyphens() -> None:
    extracted = "非执行董事\u00a0NON‑EXECUTIVE"
    expected = "非执行董事 NON-EXECUTIVE"

    assert _normalized(extracted) == _normalized(expected)


def test_rv6_single_footer_uses_page_edge_instead_of_body_bottom_margin() -> None:
    page = _enumerate(BLIND_P0005, "rv6-footer-page-edge")
    policy = load_p8_toolbox_policy(P8_POLICY)
    containers = build_containers(page.facts, policy)
    font = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    placements = plan_placements(
        page.facts,
        containers,
        {item.container_id: "译文" for item in containers},
        policy,
        font,
    )
    footer_index = max(
        range(len(containers)),
        key=lambda index: containers[index].source_bbox[1],
    )

    assert containers[footer_index].role == "margin"
    assert placements[footer_index].fit
    assert placements[footer_index].output_bbox[3] <= page.facts.crop_box[3] - 4.0


def test_rv6_single_projects_each_native_bbox_to_only_one_semantic_unit() -> None:
    _, _, _, _, semantic_map = _single(BLIND_P0023, "rv6-unique-native-bbox")

    projected = tuple(
        object_id
        for entry in semantic_map.entries
        for object_id in entry.source_object_ids
    )
    assert len(projected) == len(set(projected))


def test_rv6_single_translates_a_styled_uppercase_heading() -> None:
    _, _, _, _, semantic_map = _single(BLIND_P0079, "rv6-uppercase-heading")

    revenue = next(item for item in semantic_map.entries if item.source_text == "7. REVENUE")
    assert revenue.disposition.value == "TRANSLATE"
    assert "REVENUE" not in extract_required_literals(revenue.source_text)


def test_rv6_single_does_not_treat_a_bold_heading_as_an_acronym() -> None:
    _, _, _, _, semantic_map = _single(BLIND_P0006, "rv6-bold-heading")

    gratitude = next(item for item in semantic_map.entries if item.source_text == "GRATITUDE")
    assert gratitude.disposition.value == "TRANSLATE"


def test_rv6_visual_selection_covers_focus_target_visual_and_distinct_disabled() -> None:
    pages = [
        {"page_no": 1, "route": "cover"},
        {"page_no": 2, "route": "visual_only"},
        {"page_no": 3, "route": "body.flow_text.single"},
        {"page_no": 4, "route": "body.table"},
        {"page_no": 5, "route": "body.flow_text.single"},
        {"page_no": 6, "route": "body.chart"},
    ]
    selected = _choose_visual_pages(pages, (4,))
    assert 4 in selected
    assert 2 in selected
    assert {3, 5}.issubset(selected)
    assert {1, 6}.issubset(selected)


@pytest.mark.skipif(os.name != "nt", reason="覆盖 Windows 经典路径长度边界")
def test_rv6_runtime_path_can_publish_layout_memory_artifact(tmp_path: Path) -> None:
    """RV6 长 run 名仍必须容纳布局记忆的哈希文件与原子写临时后缀。"""

    document_id = "tm2-baseline"
    digest = hashlib.sha256(b"{}").hexdigest()
    relative_path = f"artifacts/audit/document-layout-memory/{digest}.json"
    padding = 1
    while True:
        run_root = tmp_path / ("r" * padding)
        legacy_runtime = run_root / "documents" / document_id / "process/runtime"
        legacy_partial = legacy_runtime / f"{relative_path}.partial"
        if len(str(legacy_partial)) >= 267:
            break
        padding += 1

    runtime_root = _document_runtime_root(run_root, document_id)
    store = SharedFilesystemArtifactAdapter(runtime_root, "rv6-path-budget")
    payload = ArtifactPayload(
        artifact_id=f"document-layout-memory-{digest}",
        media_type="application/json",
        content=b"{}",
        content_hash=digest,
    )
    reference = store.put_atomic(payload, relative_path, "audit")
    assert store.verify(reference)


@pytest.mark.skipif(os.name != "nt", reason="覆盖 Windows 经典路径长度边界")
def test_rv6_internal_run_id_can_publish_final_artifact() -> None:
    """最终文件名不得再次嵌入完整外层 run 目录名并突破路径预算。"""

    with tempfile.TemporaryDirectory(prefix="rv6-path-") as temporary:
        temporary_root = Path(temporary)
        run_name = "05-full-pdf-current-chain-20260722-143651"
        document_id = "blind-annual-08210"
        digest = hashlib.sha256(b"%PDF-rv6").hexdigest()
        run_hash = hashlib.sha256(run_name.encode("utf-8")).hexdigest()[:12]
        legacy_run_id = f"rv6-{run_hash}-{document_id}"
        legacy_artifact_id = f"final-{legacy_run_id}"
        legacy_relative = (
            Path("_runtime")
            / document_id
            / "final"
            / f"{legacy_artifact_id}-{digest}.pdf.partial"
        )
        padding = 1
        while len(
            str(temporary_root / ("r" * padding) / run_name / legacy_relative)
        ) < 264:
            padding += 1

        run_root = temporary_root / ("r" * padding) / run_name
        runtime_root = _document_runtime_root(run_root, document_id)
        run_id = _document_run_id(run_root, document_id)
        artifact_id = f"final-{run_id}"
        relative_path = f"final/{artifact_id}-{digest}.pdf"
        store = SharedFilesystemArtifactAdapter(runtime_root, run_id)
        payload = ArtifactPayload(
            artifact_id=artifact_id,
            media_type="application/pdf",
            content=b"%PDF-rv6",
            content_hash=digest,
        )
        reference = store.put_atomic(payload, relative_path, "final")
        assert store.verify(reference)
