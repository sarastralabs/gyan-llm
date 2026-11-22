"""Build the Gyan icon set from the Sarastra Labs logo.

    pip install Pillow
    python ui/scripts/make_brand_assets.py --src ui/brand/logo-signature.png --out ui/assets

The source is the company logo (mark on the left, wordmark on the right). The script cuts out the
mark and writes every icon LibreChat looks for, plus `gyan-icon.png` (used as the model's avatar)
and `logo.svg` (the login page logo: the mark embedded as a PNG so it needs no fonts).
"""
from __future__ import annotations

import argparse
import base64
import io
from pathlib import Path

from PIL import Image

PAPER = (251, 247, 242, 255)  # sarastralabs.com background, #FBF7F2


def cut_out_mark(logo: Image.Image) -> Image.Image:
    """The leftmost shape of the logo: columns up to the first clear gap."""
    alpha = logo.getchannel("A")
    width, height = logo.size
    columns = [alpha.crop((x, 0, x + 1, height)).getbbox() is not None for x in range(width)]
    start = columns.index(True)
    end, gap = start, 0
    for x in range(start, width):
        if columns[x]:
            end, gap = x, 0
        else:
            gap += 1
            if gap > max(6, width // 60):
                break
    mark = logo.crop((start, 0, end + 1, height))
    box = mark.getchannel("A").getbbox()
    return mark.crop(box) if box else mark


def on_square(mark: Image.Image, size: int, fill: float, background=None) -> Image.Image:
    """Center the mark in a square canvas, using `fill` of its width/height."""
    canvas = Image.new("RGBA", (size, size), background or (0, 0, 0, 0))
    target = int(size * fill)
    scale = target / max(mark.size)
    resized = mark.resize((max(1, round(mark.width * scale)), max(1, round(mark.height * scale))),
                          Image.LANCZOS)
    canvas.alpha_composite(resized, ((size - resized.width) // 2, (size - resized.height) // 2))
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    mark = cut_out_mark(Image.open(args.src).convert("RGBA"))
    print(f"mark size: {mark.size}")

    icon_512 = on_square(mark, 512, 0.84)
    icon_512.save(args.out / "gyan-icon.png")
    on_square(mark, 192, 0.84).save(args.out / "icon-192x192.png")
    on_square(mark, 32, 0.92).save(args.out / "favicon-32x32.png")
    on_square(mark, 16, 0.94).save(args.out / "favicon-16x16.png")
    # iOS and installed-app icons: a solid background, with the mark well inside the safe area.
    on_square(mark, 180, 0.62, PAPER).save(args.out / "apple-touch-icon-180x180.png")
    on_square(mark, 512, 0.52, PAPER).save(args.out / "maskable-icon.png")

    buffer = io.BytesIO()
    icon_512.save(buffer, format="PNG", optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode()
    (args.out / "logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="512" height="512" viewBox="0 0 512 512">'
        f'<image width="512" height="512" href="data:image/png;base64,{encoded}"/></svg>\n',
        encoding="utf-8",
    )
    for path in sorted(args.out.iterdir()):
        print(f"  {path.name:34} {path.stat().st_size:>8} bytes")


if __name__ == "__main__":
    main()
