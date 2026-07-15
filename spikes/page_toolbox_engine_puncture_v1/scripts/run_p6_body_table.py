from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLBOX_ROOT = ROOT / "toolboxes" / "body" / "table"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from page_toolbox_puncture.contracts import PageTranslationBundle, TranslationResult, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import QwenConfig, QwenPageTranslationProvider
from toolboxes.body.table.tools.engine import run_p6_page


class RecordedTranslationProvider:
    provider_name = "recorded"
    model_name = "p6-recorded-bundle"

    def __init__(self, bundle_path: Path) -> None:
        self.bundle_path = bundle_path

    def translate(self, request):
        value = json.loads(self.bundle_path.read_text(encoding="utf-8"))
        bundle = PageTranslationBundle(
            request_id=request.request_id,
            page_id=request.page_id,
            provider=self.provider_name,
            model=str(value.get("model") or self.model_name),
            translations=tuple(
                TranslationResult(str(item["container_id"]), str(item["translated_text"]))
                for item in value["translations"]
            ),
            response_sha256=value.get("response_sha256"),
        )
        bundle.validate_against(request)
        return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Run P6 body.table page packages")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--sample-id", action="append")
    selection.add_argument("--initial", action="store_true", help="development plus three initial regression pages")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--provider", choices=("qwen", "recorded"), default="qwen")
    parser.add_argument("--recorded-run", type=Path)
    parser.add_argument("--allow-holdout", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--font-file", default="C:/Windows/Fonts/msyh.ttc")
    parser.add_argument("--bold-font-file", default="C:/Windows/Fonts/msyhbd.ttc")
    args = parser.parse_args()

    records = _read_manifest(TOOLBOX_ROOT / "samples" / "manifest.jsonl")
    if args.sample_id:
        requested = set(args.sample_id)
        selected = [record for record in records if record["sample_id"] in requested]
        missing = requested - {record["sample_id"] for record in selected}
        if missing:
            parser.error("unknown sample IDs: " + ",".join(sorted(missing)))
    elif args.initial:
        selected = [record for record in records if record.get("validation_phase") == "initial"]
    else:
        selected = records
    if any(record["split"] == "holdout" for record in selected) and not args.allow_holdout:
        parser.error("holdout selection requires --allow-holdout after workflow freeze")
    if args.provider == "recorded" and args.recorded_run is None:
        parser.error("--provider recorded requires --recorded-run")

    run_id = args.run_id or datetime.now(timezone.utc).strftime("p6-%Y%m%dT%H%M%SZ")
    run_root = TOOLBOX_ROOT / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    (run_root / "cases").mkdir()
    (run_root / "reports").mkdir()
    (run_root / "input").mkdir()
    shutil.copy2(TOOLBOX_ROOT / "samples" / "manifest.jsonl", run_root / "input" / "sample_manifest.jsonl")
    write_json(
        run_root / "input" / "selection.json",
        {
            "toolbox_key": "body.table",
            "run_id": run_id,
            "provider": args.provider,
            "sample_ids": [record["sample_id"] for record in selected],
            "holdout_accessed": any(record["split"] == "holdout" for record in selected),
        },
    )

    results = []
    for record in selected:
        sample_id = record["sample_id"]
        source = TOOLBOX_ROOT / record["source_ref"]
        if sha256_file(source) != record["sha256"]:
            raise RuntimeError(f"sample_hash_mismatch:{sample_id}")
        if args.provider == "qwen":
            prompt_name = "page_translation.en-zh.zh-CN.md" if record["source_language"] == "en" else "page_translation.zh-en.zh-CN.md"
            provider = QwenPageTranslationProvider(
                QwenConfig.from_environment(),
                (TOOLBOX_ROOT / "prompts" / prompt_name).read_text(encoding="utf-8"),
            )
        else:
            bundle_path = args.recorded_run / "cases" / sample_id / "output" / "translation_bundle.json"
            provider = RecordedTranslationProvider(bundle_path)
        result = run_p6_page(
            source_pdf=source,
            page_id=sample_id,
            run_dir=run_root / "cases" / sample_id,
            provider=provider,
            font_file=args.font_file,
            bold_font_file=args.bold_font_file,
            source_language=record["source_language"],
            target_language=record["target_language"],
        )
        results.append(result)
        print(
            json.dumps(
                {
                    "sample_id": sample_id,
                    "process": result.process_verdict,
                    "product": result.product_verdict,
                    "state": result.terminal_state,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    summary = {
        "schema_version": "p6-body-table-batch/v1",
        "toolbox_key": "body.table",
        "run_id": run_id,
        "provider": args.provider,
        "sample_count": len(results),
        "process_pass_count": sum(result.process_verdict == "PASS" for result in results),
        "product_pass_count": sum(result.product_verdict == "PASS" for result in results),
        "terminal_states": {state: sum(result.terminal_state == state for result in results) for state in sorted({result.terminal_state for result in results})},
        "results": results,
    }
    write_json(run_root / "reports" / "batch_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, default=lambda value: value.__dict__, indent=2))
    return 0 if all(result.process_verdict == "PASS" for result in results) else 2


def _read_manifest(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
