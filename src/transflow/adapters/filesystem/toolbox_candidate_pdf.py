"""使用唯一 PagePatchInterpreter 从真实源 PDF 物化 P9B 候选 PDF。"""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf

from transflow.domain.pages import PageExecutionContext
from transflow.domain.toolbox import PagePatch
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.pdf_kernel.patch import PagePatchInterpreter

LOGGER = logging.getLogger("transflow.adapters.filesystem.toolbox_candidate_pdf")
FILESYSTEM_ROOT = Path(__file__).resolve().parent.parent


class ToolboxCandidatePdfRenderer:
    """从完整源文件副本应用已声明 Patch，并在内存中形成可打开候选 PDF。"""

    def __init__(
        self,
        source_path: Path,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        interpreter: PagePatchInterpreter,
        expected_owner: str,
    ) -> None:
        """绑定调用方选择的真实源、页面事实和唯一 Patch 解释器。"""

        self._source_path = source_path.resolve()
        self._context = context
        self._facts = facts
        self._interpreter = interpreter
        self._expected_owner = expected_owner

    def render_pdf(self, patch: PagePatch | None) -> bytes:
        """物化完整候选 PDF，并用 PyMuPDF 重新打开验证页数与目标页。"""

        LOGGER.info(
            "调用 P9B 候选 PDF 物化，意图=保存实际 Judge 输入 page_no=%s patch=%s",
            self._context.page_no,
            patch.patch_id if patch is not None else "candidate-0-no-patch",
        )
        with pymupdf.open(self._source_path) as document:
            if patch is not None:
                self._interpreter.apply(
                    document,
                    self._context,
                    self._facts,
                    patch,
                    self._expected_owner,
                )
            content = document.tobytes(garbage=4, deflate=True)
            expected_pages = document.page_count
        with pymupdf.open(stream=content, filetype="pdf") as verification:
            if verification.page_count != expected_pages:
                raise ValueError("候选 PDF 页面数漂移")
            verification.load_page(self._context.page_no - 1)
        return content


def main() -> int:
    """记录 P9B 候选必须复用唯一 Patch 解释器并重新打开验证。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("ToolboxCandidatePdfRenderer 示例，意图=从真实源物化完整候选 PDF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
