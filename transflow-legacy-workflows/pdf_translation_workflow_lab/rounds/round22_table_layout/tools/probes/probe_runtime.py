import json
import sys
from importlib.util import find_spec
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    report = {
        "tool": "probe_runtime",
        "python": sys.version,
        "package_root": str(root),
        "dependencies": {
            "fitz": find_spec("fitz") is not None,
            "PIL": find_spec("PIL") is not None,
        },
        "verdict": "PASS" if find_spec("fitz") and find_spec("PIL") else "FAIL",
    }
    out = root / "reports" / "tool_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if report["verdict"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
