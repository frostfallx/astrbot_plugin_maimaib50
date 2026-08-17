from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT.parent))

if "astrbot_plugin_maib50" not in sys.modules:
    package_spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_maib50",
        REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    if package_spec is None or package_spec.loader is None:
        raise RuntimeError("Unable to load the plugin package")
    package = importlib.util.module_from_spec(package_spec)
    sys.modules["astrbot_plugin_maib50"] = package
    package_spec.loader.exec_module(package)

from astrbot_plugin_maib50.awmc_render import render_play_info
from astrbot_plugin_maib50.context_builder import build_context, load_peer_stats
from astrbot_plugin_maib50.render import render_b50_image, render_image


def chart(index: int, *, is_new: bool) -> dict:
    return {
        "song_id": index + (10001 if is_new else 1),
        "title": f"Render Smoke Song {index + 1}",
        "level_index": 3 + (1 if index % 9 == 0 else 0),
        "level_label": "Re:Master" if index % 9 == 0 else "Master",
        "ds": round(13.0 + (index % 17) / 10, 1),
        "achievements": round(99.0 + (index % 20) / 10, 4),
        "ra": 285 + index,
        "fc": "ap" if index % 5 == 0 else "fc",
        "type": "DX" if is_new else "SD",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Render an offline B50 smoke image")
    parser.add_argument("--assets", type=Path, default=REPO_ROOT / "assets")
    parser.add_argument("--mode", choices=("b50", "analysis", "info"), default="b50")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
    )
    args = parser.parse_args()

    raw = {
        "nickname": "Offline Smoke Test",
        "rating": 15000,
        "_source": "水鱼查分器（Diving-Fish）",
        "charts": {
            "sd": [chart(index, is_new=False) for index in range(35)],
            "dx": [chart(index, is_new=True) for index in range(15)],
        },
        "_assets_path": str(args.assets.resolve()),
    }
    context = build_context(raw, load_peer_stats(str(args.assets.resolve())))
    if args.mode == "info":
        song = {
            "id": "10001",
            "title": "Offline Info Test",
            "type": "DX",
            "level": ["3", "7", "10", "13"],
            "ds": [3.0, 7.0, 10.0, 13.4],
            "basic_info": {"artist": "Local Test", "bpm": 180},
            "charts": [{"notes": [100, 20, 10, 5]}] * 4,
        }
        records = [
            {
                "music_id": "10001",
                "level_index": 3,
                "achievement": 100.1234,
                "ra": 305,
                "dxScore": 390,
                "rate": "sss",
                "fc": "ap",
                "fs": "fsd",
            }
        ]
        image = render_play_info(
            song,
            records,
            str(args.assets.resolve()),
            "水鱼查分器（Diving-Fish）",
        )
    elif args.mode == "analysis":
        analysis = json.dumps(
            {
                "title": "离线结构化锐评测试",
                "overall_roast": (
                    "这是一段只使用本地 B35、B15、定数、"
                    "达成率和谱面标签的渲染冒烟测试，不调用 LLM。"
                ),
                "impression_roast": "结构化证据存在时才展示结论。",
                "push_recommendations": [
                    {
                        "title": "Render Smoke Song 1",
                        "reason": "继续巩固准度与稳定性",
                    }
                ],
            },
            ensure_ascii=False,
        )
        image = render_image(context, analysis, str(args.assets.resolve()))
    else:
        image = render_b50_image(context, str(args.assets.resolve()))
    output = args.output or REPO_ROOT / "data" / f"smoke-{args.mode}.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        image.save(output, "PNG")
    finally:
        image.close()
    print(output.resolve())


if __name__ == "__main__":
    main()
