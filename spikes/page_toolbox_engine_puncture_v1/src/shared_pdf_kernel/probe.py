from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Any


def probe_tools() -> dict[str, Any]:
    font_dir = Path("C:/Windows/Fonts")
    fonts = [font_dir / "msyh.ttc", font_dir / "msyhbd.ttc", font_dir / "simhei.ttf", font_dir / "arial.ttf"]
    packages = {name: importlib.util.find_spec(name) is not None for name in ("fitz", "PIL", "pypdf", "httpx")}
    result = {
        "kernel": "shared-pdf-kernel/v1",
        "python": sys.version,
        "packages": packages,
        "executables": {"pdfinfo": shutil.which("pdfinfo"), "pdftoppm": shutil.which("pdftoppm")},
        "fonts": [{"path": str(path), "exists": path.exists()} for path in fonts],
    }
    result["required_ok"] = bool(packages["fitz"] and packages["PIL"] and any(item["exists"] for item in result["fonts"]))
    return result

