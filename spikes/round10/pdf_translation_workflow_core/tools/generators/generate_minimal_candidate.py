"""Generate a minimal candidate PDF to exercise gates.

tool_name: generate_minimal_candidate
category: generators
input_contract: source PDF path and output PDF path
output_contract: candidate PDF copied from source plus generation evidence
failure_signals: source cannot be opened or output cannot be written
fallback: mark S_FAIL_TOOLING
anti_overfit_statement: this is a generic smoke-test generator; it does not translate, redline, or branch on sample identity
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import ensure_dir, rel, resolve_workspace_path, sha256_file, write_json  # noqa: E402


def generate(source: Path, output: Path) -> dict:
    ensure_dir(output.parent)
    doc = fitz.open(source)
    doc.save(output, garbage=4, deflate=True)
    doc.close()
    return {
        "tool": "generate_minimal_candidate",
        "strategy": "copy_source_as_candidate_for_gate_exercise",
        "not_a_translation_engine": True,
        "input_pdf": rel(source),
        "output_pdf": rel(output),
        "output_sha256": sha256_file(output),
        "expected_quality": "fail_text_residue_in_product_quality_mode",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    result = generate(resolve_workspace_path(args.input), Path(args.output))
    write_json(Path(args.evidence), result)
    print(args.evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
