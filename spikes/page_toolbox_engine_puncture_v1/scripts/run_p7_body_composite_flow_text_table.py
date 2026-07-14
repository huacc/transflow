from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from page_toolbox_puncture.translation import QwenConfig, QwenPageTranslationProvider
from toolboxes.body.composite.flow_text_table.tools.engine import run_p7_page


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = (
    ROOT
    / "toolboxes"
    / "body"
    / "composite"
    / "flow_text_table"
    / "prompts"
    / "page_translation.zh-CN.md"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one P7 body.composite.flow_text_table page")
    parser.add_argument("source_pdf", type=Path)
    parser.add_argument("page_id")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--source-language", default="en")
    parser.add_argument("--target-language", default="zh-CN")
    parser.add_argument("--font-file", default="C:/Windows/Fonts/msyh.ttc")
    parser.add_argument("--bold-font-file", default="C:/Windows/Fonts/msyhbd.ttc")
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    args = parser.parse_args()

    provider = QwenPageTranslationProvider(
        QwenConfig.from_environment(),
        args.prompt.read_text(encoding="utf-8"),
    )
    result = run_p7_page(
        source_pdf=args.source_pdf,
        page_id=args.page_id,
        run_dir=args.run_dir,
        provider=provider,
        font_file=args.font_file,
        bold_font_file=args.bold_font_file,
        source_language=args.source_language,
        target_language=args.target_language,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0 if result.terminal_state == "PAGE_PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
