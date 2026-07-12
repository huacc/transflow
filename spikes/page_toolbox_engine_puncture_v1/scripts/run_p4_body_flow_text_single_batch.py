from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from page_toolbox_puncture.translation import ProviderError, QwenConfig, QwenPageTranslationProvider
from toolboxes.body.flow_text.single.tools.p4_batch import finalize_p4_batch, initialize_p4_batch, load_p4_manifest, publish_p4_case
from toolboxes.body.flow_text.single.tools.p4_engine import P4RunResult, run_p4_page


TOOLBOX_ROOT = ROOT / "toolboxes" / "body" / "flow_text" / "single"


class RetryingProvider:
    def __init__(self, provider: QwenPageTranslationProvider, retries: int = 3) -> None:
        self.provider = provider
        self.retries = retries
        self.provider_name = provider.provider_name
        self.model_name = provider.model_name

    def translate(self, request):
        last: ProviderError | None = None
        for attempt in range(self.retries):
            try:
                return self.provider.translate(request)
            except ProviderError as exc:
                last = exc
                if attempt + 1 < self.retries:
                    time.sleep(1.5 * (attempt + 1))
        raise last or ProviderError("P4_QWEN_RETRY_EXHAUSTED")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one P4 phase for body.flow_text.single")
    parser.add_argument("--phase", choices=("development", "regression", "holdout"), required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--font-file", default="C:/Windows/Fonts/msyh.ttc")
    parser.add_argument("--workflow-frozen", action="store_true")
    parser.add_argument("--sample-ids", help="Comma-separated subset within the selected phase")
    parser.add_argument("--resume", action="store_true", help="Reuse completed cases in an existing run and rerun only incomplete cases")
    args = parser.parse_args()
    if args.phase == "holdout" and not args.workflow_frozen:
        raise SystemExit("holdout_requires_--workflow-frozen")

    rows = load_p4_manifest(TOOLBOX_ROOT / "samples" / "p4_all_manifest.jsonl", args.phase)
    if args.sample_ids:
        requested = {value.strip() for value in args.sample_ids.split(",") if value.strip()}
        rows = [row for row in rows if row["sample_id"] in requested]
        missing = requested - {str(row["sample_id"]) for row in rows}
        if missing:
            raise SystemExit("sample_ids_not_in_phase:" + ",".join(sorted(missing)))
    config = QwenConfig.from_environment()
    providers = {
        "en": RetryingProvider(QwenPageTranslationProvider(config, (TOOLBOX_ROOT / "prompts" / "page_translation.en-zh.zh-CN.md").read_text(encoding="utf-8"))),
        "zh": RetryingProvider(QwenPageTranslationProvider(config, (TOOLBOX_ROOT / "prompts" / "page_translation.zh-en.zh-CN.md").read_text(encoding="utf-8"))),
    }
    run_id = args.run_id or f"p4-{args.phase}-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = TOOLBOX_ROOT / "runs" / run_id
    if args.resume:
        if not run_root.is_dir():
            raise SystemExit(f"resume_run_not_found:{run_root}")
    else:
        initialize_p4_batch(run_root=run_root, toolbox_root=TOOLBOX_ROOT, run_id=run_id, phase=args.phase, rows=rows, model=config.model)
    lock = threading.Lock()
    completed = 0

    def execute(row: dict[str, object]) -> P4RunResult:
        page_id = str(row["sample_id"])
        source_language = str(row["source_language"])
        target_language = str(row["target_language"])
        result = run_p4_page(
            source_pdf=Path(str(row["source_ref"])),
            page_id=page_id,
            run_dir=run_root / "cases" / page_id,
            provider=providers[source_language],
            font_file=args.font_file,
            source_language=source_language,
            target_language=target_language,
        )
        publish_p4_case(run_root=run_root, page_id=page_id, case_root=run_root / "cases" / page_id)
        return result

    results = []
    pending_rows = []
    for row in rows:
        page_id = str(row["sample_id"])
        result_path = run_root / "cases" / page_id / "reports" / "run_result.json"
        if args.resume and result_path.is_file():
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            results.append(P4RunResult(**payload))
            publish_p4_case(run_root=run_root, page_id=page_id, case_root=run_root / "cases" / page_id)
        else:
            case_root = run_root / "cases" / page_id
            if args.resume and case_root.exists():
                shutil.rmtree(case_root)
            pending_rows.append(row)
    completed = len(results)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(execute, row): row for row in pending_rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = P4RunResult(str(row["sample_id"]), str(run_root / "cases" / str(row["sample_id"])), None, "FAIL", "NOT_REACHED", "P4_CAPABILITY_FAILED", type(exc).__name__, None)
            results.append(result)
            with lock:
                completed += 1
                print(f"[{completed}/{len(rows)}] {result.page_id} {result.process_verdict}/{result.product_verdict} {result.selected_profile_id or '-'}", flush=True)

    results.sort(key=lambda item: item.page_id)
    summary = {
        "schema_version": "p4-batch-summary/v1",
        "run_id": run_id,
        "phase": args.phase,
        "toolbox_key": "body.flow_text.single",
        "provider": "qwen",
        "model": config.model,
        "workers": args.workers,
        "page_count": len(results),
        "process_pass_count": sum(item.process_verdict == "PASS" for item in results),
        "product_pass_count": sum(item.product_verdict == "PASS" for item in results),
        "product_fail_count": sum(item.product_verdict == "FAIL" for item in results),
        "capability_fail_count": sum(item.terminal_state == "P4_CAPABILITY_FAILED" for item in results),
        "holdout_accessed": args.phase == "holdout",
        "pages": [item.__dict__ for item in results],
    }
    finalize_p4_batch(run_root, summary)
    print(json.dumps({key: value for key, value in summary.items() if key != "pages"}, ensure_ascii=False, indent=2))
    return 0 if summary["process_pass_count"] == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
