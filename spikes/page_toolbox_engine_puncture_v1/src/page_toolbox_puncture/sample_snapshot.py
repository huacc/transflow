from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .contracts import SampleManifest, write_json


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_sample(
    *,
    repo_root: Path,
    project_root: Path,
    source_pdf: Path,
    sample_id: str,
    classification_path: str,
    leaf_key: str | None,
    original_document_id: str,
    original_page_number: int,
    source_document_sha256: str,
    expected_source_sha256: str,
    snapshot_group: str = "p1",
) -> SampleManifest:
    source_pdf = source_pdf.resolve()
    repo_root = repo_root.resolve()
    project_root = project_root.resolve()
    source_pdf.relative_to(repo_root)

    source_before = sha256_file(source_pdf)
    if source_before != expected_source_sha256:
        raise ValueError("upstream_pdf_sha256_mismatch")

    if not snapshot_group or "/" in snapshot_group or "\\" in snapshot_group or snapshot_group in {".", ".."}:
        raise ValueError("invalid_snapshot_group")
    snapshot = project_root / "samples" / snapshot_group / f"{sample_id}.pdf"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_pdf, snapshot)

    source_after = sha256_file(source_pdf)
    snapshot_sha256 = sha256_file(snapshot)
    if source_before != source_after:
        raise RuntimeError("upstream_pdf_changed_during_snapshot")
    if snapshot_sha256 != source_before:
        raise RuntimeError("snapshot_pdf_hash_mismatch")

    manifest = SampleManifest(
        sample_id=sample_id,
        classification_path=classification_path,
        leaf_key=leaf_key,
        upstream_pdf=source_pdf.relative_to(repo_root).as_posix(),
        upstream_sha256=source_before,
        snapshot_pdf=snapshot.relative_to(project_root).as_posix(),
        snapshot_sha256=snapshot_sha256,
        original_document_id=original_document_id,
        original_page_number=original_page_number,
        source_document_sha256=source_document_sha256,
    )
    write_json(project_root / "samples" / snapshot_group / "manifest.json", manifest)
    return manifest
