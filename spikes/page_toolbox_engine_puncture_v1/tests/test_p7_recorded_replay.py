from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from page_toolbox_puncture.contracts import PageTranslationRequest, TranslationUnit
from page_toolbox_puncture.translation import ProviderError
from scripts.run_p7_full_batch import RecordedTranslationProvider


class P7RecordedReplayTests(unittest.TestCase):
    def test_recorded_provider_filters_the_saved_page_bundle_for_targeted_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle_path = Path(temporary) / "translation_bundle.json"
            bundle_path.write_text(
                json.dumps(
                    {
                        "model": "recorded-model",
                        "translations": [
                            {"container_id": "flow-001", "translated_text": "Flow"},
                            {"container_id": "table-001", "translated_text": "Table"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            request = PageTranslationRequest(
                "retry-request",
                "P7-test",
                "zh-CN",
                "en",
                (TranslationUnit("table-001", "表格", 0),),
            )

            bundle = RecordedTranslationProvider(bundle_path).translate(request)

            self.assertEqual("recorded-model", bundle.model)
            self.assertEqual(("table-001",), tuple(item.container_id for item in bundle.translations))
            bundle.validate_against(request)

    def test_recorded_provider_reports_missing_new_unit_as_capability_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle_path = Path(temporary) / "translation_bundle.json"
            bundle_path.write_text(
                json.dumps(
                    {
                        "translations": [
                            {"container_id": "flow-001", "translated_text": "Flow"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            request = PageTranslationRequest(
                "current-request",
                "P7-test",
                "zh-CN",
                "en",
                (
                    TranslationUnit("flow-001", "正文", 0),
                    TranslationUnit("footer-001", "页脚", 1),
                ),
            )

            with self.assertRaises(ProviderError) as raised:
                RecordedTranslationProvider(bundle_path).translate(request)

            self.assertEqual("RECORDED_TRANSLATION_MISSING", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
