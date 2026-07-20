"""使用分类结果中的真实 PDF 锁定 P9 普通叶回归边界。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf

from scripts.run_p9_real_samples import ScannedSample, _write_diagnostic_candidate
from transflow.adapters.ai.fixed import FixedTranslationAdapter
from transflow.application.contracts import EnumeratedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.toolbox_page_coordinator import ToolboxPageCoordinator, ToolboxPageWork
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.states import Fallback
from transflow.pdf_kernel import ControlledFontRegistry, PageFactsExtractor
from transflow.toolboxes.leaves import CoverToolbox, TableToolbox
from transflow.toolboxes.leaves.ordinary_policy import load_p9_ordinary_leaf_policy

TESTS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_ROOT.parent
REAL_SAMPLE_ROOT = REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "分类结果"
P9_POLICY = REPO_ROOT / "resources" / "manifests" / "p9_ordinary_leaf_policy.json"
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
FONT_ID = "noto-sans-cjk-sc-regular"


def _sha256_file(path: Path) -> str:
    """流式计算真实样本哈希，避免用文件名充当页面身份。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _enumerate_first_page(path: Path, ordinal: int) -> EnumeratedPage:
    """通过生产 PageFactsExtractor 枚举真实单页 PDF 的第一页。"""

    request = DocumentRunRequest(
        source_pdf_path=str(path.resolve()),
        source_hash=_sha256_file(path),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="a" * 64,
        job_id="job-p9-real-table-regression",
        run_id=f"run-p9-real-table-{ordinal:03d}",
    )
    pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)
    assert len(pages) == 1
    return pages[0]


def _intersects(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    """判断两个页面矩形是否存在正面积交集。"""

    return not (
        first[2] <= second[0]
        or second[2] <= first[0]
        or first[3] <= second[1]
        or second[3] <= first[1]
    )


def test_p9_real_table_with_logo_still_builds_translation_units() -> None:
    """真实表格页即使带 Logo 图片，也必须按表格事实领取原生单元格文字。"""

    table_root = REAL_SAMPLE_ROOT / "body" / "table"
    policy = load_p9_ordinary_leaf_policy(P9_POLICY)
    font_path = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    matched = 0
    for ordinal, sample_path in enumerate(sorted(table_root.glob("*.pdf"))):
        page = _enumerate_first_page(sample_path, ordinal)
        if not page.facts.image_objects or not page.facts.table_objects:
            continue
        matched += 1
        toolbox = TableToolbox(policy, font_path)
        template = toolbox.prepare(page.context, page.facts)
        batch = toolbox.build_translation_request(template)
        assert batch is not None, sample_path.name
        assert batch.units, sample_path.name

    assert matched > 0


def test_p9_real_cover_protected_background_conflict_falls_back_before_replay(
    tmp_path: Path,
) -> None:
    """真实封面文字覆盖受保护背景时，候选阶段必须整页回退而不是写入失败。"""

    cover_root = REAL_SAMPLE_ROOT / "cover"
    policy = load_p9_ordinary_leaf_policy(P9_POLICY)
    font_path = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path
    matched = 0
    for ordinal, sample_path in enumerate(sorted(cover_root.glob("*.pdf"))):
        page = _enumerate_first_page(sample_path, ordinal)
        toolbox = CoverToolbox(policy, font_path)
        template = toolbox.prepare(page.context, page.facts)
        snapshot = toolbox.audit_snapshot(template)
        if not any(
            _intersects(atom.bbox, protected)
            for atom in snapshot.atoms
            for protected in page.facts.protected_regions
        ):
            continue
        matched += 1
        batch = toolbox.build_translation_request(template)
        assert batch is not None
        translations = {unit.unit_id: "中" for unit in batch.units}
        result = ToolboxPageCoordinator(FixedTranslationAdapter(translations)).execute(
            ToolboxPageWork(page.context, page.facts, toolbox)
        )
        assert result.patch is None, sample_path.name
        assert result.proposed_patch is not None, sample_path.name
        assert result.proposed_patch.operations, sample_path.name
        assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
        assert "P9_PROTECTED_REGION_REJECTED" in result.outcome.finding_codes
        diagnostic_path = tmp_path / "diagnostic_candidate.pdf"
        diagnostic = _write_diagnostic_candidate(
            source_pdf=sample_path,
            candidate_pdf=diagnostic_path,
            sample=ScannedSample(
                "cover",
                sample_path,
                _sha256_file(sample_path),
                page,
                toolbox,
                len(batch.units),
                1,
                0,
            ),
            patch=result.proposed_patch,
            fonts=ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT),
        )
        assert diagnostic.status == "WRITTEN_UNSAFE_DIAGNOSTIC"
        assert diagnostic.applied_count == len(result.proposed_patch.operations)
        assert _sha256_file(diagnostic_path) != _sha256_file(sample_path)
        with pymupdf.open(diagnostic_path) as document:
            assert document.page_count == 1
            assert (
                document.metadata.get("subject")
                == "UNSAFE DIAGNOSTIC CANDIDATE - NOT FOR PRODUCTION"
            )
        break

    assert matched > 0
