import argparse
import json
from collections import Counter
from pathlib import Path


def plan(quality_gates: Path, output: Path, loop_index: int) -> None:
    gates = json.loads(quality_gates.read_text(encoding="utf-8"))
    failures = gates.get("blocking_failures", [])
    repair_counts = Counter(item.get("repair_family", "expand_or_reflow_slot") for item in failures)
    selected = repair_counts.most_common(1)[0][0] if repair_counts else None
    result = {
        "tool": "plan_repairs",
        "loop_index": loop_index,
        "selected_repair_family": selected,
        "blocking_failure_count": len(failures),
        "failures": failures,
        "verdict": "NO_REPAIR_NEEDED" if not selected else "REPAIR_SELECTED",
        "notes": "Round22 currently records repair selection but does not auto-edit tools. Tool edits remain explicit experimental changes.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quality-gates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--loop-index", type=int, default=0)
    args = parser.parse_args()
    plan(args.quality_gates, args.output, args.loop_index)


if __name__ == "__main__":
    main()
