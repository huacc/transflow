"""验证 RV5 single 布局、真实候选 Judge 与 Repair 边界。"""

from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path

import pymupdf

from transflow.adapters.ai.fixed import FixedTranslationAdapter
from transflow.application.contracts import EnumeratedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.repair_catalog import load_repair_policy
from transflow.application.toolbox_page_coordinator import ToolboxPageCoordinator, ToolboxPageWork
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.states import Fallback, Quality
from transflow.domain.toolbox import DecisionDisposition, PagePatch, PatchOperation
from transflow.pdf_kernel import ControlledFontRegistry, PageFactsExtractor, PagePatchInterpreter
from transflow.pdf_kernel.patch import patch_operation_hash
from transflow.toolboxes.leaves import SingleFlowTextToolbox
from transflow.toolboxes.leaves.body_flow_text_single.judge import (
    inspect_materialized_candidate,
    judge_placements,
)
from transflow.toolboxes.leaves.body_flow_text_single.layout import plan_placements
from transflow.toolboxes.leaves.body_flow_text_single.models import (
    SinglePlacement,
    SingleTextContainer,
)
from transflow.toolboxes.leaves.body_flow_text_single.template import build_containers
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_MANIFEST = REPO_ROOT / "resources/manifests/font_manifest.json"
P8_POLICY = REPO_ROOT / "resources/manifests/p8_toolbox_policy.json"
P9B_POLICY = REPO_ROOT / "resources/manifests/p9b_repair_policy.json"
FONT_ID = "noto-sans-cjk-sc-regular"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _page(path: Path) -> EnumeratedPage:
    request = DocumentRunRequest(
        source_pdf_path=str(path.resolve()),
        source_hash=_sha256(path),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="a" * 64,
        job_id="job-rv5-test",
        run_id="rv5-test",
    )
    return DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)[0]


def _blank_pdf(path: Path) -> Path:
    with pymupdf.open() as document:
        document.new_page(width=420, height=600)
        document.save(path)
    return path


def _single_pdf(path: Path, *, with_footer: bool = True) -> Path:
    with pymupdf.open() as document:
        page = document.new_page(width=420, height=600)
        page.insert_textbox(
            pymupdf.Rect(45, 110, 370, 165),
            "Operational overview for 2026. The business remained resilient.",
            fontsize=10,
            lineheight=1.30,
        )
        if with_footer:
            page.insert_text((45, 575), "Corporate Governance Report", fontsize=8)
            page.insert_text((365, 575), "99", fontsize=8)
        document.save(path)
    return path


def _superscript_pdf(path: Path) -> Path:
    with pymupdf.open() as document:
        page = document.new_page(width=420, height=600)
        page.insert_htmlbox(
            pymupdf.Rect(45, 100, 370, 180),
            "<p>Ms Cheung<sup>1</sup> owns an interest in the company.</p>",
        )
        page.insert_text((365, 575), "99", fontsize=8)
        document.save(path)
    return path


def _overlapping_redaction_pdf(path: Path) -> Path:
    with pymupdf.open() as document:
        page = document.new_page(width=420, height=600)
        page.insert_text((50, 100), "FIRST SOURCE", fontsize=10)
        page.insert_text(
            (50, 140),
            "SECOND SOURCE TEXT THAT COVERS THE FUTURE OUTPUT AREA",
            fontsize=10,
        )
        document.save(path)
    return path


def _container(
    container_id: str,
    role: str,
    bbox: tuple[float, float, float, float],
    order: int,
) -> SingleTextContainer:
    identity = f"{order + 1:064x}"
    return SingleTextContainer(
        container_id=container_id,
        semantic_object_id=identity,
        source_object_ids=(identity,),
        source_rects=(bbox,),
        source_text=container_id,
        reading_order=order,
        role=role,
        source_bbox=bbox,
        anchor=(bbox[0], bbox[1]),
        font_size=10.0,
        color_srgb=0,
        preferred_line_height=1.30,
    )


def test_rv5_t01_top_margin_does_not_shrink_body_safe_bottom(tmp_path: Path) -> None:
    """顶部页眉不能被当成正文内容底边；底部页脚才限制自然流。"""

    facts = _page(_blank_pdf(tmp_path / "blank.pdf")).facts
    containers = (
        _container("header", "margin", (30.0, 20.0, 180.0, 32.0), 0),
        _container("body", "body", (45.0, 110.0, 370.0, 150.0), 1),
        _container("footer", "margin", (45.0, 560.0, 180.0, 575.0), 2),
    )
    policy = load_p8_toolbox_policy(P8_POLICY)
    font = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    placements = plan_placements(
        facts,
        containers,
        {"header": "年度报告", "body": "正文自然排版。" * 30, "footer": "公司治理报告"},
        policy,
        font,
    )
    assert all(item.fit for item in placements)
    assert placements[1].output_bbox[3] < containers[2].source_bbox[1]


def test_rv5_t01_inline_superscript_is_not_misclassified_as_page_number(
    tmp_path: Path,
) -> None:
    """正文脚注号属于正文容器；只有页边距中的纯数字才是机械页码。"""

    page = _page(_superscript_pdf(tmp_path / "superscript.pdf"))
    containers = build_containers(page.facts, load_p8_toolbox_policy(P8_POLICY))
    body = next(item for item in containers if item.role != "margin")
    assert "Cheung1 owns" in body.source_text
    assert all(item.source_text != "99" for item in containers)


def test_rv5_t04_planned_judge_checks_clip_collision_and_protected_regions() -> None:
    """planned candidate 同时检查 clip、相互碰撞、非目标文字和保护对象。"""

    containers = (
        _container("left", "body", (10.0, 10.0, 100.0, 50.0), 0),
        _container("right", "body", (50.0, 30.0, 120.0, 70.0), 1),
    )
    placements = (
        SinglePlacement("left", "左", (10.0, 10.0, 100.0, 50.0), 10.0, 1.30, 0, True),
        SinglePlacement("right", "右", (50.0, 30.0, 120.0, 70.0), 10.0, 1.30, 0, True),
    )
    codes = {
        item.code
        for item in judge_placements(
            "rv5-planned",
            containers,
            placements,
            clip_box=(0.0, 0.0, 110.0, 100.0),
            protected_rects=((15.0, 15.0, 20.0, 20.0),),
            non_target_text_rects=((80.0, 20.0, 90.0, 40.0),),
        )
    }
    assert {
        "OWNER_CLIP_EXCEEDED",
        "PROTECTED_OBJECT_COLLISION",
        "NON_TARGET_TEXT_COLLISION",
        "TEXT_PLACEMENT_COLLISION",
    } <= codes


def test_rv5_t07_all_source_redactions_precede_translated_text_insertion(
    tmp_path: Path,
) -> None:
    """自然流下移后，后续源文字擦除不能再次擦掉前面已写入的译文。"""

    source = _overlapping_redaction_pdf(tmp_path / "overlap.pdf")
    page = _page(source)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    spans = {item.text: item for item in page.facts.text_spans}

    def operation(
        operation_id: str,
        source_text: str,
        output_rect: tuple[float, float, float, float],
        replacement: str,
    ) -> PatchOperation:
        target = spans[source_text]
        payload_hash = patch_operation_hash(
            owner="body.flow_text.single",
            target_object_ids=(target.object_id,),
            rect=output_rect,
            replacement_text=replacement,
            font_id=FONT_ID,
            font_size=8.0,
            redaction_rects=(target.bbox,),
            line_height=1.25,
            preserve_drawing_overlap=True,
        )
        return PatchOperation(
            operation_id=operation_id,
            region_id=operation_id,
            kind="replace_text",
            payload_hash=payload_hash,
            owner="body.flow_text.single",
            target_object_ids=(target.object_id,),
            rect=output_rect,
            replacement_text=replacement,
            font_id=FONT_ID,
            font_size=8.0,
            redaction_rects=(target.bbox,),
            line_height=1.25,
            preserve_drawing_overlap=True,
        )

    patch = PagePatch(
        patch_id="rv5-two-phase-redaction",
        source_hash=page.context.source_hash,
        page_no=1,
        geometry_hash=page.context.geometry_hash,
        owner="body.flow_text.single",
        operations=(
            operation("op-first", "FIRST SOURCE", (50, 130, 350, 155), "FIRST TRANSLATED"),
            operation(
                "op-second",
                "SECOND SOURCE TEXT THAT COVERS THE FUTURE OUTPUT AREA",
                (50, 180, 350, 205),
                "SECOND TRANSLATED",
            ),
        ),
    )
    candidate = tmp_path / "overlap-candidate.pdf"
    with pymupdf.open(source) as document:
        result = PagePatchInterpreter(fonts).apply(
            document,
            page.context,
            page.facts,
            patch,
            "body.flow_text.single",
        )
        assert result.fits
        document.save(candidate)
    with pymupdf.open(candidate) as document:
        output = unicodedata.normalize("NFKC", document[0].get_text())
    assert "FIRST TRANSLATED" in output
    assert "SECOND TRANSLATED" in output


def test_rv5_t03_extreme_translation_fails_without_approved_source_patch(
    tmp_path: Path,
) -> None:
    """超长译文无法安全容纳时必须诚实 fallback，不能以无变化 Repair 获批。"""

    source = _single_pdf(tmp_path / "extreme.pdf", with_footer=True)
    page = _page(source)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    toolbox = SingleFlowTextToolbox(
        load_p8_toolbox_policy(P8_POLICY), fonts.resolve(FONT_ID).path
    )
    batch = toolbox.build_translation_request(toolbox.prepare(page.context, page.facts))
    assert batch is not None
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
    assert result.translation_bundle is not None
    assert result.patch is None
    assert result.verdict.disposition is not DecisionDisposition.ACCEPT
    assert result.outcome.quality is Quality.FAIL
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH


def test_rv5_t05_single_has_a_deterministic_repair_atom() -> None:
    """single 的 overflow 进入叶私有静态目录，不借用其他叶或动态发现动作。"""

    catalog, _ = load_repair_policy(P9B_POLICY).resolve("body.flow_text.single")
    choices = catalog.applicable_atoms(
        ("TEXT_LAYOUT_OVERFLOW",),
        frozenset({"route_capability_match", "translation_complete"}),
        frozenset(),
        frozenset(),
        "b" * 64,
    )
    assert len(choices) == 1
    assert choices[0][0].atom_id == "body.flow_text.single.legacy_repair/v1"


def test_rv5_t07_materialized_candidate_is_reextracted_and_judged(tmp_path: Path) -> None:
    """真实候选必须重新提取，证明译文、行距、页码和锁定对象均通过。"""

    source = _single_pdf(tmp_path / "source.pdf", with_footer=True)
    page = _page(source)
    fonts = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)
    toolbox = SingleFlowTextToolbox(
        load_p8_toolbox_policy(P8_POLICY), fonts.resolve(FONT_ID).path
    )
    batch = toolbox.build_translation_request(toolbox.prepare(page.context, page.facts))
    assert batch is not None
    result = ToolboxPageCoordinator(
        FixedTranslationAdapter(
            {
                unit.unit_id: (
                    "公司治理报告"
                    if "Corporate" in unit.source_text
                    else "• 2026 年运营概览显示业务保持韧性，并持续改善治理和执行效率。" * 3
                )
                for unit in batch.units
            }
        )
    ).execute(ToolboxPageWork(page.context, page.facts, toolbox))
    assert result.patch is not None
    candidate = tmp_path / "candidate.pdf"
    with pymupdf.open(source) as document:
        application = PagePatchInterpreter(fonts).apply(
            document,
            page.context,
            page.facts,
            result.patch,
            "body.flow_text.single",
        )
        assert application.fits
        document.save(candidate)
    judgement = inspect_materialized_candidate(candidate, page.facts, result.patch)
    assert judgement.passed
    assert judgement.materialization_rate == 1.0
    assert judgement.line_spacing_violation_count == 0
    assert judgement.protected_modification_count == 0
