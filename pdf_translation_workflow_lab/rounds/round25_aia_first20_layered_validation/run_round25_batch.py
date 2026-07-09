import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


CASES = [
    {
        "case_id": "R25_AIA_ZH_TO_EN_pages_001_020",
        "source_pdf": "input/source_pdfs/AIA_2020_Annual_Report_zh_pages_001_020.pdf",
        "translations_json": "AUTO",
        "source_language": "zh",
        "target_language": "en",
        "purpose": "AIA Chinese annual report first 20 pages translated to English.",
    },
    {
        "case_id": "R25_AIA_EN_TO_ZH_pages_001_020",
        "source_pdf": "input/source_pdfs/AIA_2020_Annual_Report_en_pages_001_020.pdf",
        "translations_json": "AUTO",
        "source_language": "en",
        "target_language": "zh",
        "purpose": "AIA English annual report first 20 pages translated to Chinese.",
    },
    {
        "case_id": "R25_REGRESSION_00005_ZH_TO_EN_pages_001_020",
        "source_pdf": "input/source_pdfs/00005_2025_annual_report_zh_pages_001_020.pdf",
        "translations_json": "input/semantic_translations/R22_GEN_ZH_TO_EN_00005_pages_001_020.translations.json",
        "source_language": "zh",
        "target_language": "en",
        "purpose": "Regression run for the 00005 first-20-page sample used by round24.",
    },
]


def copy_runtime_dirs(root: Path, case_dir: Path) -> None:
    for name in ("reports", "output", "previews"):
        src = root / name
        dst = case_dir / name
        if dst.exists():
            shutil.rmtree(dst)
        if src.exists():
            shutil.copytree(src, dst)


def load_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def run_case(root: Path, case: dict[str, str]) -> dict[str, Any]:
    case_dir = root / "case_runs" / case["case_id"]
    case_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        sys.executable,
        "run_round25_layered_case.py",
        "--source-pdf",
        case["source_pdf"],
        "--translations-json",
        case["translations_json"],
        "--source-language",
        case["source_language"],
        "--target-language",
        case["target_language"],
        "--case-id",
        case["case_id"],
    ]
    started = now()
    proc = subprocess.run(
        command,
        cwd=root,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    copy_runtime_dirs(root, case_dir)
    verdict = load_json_if_exists(case_dir / "reports" / "round25_final_verdict.json")
    process = load_json_if_exists(case_dir / "reports" / "process_audit.json")
    record = {
        "case_id": case["case_id"],
        "purpose": case["purpose"],
        "command": command,
        "started_at": started,
        "ended_at": now(),
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-4000:],
        "case_dir": str(case_dir.relative_to(root)),
        "final_verdict": verdict,
        "process_audit": process,
    }
    (case_dir / "round25_case_run_record.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def main() -> None:
    root = Path(__file__).resolve().parent
    case_runs = root / "case_runs"
    if case_runs.exists():
        shutil.rmtree(case_runs)
    case_runs.mkdir(parents=True, exist_ok=True)
    results = [run_case(root, case) for case in CASES]
    summary = {
        "round": "round25",
        "run_started_at": results[0]["started_at"] if results else now(),
        "run_ended_at": now(),
        "case_count": len(results),
        "cases": [
            {
                "case_id": item["case_id"],
                "returncode": item["returncode"],
                "case_dir": item["case_dir"],
                "process_contract_verdict": (item.get("final_verdict") or {}).get("process_contract_verdict"),
                "product_quality_verdict": (item.get("final_verdict") or {}).get("product_quality_verdict"),
                "terminal_state": (item.get("final_verdict") or {}).get("terminal_state"),
                "candidate_pdf": (item.get("final_verdict") or {}).get("candidate_pdf"),
                "repair_accepted": (item.get("final_verdict") or {}).get("repair_accepted"),
                "loop_verdict": (item.get("final_verdict") or {}).get("loop_verdict"),
                "selected_failure_class": (item.get("final_verdict") or {}).get("selected_failure_class"),
            }
            for item in results
        ],
    }
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "round25_batch_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Round25 Batch Summary",
        "",
        "Round25 runs the layered state-machine workflow against AIA zh/en first-20-page PDFs and the round24 00005 regression sample.",
        "",
        "| Case | Process | Product | Terminal | Loop | Repair accepted | Candidate |",
        "|---|---|---|---|---|---|---|",
    ]
    for case in summary["cases"]:
        lines.append(
            f"| `{case['case_id']}` | `{case['process_contract_verdict']}` | `{case['product_quality_verdict']}` | `{case['terminal_state']}` | `{case['loop_verdict']}` | `{case['repair_accepted']}` | `{case['candidate_pdf']}` |"
        )
    (reports / "round25_batch_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if any(item["returncode"] != 0 for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
