from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from scripts.download_assets import install_assets, is_valid_assets, safe_extract


class AssetInstallerTests(unittest.TestCase):
    @staticmethod
    def _test_font() -> Path:
        for path in (
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        ):
            if path.is_file():
                return path
        raise unittest.SkipTest("系统中没有可用于渲染夹具的字体")

    @staticmethod
    def _make_assets(root: Path) -> None:
        font = root / "font"
        pic = root / "mai" / "pic"
        font.mkdir(parents=True)
        pic.mkdir(parents=True)
        bundled_font = AssetInstallerTests._test_font()
        (font / "ResourceHanRoundedCN-Bold.ttf").write_bytes(bundled_font.read_bytes())
        (font / "Torus SemiBold.otf").write_bytes(bundled_font.read_bytes())
        Image.new("RGBA", (1400, 1600), "white").save(pic / "b50.png")
        for name in ("basic", "advanced", "expert", "master", "remaster"):
            Image.new("RGBA", (276, 106), (80, 90, 120, 255)).save(
                pic / f"b50_score_{name}.png"
            )

    def test_installs_nested_asset_bundle_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source" / "static"
            self._make_assets(source)
            archive = root / "assets.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                for path in (root / "source").rglob("*"):
                    if path.is_file():
                        bundle.write(path, path.relative_to(root / "source"))

            destination = root / "installed"
            install_assets(archive.as_uri(), destination)
            install_assets("", destination)

            self.assertTrue(is_valid_assets(destination))
            self.assertTrue((destination / "mai" / "pic" / "b50.png").is_file())

    def test_validator_accepts_an_existing_nested_static_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            destination = Path(temp_name) / "assets"
            self._make_assets(destination / "static")

            self.assertFalse(is_valid_assets(destination))
            install_assets("", destination)

    def test_rejects_zip_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape.txt", "nope")
            with self.assertRaisesRegex(RuntimeError, "不安全路径"):
                safe_extract(archive, root / "extract")


if __name__ == "__main__":
    unittest.main()
