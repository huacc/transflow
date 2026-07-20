"""提供 job/run/page 私有目录分配和允许根边界校验。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from transflow.domain.errors import ErrorCode, PortCallError

LOGGER = logging.getLogger("transflow.pdf_kernel.workspace")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def require_under(path: Path, allowed_root: Path, *, must_exist: bool = False) -> Path:
    """解析路径并拒绝允许根逃逸、缺失输入和现有重解析点。"""

    root = allowed_root.resolve()
    candidate = path.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise PortCallError(
            ErrorCode.PATH_OUTSIDE_ALLOWED_ROOT,
            False,
            "路径越出允许工作区",
        ) from error
    if must_exist and not candidate.exists():
        raise PortCallError(ErrorCode.SOURCE_NOT_REGULAR_FILE, False, "工作区输入不存在")
    # 已存在的父链若包含符号链接，会让清理和发布目标发生重定向；因此提前拒绝。
    current = candidate if candidate.exists() else candidate.parent
    while current != root and current != current.parent:
        if current.exists() and current.is_symlink():
            raise PortCallError(
                ErrorCode.PATH_OUTSIDE_ALLOWED_ROOT,
                False,
                "工作区路径包含符号链接",
            )
        current = current.parent
    return candidate


def _validate_identity(value: str, field_name: str) -> str:
    """校验目录身份只包含安全字符，禁止路径片段和保留空值。"""

    if not IDENTITY_PATTERN.fullmatch(value):
        raise PortCallError(
            ErrorCode.INPUT_SHAPE_INVALID,
            False,
            f"{field_name} 不是安全目录身份",
        )
    return value


@dataclass(frozen=True, slots=True)
class RunWorkspace:
    """描述一个 run 的 job、页面、临时、报告和最终目录。"""

    run_root: Path
    job_root: Path
    pages_root: Path
    temp_root: Path
    reports_root: Path
    final_root: Path

    def page_root(self, page_no: int) -> Path:
        """返回一个 1-based 页面独占目录并确保它位于 run 根内。"""

        if page_no < 1:
            raise PortCallError(ErrorCode.INPUT_SHAPE_INVALID, False, "page_no 必须从 1 开始")
        path = require_under(self.pages_root / f"{page_no:04d}", self.run_root)
        path.mkdir(parents=True, exist_ok=True)
        return path


class WorkspaceAllocator:
    """在注入的允许根下按 job/transflow/run 分配私有工作目录。"""

    def __init__(self, allowed_root: Path) -> None:
        """绑定允许根，不依赖当前工作目录或宿主机绝对路径常量。"""

        self._allowed_root = allowed_root.resolve()
        self._allowed_root.mkdir(parents=True, exist_ok=True)

    def allocate(self, job_id: str, run_id: str) -> RunWorkspace:
        """创建一个不会与同名源 PDF 或其他 run 串扰的目录集合。"""

        safe_job = _validate_identity(job_id, "job_id")
        safe_run = _validate_identity(run_id, "run_id")
        run_root = require_under(
            self._allowed_root / safe_job / "transflow" / safe_run,
            self._allowed_root,
        )
        paths = {
            "job_root": run_root / "job",
            "pages_root": run_root / "pages",
            "temp_root": run_root / "tmp",
            "reports_root": run_root / "reports",
            "final_root": run_root / "final",
        }
        for path in paths.values():
            require_under(path, run_root).mkdir(parents=True, exist_ok=True)
        LOGGER.info(
            "调用工作区分配，意图=隔离 job/run/page 私有文件 job_id=%s run_id=%s",
            safe_job,
            safe_run,
        )
        return RunWorkspace(run_root=run_root, **paths)


def main() -> int:
    """记录工作区只接受注入允许根和安全身份。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("WorkspaceAllocator 示例，意图=禁止工作文件逃逸当前 run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
