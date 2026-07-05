"""Render PDF pages to PNG with PyMuPDF.

tool_name: render_pdf
category: renderers
input_contract: input PDF path, output directory, filename prefix
output_contract: PNG files plus JSON manifest
failure_signals: unreadable PDF or missing rendered images
fallback: use Poppler renderer if available
anti_overfit_statement: renders pages generically without page-specific logic
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import ensure_dir, rel, resolve_workspace_path, write_json  # noqa: E402


def render(pdf_path: Path, out_dir: Path, prefix: str, zoom: float) -> dict:
    ensure_dir(out_dir)
    doc = fitz.open(pdf_path)
    images = []
    for page_index, page in enumerate(doc):
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        out = out_dir / f"{prefix}_page_{page_index + 1:02d}.png"
        pix.save(out)
        images.append({"page_index": page_index, "path": rel(out), "width": pix.width, "height": pix.height})
    result = {"tool": "render_pdf", "input_pdf": rel(pdf_path), "zoom": zoom, "images": images}
    doc.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--prefix", default="render")
    parser.add_argument("--zoom", type=float, default=2.0)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    result = render(resolve_workspace_path(args.input), Path(args.out_dir), args.prefix, args.zoom)
    write_json(Path(args.manifest), result)
    print(args.manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
