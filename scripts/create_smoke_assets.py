from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from PIL import Image


def find_test_font() -> Path:
    for path in (
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    ):
        if path.is_file():
            return path
    raise RuntimeError("没有找到可用于离线冒烟测试的系统字体")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create minimal fake AWMC assets for tests"
    )
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    root = args.destination.resolve()
    font = root / "font"
    pic = root / "mai" / "pic"
    font.mkdir(parents=True, exist_ok=True)
    pic.mkdir(parents=True, exist_ok=True)
    source_font = find_test_font()
    shutil.copy2(source_font, font / "ResourceHanRoundedCN-Bold.ttf")
    shutil.copy2(source_font, font / "Torus SemiBold.otf")
    Image.new("RGBA", (1400, 1600), (235, 243, 255, 255)).save(pic / "b50.png")
    colors = (
        (70, 180, 95),
        (235, 180, 35),
        (235, 95, 105),
        (160, 85, 210),
        (195, 145, 235),
    )
    for name, color in zip(
        ("basic", "advanced", "expert", "master", "remaster"), colors
    ):
        Image.new("RGBA", (276, 106), (*color, 255)).save(pic / f"b50_score_{name}.png")
    print(root)


if __name__ == "__main__":
    main()
