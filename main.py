from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .awmc_render import (
    missing_assets,
    render_play_info,
    render_song_info,
    valid_assets,
)
from .config import Config
from .context_builder import build_context, load_peer_stats
from .daily_limit import (
    configure_data_dir,
    get_today_usage,
    increment_usage,
    reset_user,
)
from .data_source import (
    LXNS,
    SOURCE_LABELS,
    parse_source,
    source_store,
)
from .fetch import configure as configure_fetch
from .fetch import (
    fetch_music_records,
    fetch_player_data,
    init_music_data,
    source_available,
)
from .llm import generate_analysis
from .moderation import check_user_input
from .moderation import configure as configure_moderation
from .music_catalog import catalog, initialize_catalog, song_id
from .render import prepare_render_cache, render_b50_image, render_image

_ANALYSIS_COMMAND_PATTERN = r"(?:分析(?i:b50)|锐评(?i:b50)|(?i:b50)分析)"
SOURCE_REPOSITORY = "https://github.com/frostfallx/astrbot_plugin_maimaib50"


class AnalysisRejected(RuntimeError):
    def __init__(self, message: str, *, consume_usage: bool = False):
        super().__init__(message)
        self.consume_usage = consume_usage


class MaiB50Plugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = Config.from_dict(config)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_maib50")
        configured_assets = self.config.b50_assets_path
        self.assets_search_paths = self._assets_candidates(configured_assets)
        selected_assets = next(
            (path for path in self.assets_search_paths if self._valid_assets(path)),
            None,
        )
        if selected_assets is not None:
            self.config.b50_assets_path = str(selected_assets)
        elif self.assets_search_paths:
            self.config.b50_assets_path = str(self.assets_search_paths[0])
        self.render_dir = self.data_dir / "render_cache"
        self.render_dir.mkdir(parents=True, exist_ok=True)
        configure_data_dir(self.data_dir)
        source_store.configure(self.data_dir)
        configure_fetch(self.config)
        configure_moderation(self.config)
        self.peer_stats: dict | None = None
        self.active_users: set[str] = set()
        self.usage_lock = asyncio.Lock()
        self.cleanup_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _valid_assets(path: Path) -> bool:
        return valid_assets(path)

    def _assets_candidates(self, configured: str) -> tuple[Path, ...]:
        plugin_dir = Path(__file__).resolve().parent
        astrbot_data_dir = self.data_dir.parent.parent
        raw = Path(configured).expanduser() if configured else None
        roots: list[Path] = []
        if raw is not None:
            if raw.is_absolute():
                roots.append(raw)
            else:
                roots.extend((plugin_dir / raw, self.data_dir / raw, Path.cwd() / raw))
        roots.extend(
            (
                astrbot_data_dir / "assets",
                plugin_dir / "assets",
                self.data_dir / "assets",
                Path.cwd() / "data" / "assets",
                Path.cwd() / "assets",
            )
        )
        candidates: list[Path] = []
        for root in roots:
            candidates.append(root)
            candidates.extend(
                nested
                for nested in (root / "static", root / "assets")
                if nested.is_dir()
            )
            if not root.is_dir():
                continue
            for pattern in ("*", "*/*"):
                try:
                    nested_paths = root.glob(pattern)
                    candidates.extend(
                        nested
                        for nested in nested_paths
                        if nested.is_dir()
                        and (
                            (nested / "font").is_dir()
                            or (nested / "mai" / "pic").is_dir()
                        )
                    )
                except OSError:
                    continue
        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            resolved = candidate.resolve()
            key = str(resolved)
            if key not in seen:
                seen.add(key)
                unique.append(resolved)
        return tuple(unique)

    async def initialize(self):
        self.peer_stats = await asyncio.to_thread(
            load_peer_stats, self.config.b50_assets_path
        )
        await init_music_data()
        await initialize_catalog(
            self.config.b50_assets_path,
            self.data_dir,
            self.config.maimai_alias_api,
        )
        if not self.config.b50_llm_key:
            logger.warning("未在插件设置中配置 LLM API Key；普通 b50 查询仍可使用")
        assets_path = Path(self.config.b50_assets_path)
        if self._valid_assets(assets_path):
            logger.info(f"B50 分析插件使用 assets 目录: {assets_path}")
        else:
            checked = "、".join(str(path) for path in self.assets_search_paths)
            logger.warning(
                f"B50 分析插件未找到有效 assets 目录，已检查: {checked or '无'}"
            )
        logger.info("舞萌 DX B50 插件初始化完成")

    @staticmethod
    def _style_from_message(message: str) -> str:
        text = message.strip().removeprefix("/").strip()
        return re.sub(
            rf"^{_ANALYSIS_COMMAND_PATTERN}(?:\s+|$)",
            "",
            text,
            count=1,
        ).strip()

    @staticmethod
    def _analysis_target_and_style(event: AstrMessageEvent) -> tuple[str, str]:
        requester_id = str(event.get_sender_id())
        argument = MaiB50Plugin._style_from_message(event.message_str)
        parts = argument.split(maxsplit=1)
        if not parts or not re.fullmatch(r"[0-9]+", parts[0]):
            return requester_id, argument
        if not MaiB50Plugin._is_admin(event):
            raise AnalysisRejected("只有管理员可以指定 QQ 号生成他人的 B50 锐评")
        return parts[0], parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _query_target(event: AstrMessageEvent) -> str:
        text = event.message_str.strip().removeprefix("/").strip()
        argument = re.sub(r"^(?i:b50)(?:\s+|$)", "", text, count=1).strip()
        if argument.isdigit():
            return argument
        for component in getattr(event.message_obj, "message", []):
            if component.__class__.__name__.lower() != "at":
                continue
            target = str(getattr(component, "qq", "") or "").strip()
            if target.isdigit():
                return target
        return str(event.get_sender_id())

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        check = getattr(event, "is_admin", None)
        if callable(check):
            return bool(check())
        return getattr(event, "role", "member") == "admin"

    async def _usage(self, user_id: str) -> int:
        async with self.usage_lock:
            return await asyncio.to_thread(get_today_usage, user_id)

    async def _increment_usage(self, user_id: str) -> None:
        async with self.usage_lock:
            await asyncio.to_thread(increment_usage, user_id)

    def _require_assets(self) -> None:
        assets_path = Path(self.config.b50_assets_path)
        if self._valid_assets(assets_path):
            return
        checked = "\n".join(f"- {path}" for path in self.assets_search_paths)
        closest_path = assets_path
        missing = missing_assets(assets_path)
        for candidate in self.assets_search_paths:
            candidate_missing = missing_assets(candidate)
            if len(candidate_missing) < len(missing):
                closest_path = candidate
                missing = candidate_missing
        detail = "、".join(missing) if missing else "font/ 与 mai/pic/"
        raise AnalysisRejected(
            "未找到有效的 AWMC/Yuzu 素材目录。最接近的目录是："
            f"{closest_path}\n缺少：{detail}。有效素材根目录应同时包含 "
            "font/ 和 mai/pic/；常见的 assets/static 嵌套结构会自动识别。"
            "已检查：\n"
            f"{checked or '- 未配置路径'}"
        )

    async def _build_context(self, user_id: str, source: str) -> dict:
        try:
            b50_data = await fetch_player_data(user_id, source)
        except ValueError as exc:
            raise AnalysisRejected(str(exc)) from exc
        except Exception as exc:
            logger.exception("获取 B50 数据失败")
            raise AnalysisRejected("查询失败，请稍后重试") from exc

        b50_data["_assets_path"] = self.config.b50_assets_path
        context = await asyncio.to_thread(
            build_context,
            b50_data,
            self.peer_stats,
        )
        context.setdefault("player", {})["qq"] = user_id
        if not any(
            item.get("bucket") in {"B35", "B15"}
            for item in (context.get("b50") or [])
            if isinstance(item, dict)
        ):
            raise AnalysisRejected("没有获取到可展示的 B35/B15 成绩")
        return context

    async def _save_rendered_image(
        self,
        context: dict,
        renderer: Callable[[], Any],
        prefix: str,
    ) -> Path:
        await prepare_render_cache(context, self.config.b50_assets_path)
        image = await asyncio.to_thread(renderer)
        output_path = self.render_dir / (
            f"{prefix}-{context['player']['qq']}-{uuid.uuid4().hex}.png"
        )
        try:
            await asyncio.to_thread(image.save, output_path, "PNG")
        finally:
            image.close()
        return output_path

    async def _query(self, user_id: str, source: str) -> Path:
        self._require_assets()
        context = await self._build_context(user_id, source)
        try:
            return await self._save_rendered_image(
                context,
                lambda: render_b50_image(
                    context,
                    self.config.b50_assets_path,
                ),
                "b50",
            )
        except Exception as exc:
            logger.exception("渲染 B50 图片失败")
            raise AnalysisRejected(f"制图失败：{exc}") from exc

    async def _song_info(self, song: dict) -> Path:
        self._require_assets()
        sid = song_id(song)
        await prepare_render_cache(
            {"player": {}, "b50": [{"music_id": sid, "bucket": "B35"}]},
            self.config.b50_assets_path,
        )
        try:
            image = await asyncio.to_thread(
                render_song_info,
                song,
                self.config.b50_assets_path,
            )
            output_path = self.render_dir / f"song-{sid}-{uuid.uuid4().hex}.png"
            try:
                await asyncio.to_thread(image.save, output_path, "PNG")
            finally:
                image.close()
            return output_path
        except Exception as exc:
            logger.exception("渲染歌曲详情失败")
            raise AnalysisRejected(f"歌曲详情制图失败：{exc}") from exc

    async def _play_info(self, user_id: str, song: dict, source: str) -> Path:
        self._require_assets()
        sid = song_id(song)
        try:
            records = await fetch_music_records(user_id, song, source)
        except ValueError as exc:
            raise AnalysisRejected(str(exc)) from exc
        except Exception as exc:
            logger.exception("获取个人单曲成绩失败")
            raise AnalysisRejected("个人单曲成绩查询失败，请稍后重试") from exc
        if not records:
            raise AnalysisRejected(
                f"你在当前数据源中没有「{song.get('title') or sid}」的游玩记录"
            )
        await prepare_render_cache(
            {"player": {"qq": user_id}, "b50": [{"music_id": sid, "bucket": "B35"}]},
            self.config.b50_assets_path,
        )
        try:
            image = await asyncio.to_thread(
                render_play_info,
                song,
                records,
                self.config.b50_assets_path,
                SOURCE_LABELS[source],
            )
            output_path = self.render_dir / f"info-{sid}-{uuid.uuid4().hex}.png"
            try:
                await asyncio.to_thread(image.save, output_path, "PNG")
            finally:
                image.close()
            return output_path
        except Exception as exc:
            logger.exception("渲染个人单曲成绩失败")
            raise AnalysisRejected(f"个人单曲成绩制图失败：{exc}") from exc

    @staticmethod
    def _search_argument(message: str) -> str:
        text = message.strip().removeprefix("/").strip()
        return re.sub(r"^(?:查歌|(?i:search))(?:\s+|$)", "", text, count=1).strip()

    async def _analyze(
        self, user_id: str, style: str, source: str
    ) -> tuple[Path, float]:
        self._require_assets()
        if not self.config.b50_llm_key:
            raise AnalysisRejected(
                "未在插件设置中配置 b50_llm_key，锐评功能暂不可用；"
                "普通 b50 查询仍可使用"
            )

        if style:
            moderation = await check_user_input(style)
            if not moderation.get("allowed", True):
                raise AnalysisRejected(
                    moderation.get(
                        "reason",
                        "请求包含不适合处理的内容，本次分析已驳回（消耗使用次数）",
                    ),
                    consume_usage=True,
                )

        context = await self._build_context(user_id, source)

        try:
            analysis_text, snowflakes = await generate_analysis(
                context, self.config, style
            )
        except Exception as exc:
            logger.exception("生成 B50 分析失败")
            raise AnalysisRejected(f"分析生成失败：{exc}") from exc

        try:
            parsed = json.loads(analysis_text)
            recommendations = parsed.get("push_recommendations")
            if isinstance(recommendations, list):
                context.setdefault("evidence", {})["push_recommendations"] = (
                    recommendations
                )
        except (TypeError, json.JSONDecodeError):
            pass

        try:
            image_path = await self._save_rendered_image(
                context,
                lambda: render_image(
                    context,
                    analysis_text,
                    self.config.b50_assets_path,
                ),
                "analysis",
            )
            return image_path, snowflakes
        except Exception as exc:
            logger.exception("渲染 B50 分析图失败")
            raise AnalysisRejected(f"制图失败：{exc}") from exc

    async def _remove_later(self, path: Path, delay: float = 60.0) -> None:
        await asyncio.sleep(delay)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning(f"无法清理 B50 临时图片: {path}")

    def _schedule_cleanup(self, path: Path) -> None:
        task = asyncio.create_task(self._remove_later(path))
        self.cleanup_tasks.add(task)
        task.add_done_callback(self.cleanup_tasks.discard)

    @filter.regex(r"^/?(?:源码|(?i:source))$")
    async def source_information(self, event: AstrMessageEvent):
        yield event.plain_result(
            f"Source code: {SOURCE_REPOSITORY}\nLicense: AGPL-3.0-only"
        )

    @filter.regex(r"^/?(?i:b50)(?:\s+(?:\d+|.*@.*))?$")
    async def query_b50(self, event: AstrMessageEvent):
        requester_id = str(event.get_sender_id())
        if requester_id in self.active_users:
            yield event.plain_result("您正在查询 B50，请稍等完成后再试～")
            return

        self.active_users.add(requester_id)
        try:
            image_path = await asyncio.wait_for(
                self._query(
                    self._query_target(event),
                    source_store.get(requester_id),
                ),
                timeout=self.config.query_timeout_seconds,
            )
            yield event.image_result(str(image_path.resolve()))
            self._schedule_cleanup(image_path)
        except asyncio.TimeoutError:
            yield event.plain_result(
                f"查询超时（{self.config.query_timeout_seconds} 秒），请稍后重试"
            )
        except AnalysisRejected as exc:
            yield event.plain_result(str(exc))
        finally:
            self.active_users.discard(requester_id)

    @filter.regex(r"^/?(?:查歌|(?i:search))(?:\s+.+)?$")
    async def search_music(self, event: AstrMessageEvent):
        query = self._search_argument(event.message_str)
        if not query:
            yield event.plain_result("请输入歌曲关键词，例如：查歌 系ぎて")
            return
        if not catalog.songs:
            yield event.plain_result("歌曲数据尚未加载，请稍后重试")
            return
        results = catalog.search(query)
        if not results:
            yield event.plain_result(
                f"没有找到标题包含「{query}」的歌曲；如果输入的是别名，请用「{query}是什么歌」"
            )
            return
        if len(results) == 1:
            try:
                image_path = await self._song_info(results[0])
                yield event.image_result(str(image_path.resolve()))
                self._schedule_cleanup(image_path)
            except AnalysisRejected as exc:
                yield event.plain_result(str(exc))
            return
        lines = [f"找到 {len(results)} 首歌曲："]
        for song in results[:25]:
            lines.append(f"{song_id(song)}：{song.get('title') or '未知标题'}")
        if len(results) > 25:
            lines.append(f"……另有 {len(results) - 25} 首未显示")
        lines.append("发送「id 歌曲ID」查看谱面详情。")
        yield event.plain_result("\n".join(lines))

    @filter.regex(r"^/?(?i:id)\s+\d+$")
    async def music_by_id(self, event: AstrMessageEvent):
        text = event.message_str.strip().removeprefix("/").strip()
        sid = re.sub(r"^(?i:id)\s+", "", text, count=1).strip()
        song = catalog.song(sid)
        if not song:
            yield event.plain_result(f"没有找到 ID 为 {sid} 的歌曲")
            return
        try:
            image_path = await self._song_info(song)
            yield event.image_result(str(image_path.resolve()))
            self._schedule_cleanup(image_path)
        except AnalysisRejected as exc:
            yield event.plain_result(str(exc))

    @filter.regex(r"^/?(?i:info)(?:\s+.+)?$")
    async def personal_music_info(self, event: AstrMessageEvent):
        text = event.message_str.strip().removeprefix("/").strip()
        query = re.sub(r"^(?i:info)(?:\s+|$)", "", text, count=1).strip()
        if not query:
            yield event.plain_result("请输入完整歌名、别名或歌曲 ID，例如：info 123")
            return

        if query.isdigit() and (song := catalog.song(query)):
            results = [song]
        else:
            results = catalog.exact_title(query)
            if not results:
                results = catalog.resolve_alias(query)
        if not results:
            yield event.plain_result(
                f"没有找到「{query}」；info 只接受完整歌名、别名或歌曲 ID"
            )
            return
        if len(results) > 1:
            unique = {song_id(item): item for item in results}
            if len(unique) > 1:
                lines = [f"「{query}」对应多首歌曲，请改用 ID："]
                lines.extend(
                    f"{sid}：{item.get('title')} [{item.get('type') or 'SD'}]"
                    for sid, item in unique.items()
                )
                yield event.plain_result("\n".join(lines))
                return
            song = next(iter(unique.values()))
        else:
            song = results[0]

        user_id = str(event.get_sender_id())
        if user_id in self.active_users:
            yield event.plain_result("你已有查分任务正在进行，请稍后再试")
            return
        self.active_users.add(user_id)
        try:
            image_path = await asyncio.wait_for(
                self._play_info(user_id, song, source_store.get(user_id)),
                timeout=self.config.query_timeout_seconds,
            )
            yield event.image_result(str(image_path.resolve()))
            self._schedule_cleanup(image_path)
        except asyncio.TimeoutError:
            yield event.plain_result(
                f"查询超时（{self.config.query_timeout_seconds} 秒），请稍后重试"
            )
        except AnalysisRejected as exc:
            yield event.plain_result(str(exc))
        finally:
            self.active_users.discard(user_id)

    @filter.regex(r"^/?\S.*(?:是什么歌|是啥歌)$")
    async def music_by_alias(self, event: AstrMessageEvent):
        text = event.message_str.strip().removeprefix("/").strip()
        alias = re.sub(r"(?:是什么歌|是啥歌)$", "", text, count=1).strip()
        if not alias:
            yield event.plain_result("请在「是什么歌」前填写歌曲别名")
            return
        results = catalog.resolve_alias(alias)
        if not results and alias.isdigit() and catalog.song(alias):
            results = [catalog.song(alias)]
        if not results:
            yield event.plain_result(
                f"未找到别名为「{alias}」的歌曲；歌名片段请改用「查歌 {alias}」"
            )
            return
        if len(results) > 1:
            lines = [f"别名「{alias}」对应多首歌曲："]
            lines.extend(f"{song_id(song)}：{song.get('title')}" for song in results)
            lines.append("发送「id 歌曲ID」查看详情。")
            yield event.plain_result("\n".join(lines))
            return
        try:
            image_path = await self._song_info(results[0])
            yield event.image_result(str(image_path.resolve()))
            self._schedule_cleanup(image_path)
        except AnalysisRejected as exc:
            yield event.plain_result(str(exc))

    @filter.regex(r"^/?\S.*(?:有什么别名|有啥别名)$")
    async def list_music_aliases(self, event: AstrMessageEvent):
        text = event.message_str.strip().removeprefix("/").strip()
        value = re.sub(r"(?:有什么别名|有啥别名)$", "", text, count=1).strip()
        value = re.sub(r"^(?i:id)\s+", "", value, count=1).strip()
        results = catalog.aliases(value)
        if not results:
            yield event.plain_result(f"没有找到「{value}」对应的歌曲或别名")
            return
        blocks = []
        for song, aliases in results:
            header = f"{song_id(song)}：{song.get('title')}"
            blocks.append(
                header + "\n" + ("、".join(aliases) if aliases else "暂无额外别名")
            )
        yield event.plain_result("\n\n".join(blocks))

    @filter.regex(r"^/?数据源(?:\s+.*)?$")
    async def switch_data_source(self, event: AstrMessageEvent):
        text = event.message_str.strip().removeprefix("/").strip()
        argument = re.sub(r"^数据源(?:\s+|$)", "", text, count=1).strip()
        user_id = str(event.get_sender_id())
        current = source_store.get(user_id)
        if not argument:
            status = "已配置" if source_available(LXNS) else "未配置 lxns_dev_token"
            yield event.plain_result(
                f"当前数据源：「{SOURCE_LABELS[current]}」\n"
                "可发送「数据源 水鱼」或「数据源 落雪」切换。\n"
                f"落雪状态：{status}"
            )
            return

        source = parse_source(argument)
        if source is None:
            yield event.plain_result("未知数据源；可选：水鱼、落雪、0、1")
            return
        if source == LXNS and not source_available(LXNS):
            yield event.plain_result(
                "尚未在 AstrBot 插件设置中配置 lxns_dev_token，无法切换到落雪"
            )
            return
        await source_store.set(user_id, source)
        tip = ""
        if source == LXNS:
            tip = (
                "\n请确保已在落雪查分器绑定当前 QQ，并在隐私设置中允许第三方读取成绩。"
            )
        yield event.plain_result(f"数据源已切换为：「{SOURCE_LABELS[source]}」{tip}")

    @filter.regex(rf"^/?{_ANALYSIS_COMMAND_PATTERN}(?:\s+.*)?$")
    async def analyze_b50(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        try:
            target_id, style = self._analysis_target_and_style(event)
        except AnalysisRejected as exc:
            yield event.plain_result(str(exc))
            return
        if user_id in self.active_users:
            yield event.plain_result("您正在进行分析，请稍等完成后再试～")
            return

        limit = self.config.b50_daily_limit
        is_admin = self._is_admin(event)
        if limit > 0 and not is_admin and await self._usage(user_id) >= limit:
            yield event.plain_result(f"已经上限了哦，每天 {limit} 次，明天再来吧～")
            return

        self.active_users.add(user_id)
        try:
            yield event.plain_result(
                "正在生成，请耐心等待1~2分钟...\n"
                f"使用的分析模型：{self.config.b50_llm_model}"
            )
            image_path, snowflakes = await asyncio.wait_for(
                self._analyze(
                    target_id,
                    style,
                    source_store.get(user_id),
                ),
                timeout=self.config.analysis_timeout_seconds,
            )
            if limit > 0 and not is_admin:
                await self._increment_usage(user_id)
            yield event.image_result(str(image_path.resolve()))
            self._schedule_cleanup(image_path)
            snowflake_text = f"{snowflakes:.2f}".rstrip("0").rstrip(".")
            yield event.plain_result(
                f"本次请求消耗 {snowflake_text or '0'} 片雪花，您还剩 ∞ 片雪花。"
            )
        except asyncio.TimeoutError:
            yield event.plain_result(
                f"分析超时（{self.config.analysis_timeout_seconds} 秒），请稍后重试"
            )
        except AnalysisRejected as exc:
            if exc.consume_usage and limit > 0 and not is_admin:
                await self._increment_usage(user_id)
            yield event.plain_result(str(exc))
        finally:
            self.active_users.discard(user_id)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.regex(r"^/?(?:重置分析次数|重置分析)(?:\s+.*)?$")
    async def reset_analysis_usage(self, event: AstrMessageEvent):
        text = event.message_str.strip().removeprefix("/").strip()
        target = re.sub(
            r"^(?:重置分析次数|重置分析)(?:\s+|$)", "", text, count=1
        ).strip()
        if not target:
            for component in getattr(event.message_obj, "message", []):
                if component.__class__.__name__.lower() != "at":
                    continue
                target = str(getattr(component, "qq", "") or "").strip()
                if target:
                    break
        if not target or target.casefold() == "all" or not target.isdigit():
            yield event.plain_result("请指定要重置的用户 QQ 号或 @ 用户")
            return
        async with self.usage_lock:
            await asyncio.to_thread(reset_user, target)
        yield event.plain_result(f"已重置用户 {target} 的今日 B50 分析次数")

    async def terminate(self):
        for task in tuple(self.cleanup_tasks):
            task.cancel()
        if self.cleanup_tasks:
            await asyncio.gather(*self.cleanup_tasks, return_exceptions=True)
        for path in self.render_dir.glob("*.png"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
