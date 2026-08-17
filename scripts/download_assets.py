from __future__ import annotations

import argparse
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path


def _pic_asset(pic: Path, filename: str, *, themed: bool = False) -> Path | None:
    candidates = (
        (pic / "prism_plus" / filename, pic / "circle" / filename, pic / filename)
        if themed
        else (pic / filename, pic / "prism_plus" / filename, pic / "circle" / filename)
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def is_valid_assets(path: Path) -> bool:
    pic = path / "mai" / "pic"
    background = _pic_asset(pic, "b50.png", themed=True) or _pic_asset(
        pic, "b50_bg.png", themed=True
    )
    fonts = (
        path / "font" / "ResourceHanRoundedCN-Bold.ttf",
        path / "font" / "Torus SemiBold.otf",
    )
    cards = (
        _pic_asset(pic, f"b50_score_{name}.png")
        for name in ("basic", "advanced", "expert", "master", "remaster")
    )
    return bool(background) and all(item.is_file() for item in fonts) and all(cards)


def find_assets_root(path: Path) -> Path:
    if is_valid_assets(path):
        return path
    candidates = [
        candidate
        for candidate in path.rglob("*")
        if candidate.is_dir() and is_valid_assets(candidate)
    ]
    if not candidates:
        raise RuntimeError("压缩包内未找到包含 font/ 和 mai/pic/ 的 AWMC static 目录")
    return min(candidates, key=lambda item: len(item.parts))


def safe_extract(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"资源压缩包包含不安全路径：{member.filename}")
        bundle.extractall(destination)


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "astrbot-plugin-maib50-assets/1.0"},
    )
    with (
        urllib.request.urlopen(request, timeout=120) as response,
        destination.open("wb") as output,
    ):
        shutil.copyfileobj(response, output, length=1024 * 1024)


def install_assets(url: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if is_valid_assets(destination):
        print(f"B50 assets already present: {destination}")
        return
    if not url:
        try:
            nested = find_assets_root(destination)
        except RuntimeError:
            nested = None
        if nested is not None:
            print(f"B50 assets found in nested directory: {nested}")
            return
        raise RuntimeError("素材下载 URL 为空，且本地素材目录无效")

    with tempfile.TemporaryDirectory(prefix="maib50-assets-") as temp_name:
        temp_dir = Path(temp_name)
        archive = temp_dir / "assets.zip"
        extracted = temp_dir / "extracted"
        extracted.mkdir()
        print(f"Downloading B50 assets from {url}")
        download(url, archive)
        safe_extract(archive, extracted)
        source = find_assets_root(extracted)
        for item in source.iterdir():
            target = destination / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)

    if not is_valid_assets(destination):
        raise RuntimeError("资源复制完成后校验失败")
    (destination / ".source-url").write_text(url + "\n", encoding="utf-8")
    print(f"B50 assets installed: {destination}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download and validate B50 assets")
    parser.add_argument(
        "--url",
        default="",
        help="可选的 ZIP 资源地址；官方 7z 请先手动解压",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("/AstrBot/data/assets"),
    )
    args = parser.parse_args()
    install_assets(args.url.strip(), args.destination.resolve())


if __name__ == "__main__":
    main()
