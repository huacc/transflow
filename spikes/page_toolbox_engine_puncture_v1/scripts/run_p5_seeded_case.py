from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from page_toolbox_puncture.contracts import (
    PageTranslationBundle,
    PageTranslationRequest,
    write_json,
)
from page_toolbox_puncture.translation import (
    QwenConfig,
    QwenPageTranslationProvider,
)
from shared_pdf_kernel.render import render_contact_sheet, render_page
from toolboxes.body.flow_text.multi.tools.engine import P5RunResult, run_p5_page
from toolboxes.body.flow_text.multi.tools.layout_pattern import (
    QwenLayoutPatternAdjudicator,
)
from toolboxes.body.flow_text.multi.tools.typography_adjudication import QwenTypographyAdjudicator


class SeededThenQwenProvider:
    """首轮复用已审计译文；只有单容器定向重试才重新请求千问。"""

    provider_name = "qwen-seeded-targeted-retry"

    def __init__(
        self,
        seed: PageTranslationBundle | None,
        initial_live: QwenPageTranslationProvider,
        retry_live: QwenPageTranslationProvider,
        use_seed: bool = True,
    ) -> None:
        if use_seed and seed is None:
            raise ValueError("seed_bundle_required_when_seed_reuse_is_enabled")
        self._seed = seed
        self._seed_by_id = {item.container_id: item for item in seed.translations} if seed else {}
        self._initial_live = initial_live
        self._retry_live = retry_live
        self._use_seed = use_seed
        self.model_name = initial_live.model_name

    def translate(self, request: PageTranslationRequest) -> PageTranslationBundle:
        if self._use_seed and len(request.units) > 1 and all(item.container_id in self._seed_by_id for item in request.units):
            assert self._seed is not None
            bundle = PageTranslationBundle(
                request_id=request.request_id,
                page_id=request.page_id,
                provider=self.provider_name,
                model=self.model_name,
                translations=tuple(self._seed_by_id[item.container_id] for item in request.units),
                provider_request_id=self._seed.provider_request_id,
                latency_ms=self._seed.latency_ms,
                response_sha256=self._seed.response_sha256,
            )
            bundle.validate_against(request)
            return bundle
        if len(request.units) > 1:
            return self._initial_live.translate(request)
        return self._retry_live.translate(request)


def _prompt_language_tag(language: str) -> str:
    """只规范提示词文件名；运行合同仍保留调用方传入的真实语种码。"""

    normalized = language.strip().lower()
    if normalized in {"zh-cn", "zh-hans"}:
        return "zh"
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--seed-translation", type=Path)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--page-id", required=True)
    parser.add_argument("--source-language", required=True)
    parser.add_argument("--target-language", required=True)
    parser.add_argument("--font-file", default="C:/Windows/Fonts/msyh.ttc")
    parser.add_argument(
        "--force-live-translation",
        action="store_true",
        help="模板语义分块变化后忽略旧种子，按当前 source_text 重新请求千问",
    )
    args = parser.parse_args()
    if args.seed_translation is None and not args.force_live_translation:
        parser.error("--seed-translation is required unless --force-live-translation is set")

    config = QwenConfig.from_environment()
    prompt_root = Path(__file__).resolve().parents[1] / "toolboxes" / "body" / "flow_text" / "multi" / "prompts"
    pattern_prompt = prompt_root / "layout_pattern_adjudication.zh-CN.md"

    seed = PageTranslationBundle(**_read_bundle(args.seed_translation)) if args.seed_translation else None
    prompt_direction = f"{_prompt_language_tag(args.source_language)}-{_prompt_language_tag(args.target_language)}"
    translation_prompt = prompt_root / f"page_translation.{prompt_direction}.zh-CN.md"
    retry_prompt = prompt_root / f"translation_retry.{prompt_direction}.zh-CN.md"
    if not translation_prompt.is_file():
        raise FileNotFoundError(translation_prompt)
    if not retry_prompt.is_file():
        raise FileNotFoundError(retry_prompt)
    initial_live = QwenPageTranslationProvider(config, translation_prompt.read_text(encoding="utf-8"))
    retry_live = QwenPageTranslationProvider(config, retry_prompt.read_text(encoding="utf-8"))
    provider = SeededThenQwenProvider(
        seed,
        initial_live,
        retry_live,
        use_seed=not args.force_live_translation,
    )
    adjudicator = QwenLayoutPatternAdjudicator(config, pattern_prompt.read_text(encoding="utf-8"))
    typography_prompt = prompt_root / "typography_density_adjudication.zh-CN.md"
    typography_adjudicator = QwenTypographyAdjudicator(config, typography_prompt.read_text(encoding="utf-8"))
    result = run_p5_page(
        source_pdf=args.source,
        page_id=args.page_id,
        run_dir=args.run_dir,
        provider=provider,
        font_file=args.font_file,
        source_language=args.source_language,
        target_language=args.target_language,
        layout_pattern_adjudicator=adjudicator,
        typography_adjudicator=typography_adjudicator,
    )

    _publish_result_artifacts(result=result, run_dir=args.run_dir)
    # 单页入口会被代表页和批量回归共同复用，报告名不能绑定某一轮 run 编号。
    write_json(args.run_dir / "reports" / "seeded_case_summary.json", result)
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0


def _publish_result_artifacts(*, result: P5RunResult, run_dir: Path) -> None:
    source_pdf = run_dir / "input" / "source.pdf"
    render_page(source_pdf, run_dir / "previews" / "source.png")
    if not result.candidate_pdf:
        return

    candidate = Path(result.candidate_pdf)
    result_pdf = run_dir / "output" / "result.pdf"
    shutil.copy2(candidate, result_pdf)
    render_page(result_pdf, run_dir / "previews" / "result.png")
    render_contact_sheet(
        source_pdf,
        result_pdf,
        run_dir / "previews" / "comparison.png",
    )


def _read_bundle(path: Path) -> dict[str, object]:
    from page_toolbox_puncture.contracts import TranslationResult

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["translations"] = tuple(TranslationResult(**item) for item in raw["translations"])
    return raw


if __name__ == "__main__":
    raise SystemExit(main())
