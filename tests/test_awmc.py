from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT.parent))

if "astrbot_plugin_maib50" not in sys.modules:
    package_spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_maib50",
        REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    if package_spec is None or package_spec.loader is None:
        raise RuntimeError("Unable to load the plugin package for tests")
    package = importlib.util.module_from_spec(package_spec)
    sys.modules["astrbot_plugin_maib50"] = package
    package_spec.loader.exec_module(package)

from astrbot_plugin_maib50.awmc_render import (
    DESIGN_CREDIT,
    _parse_analysis,
    render_b50,
    render_play_info,
    render_song_info,
    valid_assets,
)
from astrbot_plugin_maib50.music_catalog import MusicCatalog
from astrbot_plugin_maib50.render import ANALYSIS_DESIGN_CREDIT, render_image


def _make_assets(root: Path) -> None:
    font = root / "font"
    pic = root / "mai" / "pic"
    font.mkdir(parents=True)
    pic.mkdir(parents=True)
    bundled_font = next(
        path
        for path in (
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        )
        if path.is_file()
    )
    (font / "ResourceHanRoundedCN-Bold.ttf").write_bytes(bundled_font.read_bytes())
    (font / "Torus SemiBold.otf").write_bytes(bundled_font.read_bytes())
    ui = root / "ui"
    ui_fonts = ui / "fonts"
    ui_icons = ui / "icons"
    ui_fonts.mkdir(parents=True)
    ui_icons.mkdir(parents=True)
    (ui_fonts / "ResourceHanRoundedCN.otf").write_bytes(bundled_font.read_bytes())
    (ui_fonts / "Torus SemiBold.otf").write_bytes(bundled_font.read_bytes())
    Image.new("RGBA", (300, 300), (205, 212, 226, 255)).save(
        ui / "default_cover.png"
    )
    Image.new("RGBA", (1800, 900), (244, 248, 255, 255)).save(
        ui_icons / "bj.png"
    )
    Image.new("RGBA", (1400, 1600), (235, 243, 255, 255)).save(pic / "b50.png")
    for index, name in enumerate(("basic", "advanced", "expert", "master", "remaster")):
        Image.new("RGBA", (276, 106), (50 + 30 * index, 80, 130, 255)).save(
            pic / f"b50_score_{name}.png"
        )


def _chart(index: int, bucket: str) -> dict:
    return {
        "music_id": str(index + 1),
        "title": f"Test Song {index + 1}",
        "level_index": index % 5,
        "ds": 13.0 + index % 10 / 10,
        "achievement": 99.5,
        "ra": 300 + index,
        "type": "DX" if index % 2 else "SD",
        "fc": "fc",
        "bucket": bucket,
    }


class AWMCRenderTests(unittest.TestCase):
    def test_missing_roast_does_not_fall_back_to_raw_json(self) -> None:
        parsed = _parse_analysis('{"title":"测试","push_recommendations":[]}')

        self.assertIn("模型输出格式异常", parsed["overall"])
        self.assertNotIn("push_recommendations", parsed["overall"])

    def test_nested_roast_object_is_unwrapped_for_rendering(self) -> None:
        parsed = _parse_analysis(
            '{"title":"测试","overall_roast":{"text":"给人看的正文"}}'
        )

        self.assertEqual(parsed["overall"], "给人看的正文")

    def test_double_encoded_roast_is_unwrapped_for_rendering(self) -> None:
        parsed = _parse_analysis(
            json.dumps(
                {
                    "title": "测试",
                    "overall_roast": json.dumps(
                        {"overall_roast": "最终正文"}, ensure_ascii=False
                    ),
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(parsed["overall"], "最终正文")

    def test_theme_subdirectory_score_cards_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            assets = Path(temp_name)
            _make_assets(assets)
            pic = assets / "mai" / "pic"
            theme = pic / "prism_plus"
            theme.mkdir()
            for filename in (
                "b50.png",
                *[
                    f"b50_score_{name}.png"
                    for name in (
                        "basic",
                        "advanced",
                        "expert",
                        "master",
                        "remaster",
                    )
                ],
            ):
                (pic / filename).replace(theme / filename)

            self.assertTrue(valid_assets(assets))
            image = render_b50(
                {
                    "player": {"nickname": "Tester", "rating": 300},
                    "summary": {"b35_ra": 300, "b15_ra": 0},
                    "b50": [_chart(0, "B35")],
                },
                str(assets),
            )
            self.assertEqual(image.size, (1400, 1600))
            image.close()

    def test_b50_analysis_and_song_info_render_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            assets = Path(temp_name)
            _make_assets(assets)
            self.assertTrue(valid_assets(assets))
            context = {
                "player": {
                    "nickname": "Tester",
                    "rating": 15000,
                    "qq": "123",
                    "source": "水鱼查分器（Diving-Fish）",
                },
                "summary": {"b35_ra": 10500, "b15_ra": 4500},
                "b50": [
                    *(_chart(i, "B35") for i in range(35)),
                    *(_chart(35 + i, "B15") for i in range(15)),
                ],
            }
            context["evidence"] = {
                "highlights": [context["b50"][0], context["b50"][1]],
                "highest_song_rating": [context["b50"][2]],
                "push_recommendations": [
                    {
                        **context["b50"][3],
                        "reason": "继续巩固准度与稳定性",
                        "gain_1005": 8,
                    }
                ],
            }
            b50 = render_b50(context, str(assets))
            self.assertEqual(b50.size, (1400, 1600))
            b50.close()

            analysis = render_image(
                context,
                json.dumps(
                    {
                        "title": "Test",
                        "overall_roast": "Only known facts stay visible",
                    }
                ),
                str(assets),
            )
            self.assertEqual(analysis.width, 900)
            self.assertGreater(analysis.height, 900)
            analysis.close()

            song = {
                "id": "1",
                "title": "Test Song",
                "type": "DX",
                "ds": [1.0, 5.0, 9.0, 13.0],
                "level": ["1", "5", "9", "13"],
                "basic_info": {"artist": "Artist", "genre": "maimai", "bpm": 180},
                "charts": [{"charter": "Charter", "notes": [1, 2, 3, 4]}] * 4,
            }
            info = render_song_info(song, str(assets))
            self.assertEqual(info.size, (1200, 1300))
            info.close()

            play_info = render_play_info(
                song,
                [
                    {
                        "music_id": "1",
                        "level_index": 3,
                        "achievement": 100.1234,
                        "ra": 305,
                        "dxScore": 28,
                        "rate": "sss",
                        "fc": "ap",
                        "fs": "fsd",
                    }
                ],
                str(assets),
                "水鱼查分器（Diving-Fish）",
            )
            self.assertEqual(play_info.size, (1200, 900))
            play_info.close()

    def test_design_credit_is_the_requested_signature(self) -> None:
        self.assertEqual(
            DESIGN_CREDIT,
            "Designed by Yuri-YuzuChaN & BlueDeer233 | "
            "Generated by AstrBot",
        )
        self.assertEqual(
            ANALYSIS_DESIGN_CREDIT,
            "Designed by 寒桠@OneCatBot | Generated by AstrBot",
        )


class MusicCatalogTests(unittest.TestCase):
    def test_title_alias_and_local_alias_queries(self) -> None:
        catalog = MusicCatalog()
        catalog.set_songs([{"id": "1", "title": "World's end loneliness"}])
        catalog.merge_alias_payload(
            [{"SongID": 1, "Name": "World's end loneliness", "Alias": ["水鱼"]}]
        )
        self.assertEqual(catalog.search("world's end")[0]["id"], "1")
        self.assertEqual(catalog.resolve_alias("水鱼")[0]["id"], "1")
        self.assertIn("水鱼", catalog.aliases("1")[0][1])


if __name__ == "__main__":
    unittest.main()
