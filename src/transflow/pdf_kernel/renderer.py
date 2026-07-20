"""使用 PyMuPDF 和唯一解释器生成 144 DPI 页面 PNG。"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from transflow.domain.pages import PageExecutionContext
from transflow.domain.toolbox import PagePatch
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.pdf_kernel.patch import PagePatchInterpreter, PatchApplicationResult

LOGGER = logging.getLogger("transflow.pdf_kernel.renderer")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
RENDER_SCALE = 2.0


@dataclass(frozen=True, slots=True)
class CandidateRender:
    """携带可解码 PNG 和唯一解释器结构化执行结果。"""

    png_bytes: bytes
    application: PatchApplicationResult | None


class PyMuPdfPageRenderer:
    """从完整源 PDF 直接栅格化指定页，不创建页级 PDF 中间件。"""

    def __init__(self, interpreter: PagePatchInterpreter) -> None:
        """绑定 candidate/final 共用的唯一 Patch 解释器。"""

        self._interpreter = interpreter

    def render_candidate(
        self,
        source_path: Path,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        patch: PagePatch,
        expected_owner: str,
    ) -> CandidateRender:
        """在内存文档副本应用 Patch 后直接生成 144 DPI PNG。"""

        LOGGER.info("调用候选渲染，意图=生成同语义 Patch 的页面 PNG page_no=%s", context.page_no)
        with pymupdf.open(source_path) as document:
            result = self._interpreter.apply(
                document,
                context,
                facts,
                patch,
                expected_owner,
            )
            pixmap = document[context.page_no - 1].get_pixmap(
                matrix=pymupdf.Matrix(RENDER_SCALE, RENDER_SCALE),
                alpha=False,
            )
            png_bytes = pixmap.tobytes("png")
        self.validate_png(png_bytes)
        return CandidateRender(png_bytes, result)

    def render_passthrough(self, source_path: Path, page_no: int) -> CandidateRender:
        """直接栅格化原页并返回无 Patch 的 144 DPI PNG。"""

        LOGGER.info("调用透传渲染，意图=发布原页预览 page_no=%s", page_no)
        with pymupdf.open(source_path) as document:
            pixmap = document[page_no - 1].get_pixmap(
                matrix=pymupdf.Matrix(RENDER_SCALE, RENDER_SCALE),
                alpha=False,
            )
            png_bytes = pixmap.tobytes("png")
        self.validate_png(png_bytes)
        return CandidateRender(png_bytes, None)

    @staticmethod
    def validate_png(content: bytes) -> None:
        """使用真实 PNG 解码器验证内容，不以扩展名或 magic bytes 代替。"""

        try:
            pixmap = pymupdf.Pixmap(content)
            if pixmap.width < 1 or pixmap.height < 1:
                raise ValueError("PNG 尺寸无效")
        except Exception as error:
            raise ValueError(f"PNG 无法解码:{type(error).__name__}") from error


def outside_region_diff_ratio(
    source_path: Path,
    candidate_path: Path,
    allowed_regions: Iterable[tuple[float, float, float, float]],
    *,
    page_no: int,
    scale: float = RENDER_SCALE,
    padding_points: float = 1.5,
    channel_tolerance: int = 3,
) -> float:
    """比较允许修改区域外的真实渲染像素，返回变化像素比例。"""

    LOGGER.info("调用区域外像素比较，意图=发现未声明视觉修改 page_no=%s", page_no)
    if page_no < 1 or scale <= 0 or padding_points < 0 or channel_tolerance < 0:
        raise ValueError("像素比较参数无效")
    with pymupdf.open(source_path) as source_document, pymupdf.open(
        candidate_path
    ) as candidate_document:
        source = source_document[page_no - 1].get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), alpha=False
        )
        candidate = candidate_document[page_no - 1].get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), alpha=False
        )
    if (source.width, source.height, source.n) != (
        candidate.width,
        candidate.height,
        candidate.n,
    ):
        return 1.0
    allowed_boxes = tuple(
        (
            round((x0 - padding_points) * scale),
            round((y0 - padding_points) * scale),
            round((x1 + padding_points) * scale),
            round((y1 + padding_points) * scale),
        )
        for x0, y0, x1, y1 in allowed_regions
    )
    source_bytes = memoryview(source.samples)
    candidate_bytes = memoryview(candidate.samples)
    changed = 0
    considered = 0
    for y_position in range(source.height):
        for x_position in range(source.width):
            if any(
                x0 <= x_position <= x1 and y0 <= y_position <= y1
                for x0, y0, x1, y1 in allowed_boxes
            ):
                continue
            considered += 1
            offset = (y_position * source.width + x_position) * source.n
            if any(
                abs(source_bytes[offset + channel] - candidate_bytes[offset + channel])
                > channel_tolerance
                for channel in range(source.n)
            ):
                changed += 1
    return changed / max(1, considered)


def main() -> int:
    """记录 renderer 固定输出 144 DPI PNG 而非页级 PDF。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("PyMuPdfPageRenderer 示例，意图=直接生成 144 DPI PNG")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
