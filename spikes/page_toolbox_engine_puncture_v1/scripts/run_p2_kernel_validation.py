from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from page_toolbox_puncture.contracts import ContainerWrite, PagePatch, to_jsonable, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file, snapshot_sample
from shared_pdf_kernel.facts import canonical_sha256, extract_page_facts
from shared_pdf_kernel.passthrough import passthrough_pdf
from shared_pdf_kernel.patch import apply_page_patch
from shared_pdf_kernel.probe import probe_tools
from shared_pdf_kernel.render import render_contact_sheet, render_page


SAMPLES = (
    {
        "sample_id": "S2P0043",
        "classification_path": "body/flow_text/single",
        "leaf_key": "body.flow_text.single",
        "upstream_pdf": "spikes/page_classification_engine_puncture_v1/分类结果/body/flow_text/single/S2P0043.pdf",
        "upstream_sha256": "3ac3838d414b14fb0e9d79ac576928d3351e2d166d9ee73ae9795e46b33fac56",
        "original_document_id": "R003",
        "original_page_number": 10,
        "source_document_sha256": "7560dd2df5be66699d094a5ba55e83ef2810dcf5dc500c4fa152a5a1623fcc0d",
    },
    {
        "sample_id": "S2P0049",
        "classification_path": "body/table",
        "leaf_key": "body.table",
        "upstream_pdf": "spikes/page_classification_engine_puncture_v1/分类结果_无边框规则回归/body/table/S2P0049.pdf",
        "upstream_sha256": "b780fc80ace98da1274cac90390208b220b4f95f86b98e85680d126228d4678f",
        "original_document_id": "R003",
        "original_page_number": 54,
        "source_document_sha256": "7560dd2df5be66699d094a5ba55e83ef2810dcf5dc500c4fa152a5a1623fcc0d",
    },
    {
        "sample_id": "S2P0080",
        "classification_path": "visual_only",
        "leaf_key": None,
        "upstream_pdf": "spikes/page_classification_engine_puncture_v1/分类结果/visual_only/S2P0080.pdf",
        "upstream_sha256": "d1e04bb669d1ff7d6f5a60cc3464d22406edfc6c7aa4c973aad5de6837fbf5b7",
        "original_document_id": "R004",
        "original_page_number": 202,
        "source_document_sha256": "456ff48c147cf0f73ed44fa170ac77e6973831d0290322288b206f7d6a2050e1",
    },
)


def main() -> int:
    out = ROOT / "artifacts" / "p2" / "kernel_validation"
    out.mkdir(parents=True, exist_ok=True)
    manifests = []
    facts_by_id = {}
    sample_evidence = []

    for config in SAMPLES:
        source = REPO_ROOT / config["upstream_pdf"]
        source_before = sha256_file(source)
        manifest = snapshot_sample(
            repo_root=REPO_ROOT,
            project_root=ROOT,
            source_pdf=source,
            sample_id=config["sample_id"],
            classification_path=config["classification_path"],
            leaf_key=config["leaf_key"],
            original_document_id=config["original_document_id"],
            original_page_number=config["original_page_number"],
            source_document_sha256=config["source_document_sha256"],
            expected_source_sha256=config["upstream_sha256"],
            snapshot_group="p2",
        )
        manifests.append(manifest)
        snapshot = ROOT / manifest.snapshot_pdf
        first = extract_page_facts(snapshot, page_id=config["sample_id"])
        second = extract_page_facts(snapshot, page_id=config["sample_id"])
        facts_by_id[config["sample_id"]] = first
        render = render_page(snapshot, out / "renders" / f"{config['sample_id']}.png")
        write_json(out / "facts" / f"{config['sample_id']}.json", first)
        sample_evidence.append(
            {
                "sample_id": config["sample_id"],
                "classification_path": config["classification_path"],
                "facts_stable": first == second,
                "facts_sha256": canonical_sha256(first),
                "text_object_count": len(first.text_objects),
                "image_object_count": len(first.image_objects),
                "drawing_object_count": len(first.drawing_objects),
                "font_names": sorted({item.font_name for item in first.text_objects}),
                "render": render,
                "upstream_unchanged": source_before == sha256_file(source),
            }
        )
    write_json(ROOT / "samples" / "p2" / "manifest.json", manifests)

    visual_manifest = next(item for item in manifests if item.sample_id == "S2P0080")
    visual_source = ROOT / visual_manifest.snapshot_pdf
    passthrough = passthrough_pdf(workspace_root=ROOT, source_pdf=visual_source, output_pdf=out / "visual_passthrough.pdf")

    source_manifest = next(item for item in manifests if item.sample_id == "S2P0043")
    source_pdf = ROOT / source_manifest.snapshot_pdf
    source_facts = facts_by_id["S2P0043"]
    target = next(item for item in source_facts.text_objects if item.text.startswith("DLC ASIA LIMITED"))
    x0, y0, x1, _y1 = target.bbox
    output_bbox = (x0, y0, x1, min(source_facts.height, y0 + 20.0))
    write = ContainerWrite(
        container_id=target.object_id,
        translated_text="DLC ASIA LIMITED 2025/26 年报 8",
        output_bbox=output_bbox,
        allowed_bbox=output_bbox,
        font_file="C:/Windows/Fonts/simhei.ttf",
        font_resource="p2font",
        font_size=max(6.0, min(8.0, target.font_size)),
        line_height=1.1,
    )
    patch = PagePatch(
        page_id=source_facts.page_id,
        toolbox_key="mechanical.validation",
        writes=(write,),
        source_pdf_sha256=source_facts.source_pdf_sha256,
        page_index=0,
    )
    candidate = out / "mechanical_candidate.pdf"
    patch_result = apply_page_patch(workspace_root=ROOT, source_pdf=source_pdf, candidate_pdf=candidate, facts=source_facts, patch=patch)
    write_json(out / "mechanical_patch.json", patch)
    write_json(out / "mechanical_patch_result.json", patch_result)
    if patch_result.candidate_pdf:
        render_page(candidate, out / "renders" / "mechanical_candidate.png")
        render_contact_sheet(source_pdf, candidate, out / "renders" / "mechanical_comparison.png", clip=write.allowed_bbox, zoom=4.0)

    summary = {
        "kernel_version": "shared-pdf-kernel/v1",
        "tool_probe": probe_tools(),
        "samples": sample_evidence,
        "mechanical_patch": to_jsonable(patch_result),
        "visual_passthrough": passthrough,
        "source_hashes_unchanged": all(item["upstream_unchanged"] for item in sample_evidence),
    }
    write_json(out / "summary.json", summary)
    passed = (
        summary["tool_probe"]["required_ok"]
        and all(item["facts_stable"] for item in sample_evidence)
        and summary["source_hashes_unchanged"]
        and patch_result.status == "APPLIED"
        and patch_result.source_locked_objects_sha256 == patch_result.candidate_locked_objects_sha256
        and passthrough["equivalent"]
    )
    print(out / "summary.json")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
