"""可断点续跑的 body.flow_text.single 全量批次运行器。"""

from __future__ import annotations

import argparse
import json
import shutil
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from page_toolbox_puncture.contracts import write_json
from page_toolbox_puncture.translation import QwenConfig, QwenPageTranslationProvider

from ..p4_batch import finalize_p4_batch, initialize_p4_batch, publish_p4_case
from ..p4_engine import run_p4_page


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[6]
    toolbox_root = Path(__file__).resolve().parents[2]
    run_root = toolbox_root / "runs" / args.run_id
    rows = _load_rows(toolbox_root / "samples" / "p4_all_manifest.jsonl")

    if not run_root.exists():
        initialize_p4_batch(
            run_root=run_root,
            toolbox_root=toolbox_root,
            run_id=args.run_id,
            phase="all-single",
            rows=rows,
            model="Qwen/Qwen3.6-35B-A3B",
        )
    write_json(
        run_root / "input" / "all_single_selection_manifest.json",
        {
            "schema_version": "p4-all-single-selection/v1",
            "page_count": len(rows),
            "holdout_accessed": True,
            "source_language": dict(Counter(str(row["source_language"]) for row in rows)),
            "density_band": dict(Counter(str(row["density_band"]) for row in rows)),
            "rows": rows,
        },
    )

    prompts = {
        "zh-CN": (toolbox_root / "prompts" / "page_translation.en-zh.zh-CN.md").read_text(encoding="utf-8"),
        "en": (toolbox_root / "prompts" / "page_translation.zh-en.zh-CN.md").read_text(encoding="utf-8"),
    }
    config = QwenConfig.from_environment()
    lock = threading.Lock()
    started = time.time()
    results: dict[str, dict[str, object]] = {}

    # 已完成的 case 直接复用；中断后再次启动不会重复调用千问。
    for row in rows:
        page_id = str(row["sample_id"])
        result_path = run_root / "cases" / page_id / "reports" / "run_result.json"
        if result_path.is_file():
            results[page_id] = json.loads(result_path.read_text(encoding="utf-8"))

    pending = [row for row in rows if str(row["sample_id"]) not in results]
    _write_progress(run_root, rows, results, started, active_count=0)
    print(f"RESUME completed={len(results)} pending={len(pending)} workers={args.workers}", flush=True)

    def run_one(row: dict[str, object]) -> dict[str, object]:
        page_id = str(row["sample_id"])
        case_root = run_root / "cases" / page_id
        # 只有缺少最终 run_result 的残缺 case 才归档，保留全部失败现场。
        if case_root.exists():
            archive = run_root / "reports" / "interrupted_cases" / f"{page_id}-{time.time_ns()}"
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(case_root), str(archive))

        attempt = 0
        while True:
            attempt += 1
            provider = QwenPageTranslationProvider(config, prompts[str(row["target_language"])])
            result = run_p4_page(
                source_pdf=run_root / "input" / "source_pdfs" / f"{page_id}_source.pdf",
                page_id=page_id,
                run_dir=case_root,
                provider=provider,
                font_file="C:/Windows/Fonts/msyh.ttc",
                source_language=str(row["source_language"]),
                target_language=str(row["target_language"]),
            )
            if attempt == 1 and _is_transient_qwen_failure(case_root):
                archive = run_root / "reports" / "transient_retries" / f"{page_id}-attempt-{attempt}"
                archive.parent.mkdir(parents=True, exist_ok=True)
                if archive.exists():
                    shutil.rmtree(archive)
                shutil.move(str(case_root), str(archive))
                continue
            publish_p4_case(run_root=run_root, page_id=page_id, case_root=case_root)
            return result.__dict__

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(run_one, row): str(row["sample_id"]) for row in pending}
        for future in as_completed(futures):
            page_id = futures[future]
            try:
                payload = future.result()
            except Exception as exc:  # 批次运行器自身错误也必须落盘，不能悄悄丢页。
                payload = {
                    "page_id": page_id,
                    "candidate_pdf": None,
                    "process_verdict": "FAIL",
                    "product_verdict": "NOT_REACHED",
                    "terminal_state": "P4_BATCH_RUNNER_FAILED",
                    "failure_owner": "full_batch_runner",
                    "selected_profile_id": None,
                    "error": f"{type(exc).__name__}:{exc}",
                }
            with lock:
                results[page_id] = payload
                _write_progress(run_root, rows, results, started, active_count=max(0, len(rows) - len(results)))
                print(
                    f"PROGRESS {len(results)}/{len(rows)} {page_id} "
                    f"process={payload['process_verdict']} product={payload['product_verdict']} "
                    f"state={payload['terminal_state']}",
                    flush=True,
                )

    ordered = [results[str(row["sample_id"])] for row in rows]
    summary = {
        "schema_version": "p4-all-single-summary/v1",
        "run_id": args.run_id,
        "page_count": len(rows),
        "elapsed_seconds": round(time.time() - started, 2),
        "process_pass_count": sum(item["process_verdict"] == "PASS" for item in ordered),
        "mechanical_product_pass_count": sum(item["product_verdict"] == "PASS" for item in ordered),
        "failure_count": sum(item["process_verdict"] != "PASS" or item["product_verdict"] != "PASS" for item in ordered),
        "visual_adjudication_status": "USER_REVIEW_PENDING",
        "results": ordered,
    }
    finalize_p4_batch(run_root, summary)
    _write_progress(run_root, rows, results, started, active_count=0, complete=True)
    print(json.dumps({key: summary[key] for key in ("page_count", "elapsed_seconds", "process_pass_count", "mechanical_product_pass_count", "failure_count")}), flush=True)
    return 0


def _load_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _is_transient_qwen_failure(case_root: Path) -> bool:
    decision_path = case_root / "reports" / "quality_decision.json"
    if not decision_path.is_file():
        return False
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    messages = " ".join(str(item.get("message") or "") for item in decision.get("findings", []))
    return any(token in messages for token in ("QWEN_TIMEOUT", "QWEN_HTTP_429", "QWEN_HTTP_500", "QWEN_HTTP_502", "QWEN_HTTP_503", "QWEN_HTTP_504", "QWEN_CLIENT_"))


def _write_progress(
    run_root: Path,
    rows: list[dict[str, object]],
    results: dict[str, dict[str, object]],
    started: float,
    *,
    active_count: int,
    complete: bool = False,
) -> None:
    write_json(
        run_root / "reports" / "live_progress.json",
        {
            "schema_version": "p4-all-single-live-progress/v1",
            "page_count": len(rows),
            "completed_count": len(results),
            "remaining_count": len(rows) - len(results),
            "active_or_queued_count": active_count,
            "process_pass_count": sum(item.get("process_verdict") == "PASS" for item in results.values()),
            "mechanical_product_pass_count": sum(item.get("product_verdict") == "PASS" for item in results.values()),
            "elapsed_seconds": round(time.time() - started, 2),
            "complete": complete,
            "completed_page_ids": sorted(results),
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
