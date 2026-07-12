from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps


def main() -> int:
    parser = argparse.ArgumentParser(description="Build paginated visual-review sheets from P4 comparisons")
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--columns", type=int, default=2)
    parser.add_argument("--rows", type=int, default=3)
    parser.add_argument("--thumb-width", type=int, default=900)
    args = parser.parse_args()

    comparisons = sorted((args.run_root / "cases").glob("*/previews/comparison.png"))
    if not comparisons:
        raise SystemExit("no_p4_comparisons_found")
    output_dir = args.run_root / "reports" / "visual_contact_sheets"
    output_dir.mkdir(parents=True, exist_ok=True)
    page_size = args.columns * args.rows
    for sheet_index, start in enumerate(range(0, len(comparisons), page_size), start=1):
        batch = comparisons[start : start + page_size]
        tiles = [_tile(path, args.thumb_width) for path in batch]
        tile_width = max(tile.width for tile in tiles)
        tile_height = max(tile.height for tile in tiles)
        canvas = Image.new("RGB", (tile_width * args.columns, tile_height * args.rows), "white")
        for index, tile in enumerate(tiles):
            x = (index % args.columns) * tile_width
            y = (index // args.columns) * tile_height
            canvas.paste(tile, (x, y))
        canvas.save(output_dir / f"sheet-{sheet_index:03d}.jpg", quality=88)
    print(f"contact_sheets={len(list(output_dir.glob('sheet-*.jpg')))} comparisons={len(comparisons)}")
    return 0


def _tile(path: Path, width: int) -> Image.Image:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        height = round(rgb.height * width / rgb.width)
        thumbnail = rgb.resize((width, height), Image.Resampling.LANCZOS)
    label_height = 36
    tile = Image.new("RGB", (width, height + label_height), "white")
    tile.paste(thumbnail, (0, label_height))
    ImageDraw.Draw(tile).text((12, 10), path.parents[1].name, fill="black")
    return ImageOps.expand(tile, border=1, fill="#cccccc")


if __name__ == "__main__":
    raise SystemExit(main())
