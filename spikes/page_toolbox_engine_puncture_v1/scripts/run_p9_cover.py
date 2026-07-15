from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLBOX_ROOT = ROOT / "toolboxes" / "cover"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from page_toolbox_puncture.contracts import PageTranslationBundle, TranslationResult, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, QwenConfig, QwenPageTranslationProvider
from toolboxes.cover.tools.engine import run_p9_page


class RecordedTranslationProvider:
    provider_name = "recorded"
    model_name = "p9-recorded-bundle"

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


class FixedGeometryProvider:
    provider_name = "fixed"
    model_name = "p9-fixed-geometry-fixture"

    def translate(self, request):
        results = []
        for index, unit in enumerate(request.units):
            semantic = f"封面译文{index + 1}" if request.target_language.startswith("zh") else f"Translated cover text {index + 1}"
            translated = " ".join((*unit.required_literals, semantic))
            results.append(TranslationResult(unit.container_id, translated))
        return PageTranslationBundle(
            request_id=request.request_id,
            page_id=request.page_id,
            provider=self.provider_name,
            model=self.model_name,
            translations=tuple(results),
        )


class UnavailableQwenProvider:
    provider_name = "qwen"
    model_name = "unavailable"

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code

    def translate(self, request):
        raise ProviderError(self.error_code)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run P9 cover page packages")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--sample-id", action="append")
    selection.add_argument("--initial", action="store_true")
    selection.add_argument("--initial-expansion", action="store_true")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--provider", choices=("qwen", "recorded", "fixed"), default="qwen")
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
    elif args.initial_expansion:
        selected = [record for record in records if record.get("validation_phase") == "initial_expansion"]
    else:
        selected = records
    if any(record["split"] == "holdout" for record in selected) and not args.allow_holdout:
        parser.error("holdout selection requires --allow-holdout")
    if args.provider == "recorded" and args.recorded_run is None:
        parser.error("--provider recorded requires --recorded-run")

    run_id = args.run_id or datetime.now(timezone.utc).strftime("p9-%Y%m%dT%H%M%SZ")
    run_root = TOOLBOX_ROOT / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    (run_root / "cases").mkdir()
    (run_root / "reports").mkdir()
    (run_root / "input").mkdir()
    shutil.copy2(TOOLBOX_ROOT / "samples" / "manifest.jsonl", run_root / "input" / "sample_manifest.jsonl")
    write_json(
        run_root / "input" / "selection.json",
        {
            "toolbox_key": "cover",
            "run_id": run_id,
            "provider": args.provider,
            "sample_ids": [record["sample_id"] for record in selected],
            "holdout_accessed": any(record["split"] == "holdout" for record in selected),
            "holdout_status": "NON_BLIND" if any(record["split"] == "holdout" for record in selected) else "NOT_ACCESSED",
        },
    )

    qwen_config = None
    qwen_error_code = None
    if args.provider == "qwen":
        try:
            qwen_config = QwenConfig.from_environment()
        except ProviderError as exc:
            qwen_error_code = exc.code
    results = []
    for record in selected:
        sample_id = str(record["sample_id"])
        source = TOOLBOX_ROOT / str(record["source_ref"])
        if sha256_file(source) != record["sha256"]:
            raise RuntimeError(f"sample_hash_mismatch:{sample_id}")
        if args.provider == "qwen":
            if qwen_error_code:
                provider = UnavailableQwenProvider(qwen_error_code)
            else:
                prompt_name = (
                    "page_translation.en-zh.zh-CN.md"
                    if str(record["source_language"]).startswith("en")
                    else "page_translation.zh-en.zh-CN.md"
                )
                provider = QwenPageTranslationProvider(
                    qwen_config,
                    (TOOLBOX_ROOT / "prompts" / prompt_name).read_text(encoding="utf-8"),
                )
        elif args.provider == "recorded":
            provider = RecordedTranslationProvider(
                args.recorded_run / "cases" / sample_id / "output" / "translation_bundle.json"
            )
        else:
            provider = FixedGeometryProvider()
        result = run_p9_page(
            source_pdf=source,
            page_id=sample_id,
            run_dir=run_root / "cases" / sample_id,
            provider=provider,
            font_file=args.font_file,
            bold_font_file=args.bold_font_file,
            source_language=str(record["source_language"]),
            target_language=str(record["target_language"]),
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

    accepted_states = {"PAGE_PASSED", "VISUAL_ONLY_PASSED", "ALREADY_TARGET_PASSED"}
    summary = {
        "schema_version": "p9-cover-batch/v1",
        "toolbox_key": "cover",
        "run_id": run_id,
        "provider": args.provider,
        "sample_count": len(results),
        "process_pass_count": sum(result.process_verdict == "PASS" for result in results),
        "translated_page_pass_count": sum(result.terminal_state == "PAGE_PASSED" for result in results),
        "visual_only_passthrough_count": sum(result.terminal_state == "VISUAL_ONLY_PASSED" for result in results),
        "already_target_passthrough_count": sum(result.terminal_state == "ALREADY_TARGET_PASSED" for result in results),
        "accepted_count": sum(result.terminal_state in accepted_states for result in results),
        "terminal_states": {
            state: sum(result.terminal_state == state for result in results)
            for state in sorted({result.terminal_state for result in results})
        },
        "results": results,
    }
    write_json(run_root / "reports" / "batch_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, default=lambda value: value.__dict__, indent=2))
    return 0 if all(result.terminal_state in accepted_states for result in results) else 2


def _read_manifest(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
