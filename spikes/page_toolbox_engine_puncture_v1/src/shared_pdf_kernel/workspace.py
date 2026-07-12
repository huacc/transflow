from __future__ import annotations

import os
from pathlib import Path


class WorkspaceBoundaryError(ValueError):
    pass


def is_under(path: Path, root: Path) -> bool:
    resolved_path = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    try:
        return os.path.commonpath([os.path.normcase(str(resolved_path)), os.path.normcase(str(resolved_root))]) == os.path.normcase(str(resolved_root))
    except ValueError:
        return False


def require_under(path: Path, root: Path, *, must_exist: bool = False) -> Path:
    resolved = path.resolve(strict=False)
    if not is_under(resolved, root):
        raise WorkspaceBoundaryError(f"path_outside_workspace:{resolved}")
    if must_exist and not resolved.exists():
        raise WorkspaceBoundaryError(f"required_path_missing:{resolved}")
    return resolved

