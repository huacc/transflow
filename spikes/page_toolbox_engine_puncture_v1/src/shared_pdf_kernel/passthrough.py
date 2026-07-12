from __future__ import annotations

import shutil
from pathlib import Path

from page_toolbox_puncture.sample_snapshot import sha256_file

from .workspace import require_under


def passthrough_pdf(*, workspace_root: Path, source_pdf: Path, output_pdf: Path) -> dict[str, object]:
    source_pdf = require_under(source_pdf, workspace_root, must_exist=True)
    output_pdf = require_under(output_pdf, workspace_root)
    source_hash = sha256_file(source_pdf)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_pdf, output_pdf)
    output_hash = sha256_file(output_pdf)
    return {"source_sha256": source_hash, "output_sha256": output_hash, "equivalent": source_hash == output_hash, "output_pdf": str(output_pdf)}

