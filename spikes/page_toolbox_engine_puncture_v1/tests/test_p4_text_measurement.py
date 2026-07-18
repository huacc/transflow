from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from toolboxes.body.flow_text.single.tools.p4_layout_planner import _font_variant, _minimum_text_height


class _ProbePage:
    def __init__(self) -> None:
        self.insert_font_calls = 0
        self.insert_textbox_calls = 0

    def insert_font(self, **_kwargs) -> None:
        self.insert_font_calls += 1

    def insert_textbox(self, rect, *_args, **_kwargs) -> float:
        self.insert_textbox_calls += 1
        return 1.0 if rect.y1 >= 40.0 else -1.0


class _ProbeDocument:
    def __init__(self) -> None:
        self.page = _ProbePage()
        self.new_page_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def new_page(self, **_kwargs):
        self.new_page_calls += 1
        return self.page


class P4TextMeasurementTests(unittest.TestCase):
    def test_font_variant_uses_hyphenated_bold_file_when_available(self) -> None:
        with TemporaryDirectory() as directory:
            regular = Path(directory) / "ExampleSans.ttf"
            bold = Path(directory) / "ExampleSans-Bold.ttf"
            regular.touch()
            bold.touch()

            font_file, font_resource = _font_variant(str(regular), "flow", "bold")

        self.assertEqual(str(bold), font_file)
        self.assertEqual("flow_bold", font_resource)

    def test_minimum_height_reuses_one_preloaded_probe_page(self) -> None:
        document = _ProbeDocument()
        with patch(
            "toolboxes.body.flow_text.single.tools.p4_layout_planner.fitz.open",
            return_value=document,
        ):
            height = _minimum_text_height(
                595.0,
                842.0,
                200.0,
                "translated text",
                8.0,
                1.15,
                "font.ttc",
                "probe_font",
                0,
            )

        self.assertGreaterEqual(height, 40.0)
        self.assertEqual(1, document.new_page_calls)
        self.assertEqual(1, document.page.insert_font_calls)
        self.assertEqual(11, document.page.insert_textbox_calls)


if __name__ == "__main__":
    unittest.main()
