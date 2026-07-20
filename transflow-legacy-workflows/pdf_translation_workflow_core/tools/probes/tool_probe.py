"""Probe local PDF workflow tools.

tool_name: tool_probe
category: probes
input_contract: optional output path
output_contract: JSON with Python package, executable, and font availability
failure_signals: missing required PyMuPDF or missing CJK fonts
fallback: caller may mark S_FAIL_TOOLING or use documented fallback
anti_overfit_statement: tool probe is environment-driven and does not inspect sample PDFs
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import now_local, write_json  # noqa: E402


def package_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    font_dir = Path("C:/Windows/Fonts")
    fonts = {
        "msyh": str(font_dir / "msyh.ttc"),
        "msyhbd": str(font_dir / "msyhbd.ttc"),
        "msyhl": str(font_dir / "msyhl.ttc"),
        "simhei": str(font_dir / "simhei.ttf"),
    }
    result = {
        "tool": "tool_probe",
        "probed_at_local": now_local(),
        "python": sys.version,
        "packages": {
            "fitz": package_available("fitz"),
            "pypdf": package_available("pypdf"),
            "pdfplumber": package_available("pdfplumber"),
            "reportlab": package_available("reportlab"),
            "PIL": package_available("PIL"),
        },
        "executables": {
            "pdfinfo": shutil.which("pdfinfo"),
            "pdftoppm": shutil.which("pdftoppm"),
        },
        "fonts": {name: {"path": path, "exists": Path(path).exists()} for name, path in fonts.items()},
        "path_encoding_rule": "Do not embed Chinese absolute paths in Python source; pass paths via CLI args, env vars, or manifest JSON.",
    }
    result["required_ok"] = bool(result["packages"]["fitz"]) and any(v["exists"] for v in result["fonts"].values())
    write_json(Path(args.out), result)
    print(args.out)
    return 0 if result["required_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

