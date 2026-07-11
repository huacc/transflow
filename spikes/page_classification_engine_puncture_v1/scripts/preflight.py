from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from page_classifier.config import NODE_PROMPTS, PROVIDER
from page_classifier.evidence import build_evidence, compact_evidence
from page_classifier.io_utils import read_jsonl, write_json
from page_classifier.qwen import QwenJudge


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-dir", default="样本1")
    parser.add_argument("--source-manifest", default="manifests/source_manifest.jsonl")
    args = parser.parse_args()
    sample_root = (ROOT / args.sample_dir).resolve()
    source = read_jsonl((ROOT / args.source_manifest).resolve())[0]
    prompt_text = {
        path: (ROOT / path).read_text(encoding="utf-8")
        for node in NODE_PROMPTS.values()
        for path in node.values()
    }
    preflight_root = ROOT / "artifacts" / "preflight"
    image_path = preflight_root / "P0001.png"
    sample_id = source["sample_id"]
    evidence = build_evidence(sample_root / f"{sample_id}.pdf", source, image_path)
    judge = QwenJudge(ROOT, prompt_text)
    judgement, result, payload_hash, prompt_path = judge.decide(
        node_key="page.role",
        stage="PRIMARY",
        sample_id=sample_id,
        evidence=evidence,
        compact_evidence=compact_evidence(evidence),
        page_image=image_path,
    )
    output = {
        "PREFLIGHT_READY": result.http_status == 200 and result.error_code is None and judgement.status in {"DECIDED", "INCONCLUSIVE"},
        "provider": {"base_url": PROVIDER.base_url, "model": PROVIDER.model},
        "http_status": result.http_status,
        "error_code": result.error_code,
        "reported_model": result.reported_model,
        "finish_reason": result.finish_reason,
        "judgement": judgement.as_dict(),
        "payload_sha256": payload_hash,
        "prompt_path": prompt_path,
        "key_persisted": False,
    }
    write_json(preflight_root / "preflight.json", output)
    print(json.dumps(output, ensure_ascii=False))
    if not output["PREFLIGHT_READY"]:
        raise RuntimeError("preflight_failed")


if __name__ == "__main__":
    main()
