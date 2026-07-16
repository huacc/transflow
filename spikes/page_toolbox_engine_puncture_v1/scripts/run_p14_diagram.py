from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLBOX_ROOT = ROOT / "toolboxes" / "body" / "diagram"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from page_toolbox_puncture.contracts import PageTranslationBundle, TranslationResult, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, QwenConfig, QwenPageTranslationProvider
from toolboxes.body.diagram.tools.engine import run_p14_page


class FixedGeometryProvider:
    provider_name = "fixed"
    model_name = "p14-fixed-geometry-fixture"

    def translate(self, request):
        results = []
        for index, unit in enumerate(request.units):
            semantic = f"示意图文本{index + 1}" if request.target_language.startswith("zh") else f"Diagram text {index + 1}"
            translated = " ".join((*unit.required_literals, semantic)).strip()
            results.append(TranslationResult(unit.container_id, translated))
        return PageTranslationBundle(request.request_id, request.page_id, self.provider_name, self.model_name, tuple(results))


class RecordedTranslationProvider:
    provider_name = "recorded"
    model_name = "p14-recorded-bundle"

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


class UnavailableQwenProvider:
    provider_name = "qwen"
    model_name = "unavailable"

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code

    def translate(self, request):
        raise ProviderError(self.error_code)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run P14 body.diagram page packages")
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
    parser.add_argument("--font-candidate", action="append", default=[])
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")

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

    run_id = args.run_id or datetime.now(timezone.utc).strftime("p14-%Y%m%dT%H%M%SZ")
    run_root = TOOLBOX_ROOT / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    for name in ("cases", "reports", "input"):
        (run_root / name).mkdir()
    shutil.copy2(TOOLBOX_ROOT / "samples" / "manifest.jsonl", run_root / "input" / "sample_manifest.jsonl")
    write_json(
        run_root / "input" / "selection.json",
        {
            "toolbox_key": "body.diagram",
            "run_id": run_id,
            "provider": args.provider,
            "font_candidates": [args.font_file, args.bold_font_file, *args.font_candidate],
            "sample_ids": [record["sample_id"] for record in selected],
            "holdout_accessed": any(record["split"] == "holdout" for record in selected),
            "holdout_integrity": "NON_BLIND_PREVIEWED_BEFORE_FREEZE",
            "cadence_exception": "USER_DIRECTED_P14_WHILE_P12_GATE_IS_FAIL",
        },
    )

    qwen_config = None
    qwen_error = None
    if args.provider == "qwen":
        try:
            qwen_config = QwenConfig.from_environment()
        except ProviderError as exc:
            qwen_error = exc.code

    results = []
    started = time.perf_counter()
    selected_order = {str(record["sample_id"]): index for index, record in enumerate(selected)}

    def record_progress(row):
        results.append(row)
        results.sort(key=lambda item: selected_order[str(item["sample_id"])])
        write_json(
            run_root / "reports" / "progress.json",
            {
                "schema_version": "p14-body-diagram-progress/v1",
                "run_id": run_id,
                "completed_count": len(results),
                "sample_count": len(selected),
                "last_sample_id": row["sample_id"],
                "last_terminal_state": row["terminal_state"],
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "results": results,
            },
        )
        print(
            json.dumps(
                {
                    "sample_id": row["sample_id"],
                    "mode": row["mode"],
                    "process": row["process_verdict"],
                    "product": row["product_verdict"],
                    "state": row["terminal_state"],
                    "nodes": row["node_count"],
                    "connectors": row["connector_count"],
                    "containers": row["container_count"],
                    "elapsed_seconds": row["elapsed_seconds"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    if args.workers == 1:
        for record in selected:
            record_progress(
                _execute_record(
                    record,
                    args.provider,
                    args.recorded_run,
                    qwen_config,
                    qwen_error,
                    run_root,
                    args.font_file,
                    args.bold_font_file,
                    tuple(args.font_candidate),
                )
            )
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = [
                executor.submit(
                    _execute_record,
                    record,
                    args.provider,
                    args.recorded_run,
                    qwen_config,
                    qwen_error,
                    run_root,
                    args.font_file,
                    args.bold_font_file,
                    tuple(args.font_candidate),
                )
                for record in selected
            ]
            for future in as_completed(futures):
                record_progress(future.result())

    accepted_states = {"PAGE_PASSED", "PASSTHROUGH_PASSED"}
    states = sorted({row["terminal_state"] for row in results})
    summary = {
        "schema_version": "p14-body-diagram-batch/v1",
        "toolbox_key": "body.diagram",
        "run_id": run_id,
        "provider": args.provider,
        "sample_count": len(results),
        "process_pass_count": sum(row["process_verdict"] == "PASS" for row in results),
        "product_pass_count": sum(row["product_verdict"] == "PASS" for row in results),
        "accepted_count": sum(row["terminal_state"] in accepted_states for row in results),
        "translated_page_count": sum(row["mode"] == "translated" for row in results),
        "passthrough_page_count": sum(row["mode"] == "passthrough" for row in results),
        "terminal_states": {state: sum(row["terminal_state"] == state for row in results) for state in states},
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "holdout_integrity": "NON_BLIND_PREVIEWED_BEFORE_FREEZE",
        "cadence_exception": "USER_DIRECTED_P14_WHILE_P12_GATE_IS_FAIL",
        "results": results,
    }
    write_json(run_root / "reports" / "batch_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(row["terminal_state"] in accepted_states for row in results) else 2


def _execute_record(
    record,
    provider_kind,
    recorded_run,
    qwen_config,
    qwen_error,
    run_root,
    font_file,
    bold_font_file,
    font_candidates,
):
    sample_started = time.perf_counter()
    sample_id = str(record["sample_id"])
    source = TOOLBOX_ROOT / str(record["source_ref"])
    if sha256_file(source) != record["sha256"]:
        raise RuntimeError(f"sample_hash_mismatch:{sample_id}")
    provider = _provider(provider_kind, sample_id, record, recorded_run, qwen_config, qwen_error)
    result = run_p14_page(
        source_pdf=source,
        page_id=sample_id,
        run_dir=run_root / "cases" / sample_id,
        provider=provider,
        font_file=font_file,
        bold_font_file=bold_font_file,
        font_candidates=font_candidates,
        source_language=str(record["source_language"]),
        target_language=str(record["target_language"]),
    )
    return {
        **result.__dict__,
        "sample_id": sample_id,
        "split": record["split"],
        "elapsed_seconds": round(time.perf_counter() - sample_started, 3),
    }


def _provider(kind, sample_id, record, recorded_run, qwen_config, qwen_error):
    if kind == "fixed":
        return FixedGeometryProvider()
    if kind == "recorded":
        return RecordedTranslationProvider(recorded_run / "cases" / sample_id / "output" / "translation_bundle.json")
    if qwen_error:
        return UnavailableQwenProvider(qwen_error)
    prompt_name = "page_translation.en-zh.zh-CN.md" if str(record["source_language"]).startswith("en") else "page_translation.zh-en.zh-CN.md"
    return QwenPageTranslationProvider(qwen_config, (TOOLBOX_ROOT / "prompts" / prompt_name).read_text(encoding="utf-8"))


def _read_manifest(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
