from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

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

from astrbot_plugin_maib50.config import Config
from astrbot_plugin_maib50.context_builder import (
    _build_push_candidates,
    build_context,
    push_target_for_achievement,
)
from astrbot_plugin_maib50.data_source import (
    DIVING_FISH,
    LXNS,
    SourceStore,
    parse_source,
)
from astrbot_plugin_maib50.fetch import (
    PlayerNotFoundError,
    _fetch_diving_fish_music_records,
    _fetch_dev_records,
    _fetch_lxns_music_records,
    _fetch_lxns_player,
    _is_new,
    _lxns_record,
    _missing_player_message,
)
from astrbot_plugin_maib50.llm import (
    _SYSTEM,
    _analysis_length_instruction,
    _cleanup_response,
    _merge_push_recommendations,
    _snowflakes_from_usage,
)
from astrbot_plugin_maib50.main import MaiB50Plugin, SOURCE_REPOSITORY
from astrbot_plugin_maib50.render import split_b50_charts


def _chart(index: int, *, is_new: bool) -> dict:
    return {
        "song_id": index + (10000 if is_new else 1),
        "title": f"Song {index}",
        "level_index": 3,
        "level_label": "Master",
        "ds": 13.0 + (index % 16) / 10,
        "achievements": 99.0 + (index % 20) / 10,
        "ra": 250 + index,
        "fc": "fc" if index % 2 else "ap",
        "type": "DX" if is_new else "SD",
    }


class ConfigTests(unittest.TestCase):
    def test_schema_values_ignore_process_environment(self) -> None:
        env = {
            "B50_LLM_KEY": "env-key",
            "B50_LLM_MODEL": "env-model",
            "B50_QUERY_TIMEOUT_SECONDS": "999",
            "B50_DAILY_LIMIT": "3",
            "LXNS_DEV_TOKEN": "env-lxns-token",
        }
        with patch.dict(os.environ, env, clear=False):
            config = Config.from_dict(
                {
                    "b50_llm_key": "webui-key",
                    "b50_llm_model": "webui-model",
                    "query_timeout_seconds": 20,
                }
            )
        self.assertEqual(config.b50_llm_key, "webui-key")
        self.assertEqual(config.b50_llm_model, "webui-model")
        self.assertEqual(config.query_timeout_seconds, 20)
        self.assertEqual(config.b50_daily_limit, 0)
        self.assertEqual(config.lxns_dev_token, "")

    def test_default_llm_output_budget_avoids_truncated_json(self) -> None:
        config = Config.from_dict({})
        self.assertEqual(config.b50_llm_max_tokens, 2048)
        self.assertEqual(config.b50_llm_input_price_per_million, 1.0)
        self.assertEqual(config.b50_llm_output_price_per_million, 2.0)

    def test_old_zero_prices_migrate_to_deepseek_defaults(self) -> None:
        config = Config.from_dict(
            {
                "b50_llm_input_price_per_million": 0,
                "b50_llm_output_price_per_million": 0,
            }
        )
        self.assertEqual(config.b50_llm_input_price_per_million, 1.0)
        self.assertEqual(config.b50_llm_output_price_per_million, 2.0)

    def test_llm_prices_are_read_from_astrbot_config(self) -> None:
        config = Config.from_dict(
            {
                "b50_llm_input_price_per_million": "2.5",
                "b50_llm_output_price_per_million": 10,
            }
        )
        self.assertEqual(config.b50_llm_input_price_per_million, 2.5)
        self.assertEqual(config.b50_llm_output_price_per_million, 10.0)


class DataSourceTests(unittest.TestCase):
    @staticmethod
    def _http_client(response: object) -> tuple[MagicMock, AsyncMock]:
        client = AsyncMock()
        client.post.return_value = response
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)
        return context, client

    def test_developer_records_use_live_b50_groups_when_catalog_is_stale(self) -> None:
        old = {
            "song_id": 1, "title": "Old song", "level_index": 3, "type": "DX", "ra": 300
        }
        extra = {
            "song_id": 2, "title": "Extra song", "level_index": 3, "type": "DX", "ra": 280
        }
        new = {
            "song_id": 3, "title": "New song", "level_index": 3, "type": "DX", "ra": 315
        }
        response = MagicMock(status_code=200)
        response.json.return_value = {"records": [old, extra, new], "rating": 895}
        context, client = self._http_client(response)
        client.get.return_value = response
        public = {"charts": {"sd": [old], "dx": [new]}}

        with (
            patch("astrbot_plugin_maib50.fetch.httpx.AsyncClient", return_value=context),
            patch("astrbot_plugin_maib50.fetch.get_music_lookup", return_value={}),
            patch(
                "astrbot_plugin_maib50.fetch._fetch_public_b50",
                AsyncMock(return_value=public),
            ),
        ):
            result = asyncio.run(_fetch_dev_records("123456", "developer-token"))

        self.assertEqual(
            [row["title"] for row in result["charts"]["sd"]],
            ["Old song", "Extra song"],
        )
        self.assertEqual([row["title"] for row in result["charts"]["dx"]], ["New song"])
        b35, b15 = split_b50_charts(build_context(result))
        self.assertEqual([row["title"] for row in b15], ["New song"])
        self.assertEqual([row["title"] for row in b35], ["Old song", "Extra song"])

    def test_developer_records_remain_available_if_live_b50_fails(self) -> None:
        record = {"song_id": 1, "title": "Known song", "level_index": 3, "ra": 300}
        response = MagicMock(status_code=200)
        response.json.return_value = {"records": [record], "rating": 300}
        context, client = self._http_client(response)
        client.get.return_value = response
        lookup = {"1": {"basic_info": {"is_new": True}}}

        with (
            patch("astrbot_plugin_maib50.fetch.httpx.AsyncClient", return_value=context),
            patch("astrbot_plugin_maib50.fetch.get_music_lookup", return_value=lookup),
            patch(
                "astrbot_plugin_maib50.fetch._fetch_public_b50",
                AsyncMock(side_effect=ValueError("unavailable")),
            ),
        ):
            result = asyncio.run(_fetch_dev_records("123456", "developer-token"))

        self.assertEqual(result["charts"]["sd"], [])
        self.assertEqual(
            [row["title"] for row in result["charts"]["dx"]], ["Known song"]
        )

    def test_source_aliases_and_persistence(self) -> None:
        self.assertEqual(parse_source("水鱼"), DIVING_FISH)
        self.assertEqual(parse_source("LXNS"), LXNS)
        self.assertIsNone(parse_source("其他"))

        with tempfile.TemporaryDirectory() as temp_name:
            data_dir = Path(temp_name)
            store = SourceStore()
            store.configure(data_dir)
            self.assertEqual(store.get("123"), DIVING_FISH)

            asyncio.run(store.set("123", LXNS))
            restored = SourceStore()
            restored.configure(data_dir)
            self.assertEqual(restored.get("123"), LXNS)

    def test_lxns_score_is_adapted_to_renderer_shape(self) -> None:
        record = _lxns_record(
            {
                "id": 1,
                "song_name": "Test Song",
                "level": "13",
                "level_index": 3,
                "achievements": 100.1234,
                "fc": "ap",
                "fs": "fsd",
                "rate": "sss",
                "dx_score": 1234,
                "dx_rating": 305,
                "type": "dx",
            }
        )
        self.assertEqual(record["music_id"], "10001")
        self.assertEqual(record["type"], "DX")
        self.assertEqual(record["achievement"], 100.1234)
        self.assertEqual(record["ra"], 305)

    def test_lxns_single_song_uses_friend_code_and_dx_id(self) -> None:
        score = {
            "id": 1,
            "level_index": 3,
            "achievements": 100.0,
            "dx_rating": 300,
            "type": "dx",
        }
        mocked_get = AsyncMock(side_effect=[{"friend_code": 987654321}, [score]])
        with patch("astrbot_plugin_maib50.fetch._lxns_get", mocked_get):
            records = asyncio.run(
                _fetch_lxns_music_records("123456", {"id": "10001", "type": "DX"})
            )

        self.assertEqual(records[0]["music_id"], "10001")
        self.assertEqual(
            mocked_get.call_args_list,
            [
                call("/player/qq/123456"),
                call(
                    "/player/987654321/bests",
                    params={"song_id": 1, "song_type": "dx"},
                ),
            ],
        )

    def test_diving_fish_single_song_sends_music_id_as_string(self) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {"1": [{"id": 1, "achievements": 100.5}]}
        context, client = self._http_client(response)

        with (
            patch(
                "astrbot_plugin_maib50.fetch._get_dev_token",
                return_value="developer-token",
            ),
            patch(
                "astrbot_plugin_maib50.fetch.httpx.AsyncClient",
                return_value=context,
            ),
        ):
            records = asyncio.run(
                _fetch_diving_fish_music_records("123456", {"id": "1"})
            )

        self.assertEqual(records[0]["achievements"], 100.5)
        self.assertEqual(client.post.await_args.kwargs["json"]["music_id"], "1")

    def test_diving_fish_token_error_is_not_reported_as_missing_user(self) -> None:
        response = MagicMock(status_code=400)
        response.json.return_value = {
            "status": "error",
            "message": "Developer token invalid",
        }
        context, _client = self._http_client(response)

        with (
            patch(
                "astrbot_plugin_maib50.fetch._get_dev_token",
                return_value="invalid-token",
            ),
            patch(
                "astrbot_plugin_maib50.fetch.httpx.AsyncClient",
                return_value=context,
            ),
            self.assertRaisesRegex(ValueError, "水鱼开发者 Token 不可用"),
        ):
            asyncio.run(_fetch_diving_fish_music_records("123456", {"id": "1"}))

    def test_diving_fish_missing_user_keeps_specific_error(self) -> None:
        response = MagicMock(status_code=400)
        response.json.return_value = {"message": "no such user"}
        context, _client = self._http_client(response)

        with (
            patch(
                "astrbot_plugin_maib50.fetch._get_dev_token",
                return_value="developer-token",
            ),
            patch(
                "astrbot_plugin_maib50.fetch.httpx.AsyncClient",
                return_value=context,
            ),
            self.assertRaisesRegex(ValueError, "用户不存在或未开放 B50 查询"),
        ):
            asyncio.run(_fetch_diving_fish_music_records("123456", {"id": "1"}))

    def test_missing_player_message_matches_requested_copy(self) -> None:
        self.assertEqual(
            _missing_player_message("999999999999"),
            "用户不存在或未开放 B50 查询（QQ: 999999999999）；\n"
            "请确保已配置水鱼导入令牌/落雪API秘钥，并绑定QQ。",
        )

    def test_lxns_missing_player_uses_unified_message(self) -> None:
        with (
            patch(
                "astrbot_plugin_maib50.fetch._lxns_get",
                AsyncMock(side_effect=PlayerNotFoundError("not found")),
            ),
            self.assertRaisesRegex(ValueError, "用户不存在或未开放 B50 查询"),
        ):
            asyncio.run(_fetch_lxns_player("999999999999"))


class ContextTests(unittest.TestCase):
    def test_push_candidates_only_include_bird_near_misses(self) -> None:
        charts = [
            {"song_id": "1", "title": "鸟寸", "achievement": 99.95, "ds": 14.0, "ra": 300, "bucket": "B35"},
            {"song_id": "2", "title": "鸟加寸", "achievement": 100.45, "ds": 14.0, "ra": 310, "bucket": "B35"},
            {"song_id": "3", "title": "差得较远", "achievement": 99.89, "ds": 14.0, "ra": 290, "bucket": "B35"},
            {"song_id": "4", "title": "已鸟但非鸟加寸", "achievement": 100.30, "ds": 14.0, "ra": 305, "bucket": "B35"},
        ]

        candidates = _build_push_candidates(charts)
        by_title = {item["title"]: item for item in candidates}

        self.assertEqual(set(by_title), {"鸟寸", "鸟加寸"})
        self.assertEqual(by_title["鸟寸"]["target"], "SSS")
        self.assertEqual(by_title["鸟加寸"]["target"], "SSS+")

    def test_push_target_near_miss_boundaries(self) -> None:
        self.assertIsNone(push_target_for_achievement(99.8999))
        self.assertEqual(push_target_for_achievement(99.9), "SSS")
        self.assertIsNone(push_target_for_achievement(100.0))
        self.assertIsNone(push_target_for_achievement(100.3999))
        self.assertEqual(push_target_for_achievement(100.4), "SSS+")
        self.assertIsNone(push_target_for_achievement(100.5))

    def test_music_api_is_new_flag_wins_over_version_fallback(self) -> None:
        lookup = {
            "1": {
                "id": "1",
                "basic_info": {"from": "future version", "is_new": True},
            }
        }
        self.assertTrue(_is_new("1", lookup))

    def test_build_context_keeps_b35_b15_and_evidence(self) -> None:
        raw = {
            "nickname": "Tester",
            "rating": 15000,
            "charts": {
                "sd": [_chart(index, is_new=False) for index in range(35)],
                "dx": [_chart(index, is_new=True) for index in range(15)],
            },
        }
        context = build_context(raw)
        b35, b15 = split_b50_charts(context)

        self.assertEqual(len(b35), 35)
        self.assertEqual(len(b15), 15)
        self.assertEqual(context["summary"]["b35_ra"], sum(c["ra"] for c in b35))
        self.assertEqual(context["summary"]["b15_ra"], sum(c["ra"] for c in b15))
        structure = context["b50_evidence_pack"]["b35_b15_structure"]
        self.assertEqual(structure["b35"]["count"], 35)
        self.assertEqual(structure["b15"]["count"], 15)
        self.assertEqual(
            len(context["b50_evidence_pack"]["roast_targets"]),
            3,
        )

    def test_split_ignores_full_record_extras(self) -> None:
        context = {
            "b50": [
                *({"bucket": "B35", "title": str(i)} for i in range(35)),
                *({"bucket": None, "title": f"extra-{i}"} for i in range(20)),
                *({"bucket": "B15", "title": str(i)} for i in range(15)),
            ]
        }
        b35, b15 = split_b50_charts(context)
        self.assertEqual((len(b35), len(b15)), (35, 15))


class CommandTests(unittest.TestCase):
    def test_truncated_llm_json_salvages_human_readable_roast(self) -> None:
        raw = (
            '{"title":"15139分交互尾杀吃透",'
            '"overall_roast":"家人们，这段正文应该给人看。",'
            '"impression_roast":"交互吃透",'
            '"push_recommendations":[{"title":"DATAERROR","gain'
        )
        cleaned = json.loads(_cleanup_response(raw))

        self.assertEqual(cleaned["title"], "15139分交互尾杀吃透")
        self.assertEqual(cleaned["overall_roast"], "家人们，这段正文应该给人看。")
        self.assertEqual(cleaned["impression_roast"], "交互吃透")
        self.assertEqual(cleaned["push_recommendations"], [])
        self.assertNotIn('"overall_roast"', cleaned["overall_roast"])

    def test_unrecoverable_structured_output_never_leaks_json(self) -> None:
        cleaned = json.loads(_cleanup_response('{"title":'))
        self.assertNotIn('{"title"', cleaned["overall_roast"])
        self.assertIn("模型输出格式异常", cleaned["overall_roast"])

    def test_valid_json_without_roast_never_becomes_rendered_json(self) -> None:
        cleaned = json.loads(
            _cleanup_response('{"title":"测试","push_recommendations":[]}')
        )
        self.assertEqual(cleaned["title"], "测试")
        self.assertIn("模型输出格式异常", cleaned["overall_roast"])
        self.assertNotIn("push_recommendations", cleaned["overall_roast"])

    def test_nested_roast_text_is_unwrapped_without_stringifying_object(self) -> None:
        cleaned = json.loads(
            _cleanup_response(
                '{"title":"测试","overall_roast":{"text":"可读正文"}}'
            )
        )
        self.assertEqual(cleaned["overall_roast"], "可读正文")

    def test_double_encoded_roast_does_not_render_json_fields(self) -> None:
        raw = json.dumps(
            {
                "title": "外层",
                "overall_roast": json.dumps(
                    {
                        "title": "内层",
                        "overall_roast": "真正给人看的正文",
                        "push_recommendations": [],
                    },
                    ensure_ascii=False,
                ),
            },
            ensure_ascii=False,
        )
        cleaned = json.loads(_cleanup_response(raw))

        self.assertEqual(cleaned["overall_roast"], "真正给人看的正文")
        self.assertNotIn("overall_roast", cleaned["overall_roast"])

    def test_output_length_tracks_token_budget(self) -> None:
        self.assertIn("350-500", _analysis_length_instruction(1024))
        self.assertIn("600-850", _analysis_length_instruction(2048))

    def test_b50_returns_only_the_result_without_progress_message(self) -> None:
        class Event:
            message_str = "b50"
            message_obj = SimpleNamespace(message=[])

            @staticmethod
            def get_sender_id() -> str:
                return "123456"

            @staticmethod
            def image_result(path: str) -> tuple[str, str]:
                return "image", path

            @staticmethod
            def plain_result(message: str) -> tuple[str, str]:
                return "plain", message

        plugin = object.__new__(MaiB50Plugin)
        plugin.active_users = set()
        plugin.config = SimpleNamespace(query_timeout_seconds=5)
        plugin._query = AsyncMock(return_value=Path("b50-result.png"))
        plugin._schedule_cleanup = lambda _path: None

        async def collect() -> list[tuple[str, str]]:
            return [item async for item in plugin.query_b50(Event())]

        results = asyncio.run(collect())
        self.assertEqual(results, [("image", str(Path("b50-result.png").resolve()))])

    def test_source_command_exposes_agpl_corresponding_source(self) -> None:
        class Event:
            @staticmethod
            def plain_result(message: str) -> tuple[str, str]:
                return "plain", message

        plugin = object.__new__(MaiB50Plugin)

        async def collect() -> list[tuple[str, str]]:
            return [item async for item in plugin.source_information(Event())]

        self.assertEqual(
            asyncio.run(collect()),
            [
                (
                    "plain",
                    f"Source code: {SOURCE_REPOSITORY}\nLicense: AGPL-3.0-only",
                )
            ],
        )

    def test_alias_song_returns_image_without_confirmation_text(self) -> None:
        class Event:
            message_str = "dx是什么歌"

            @staticmethod
            def image_result(path: str) -> tuple[str, str]:
                return "image", path

            @staticmethod
            def plain_result(message: str) -> tuple[str, str]:
                return "plain", message

        plugin = object.__new__(MaiB50Plugin)
        plugin._song_info = AsyncMock(return_value=Path("song-info.png"))
        plugin._schedule_cleanup = lambda _path: None

        async def collect() -> list[tuple[str, str]]:
            return [item async for item in plugin.music_by_alias(Event())]

        with patch(
            "astrbot_plugin_maib50.main.catalog.resolve_alias",
            return_value=[{"id": "1", "title": "Test Song"}],
        ):
            results = asyncio.run(collect())

        self.assertEqual(results, [("image", str(Path("song-info.png").resolve()))])

    def test_analysis_sends_usage_after_image(self) -> None:
        class Event:
            message_str = "锐评b50"
            role = "member"

            @staticmethod
            def get_sender_id() -> str:
                return "123456"

            @staticmethod
            def image_result(path: str) -> tuple[str, str]:
                return "image", path

            @staticmethod
            def plain_result(message: str) -> tuple[str, str]:
                return "plain", message

        plugin = object.__new__(MaiB50Plugin)
        plugin.active_users = set()
        plugin.config = SimpleNamespace(
            b50_daily_limit=0,
            analysis_timeout_seconds=5,
            b50_llm_model="gpt-5.6-sol",
        )
        plugin._analyze = AsyncMock(
            return_value=(Path("analysis-result.png"), 12.5)
        )
        plugin._schedule_cleanup = lambda _path: None

        async def collect() -> list[tuple[str, str]]:
            return [item async for item in plugin.analyze_b50(Event())]

        results = asyncio.run(collect())
        self.assertEqual(
            results,
            [
                (
                    "plain",
                    "正在生成，请耐心等待1~2分钟...\n"
                    "使用的分析模型：gpt-5.6-sol",
                ),
                ("image", str(Path("analysis-result.png").resolve())),
                ("plain", "本次请求消耗 12.5 片雪花，您还剩 ∞ 片雪花。"),
            ],
        )

    def test_analysis_prompt_forbids_all_formatting_markup(self) -> None:
        self.assertIn("输出必须是严格 JSON", _SYSTEM)
        self.assertIn("禁止 Markdown、HTML", _SYSTEM)
        self.assertIn("只能使用纯文本", _SYSTEM)
        self.assertNotIn("<r>", _SYSTEM)

    def test_token_usage_is_converted_to_snowflakes(self) -> None:
        config = Config.from_dict(
            {
                "b50_llm_input_price_per_million": 2,
                "b50_llm_output_price_per_million": 8,
            }
        )
        usage = SimpleNamespace(prompt_tokens=10_000, completion_tokens=5_000)

        self.assertEqual(_snowflakes_from_usage(usage, config), 6.0)
        self.assertEqual(_snowflakes_from_usage(None, config), 0.0)

    def test_cached_prompt_tokens_are_never_billed(self) -> None:
        config = Config.from_dict(
            {
                "b50_llm_input_price_per_million": 10,
                "b50_llm_output_price_per_million": 1,
            }
        )
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 600},
        }

        self.assertEqual(_snowflakes_from_usage(usage, config), 0.4)

    def test_deepseek_usage_sample_has_nonzero_default_cost(self) -> None:
        usage = {
            "prompt_tokens": 15,
            "completion_tokens": 271,
            "total_tokens": 286,
            "prompt_tokens_details": {"cached_tokens": 0},
        }

        self.assertEqual(_snowflakes_from_usage(usage, Config.from_dict({})), 0.06)

    def test_shared_data_assets_are_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            data_dir = Path(temp_name) / "data"
            plugin = object.__new__(MaiB50Plugin)
            plugin.data_dir = data_dir / "plugin_data" / "astrbot_plugin_maib50"
            candidates = plugin._assets_candidates("")

        self.assertEqual(candidates[0], (data_dir / "assets").resolve())

    def test_nested_static_assets_are_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            data_dir = Path(temp_name) / "data"
            nested = data_dir / "assets" / "static"
            (nested / "font").mkdir(parents=True)
            (nested / "mai" / "pic").mkdir(parents=True)
            plugin = object.__new__(MaiB50Plugin)
            plugin.data_dir = data_dir / "plugin_data" / "astrbot_plugin_maib50"

            candidates = plugin._assets_candidates("")

        self.assertIn(nested.resolve(), candidates)

    def test_roast_alias_extracts_style(self) -> None:
        self.assertEqual(
            MaiB50Plugin._style_from_message("/锐评b50 重点看准度"),
            "重点看准度",
        )
        self.assertEqual(
            MaiB50Plugin._style_from_message("分析B50"),
            "",
        )

    def test_hallucinated_push_song_is_rejected(self) -> None:
        fallback = [
            {
                "music_id": "1",
                "level_index": 3,
                "title": "Known Song",
                "ds": 13.7,
                "achievement": 99.9,
            }
        ]
        raw = [
            {
                "music_id": "999",
                "level_index": 3,
                "title": "Invented Song",
                "ds": 14.9,
                "achievement": 99.0,
            }
        ]
        merged = _merge_push_recommendations(raw, fallback)
        self.assertEqual([item["title"] for item in merged], ["Known Song"])

        forged_fields = [
            {
                "title": "Known Song",
                "ds": 15.0,
                "achievement": 101.0,
            }
        ]
        merged = _merge_push_recommendations(forged_fields, fallback)
        self.assertEqual(merged[0]["ds"], 13.7)
        self.assertEqual(merged[0]["achievement"], 99.9)


if __name__ == "__main__":
    unittest.main()
