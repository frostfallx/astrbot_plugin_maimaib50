from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

import httpx
from astrbot.api import logger

from .config import Config
from .data_source import DIVING_FISH, LXNS, SOURCE_LABELS

WATER_FISH_BASE = "https://www.diving-fish.com/api/maimaidxprober"

_cfg = Config()
_music_data_cache: list[dict] | None = None


class DataSourceResponseError(ValueError):
    """The selected score source returned a payload with an unexpected shape."""


class PlayerNotFoundError(ValueError):
    """The selected score source could not resolve the requested player."""


def configure(config: Config) -> None:
    global _cfg
    _cfg = config


def _get_dev_token() -> str:
    return (_cfg.maimaidxtoken or "").strip()


def _missing_player_message(qq: str) -> str:
    return (
        f"用户不存在或未开放 B50 查询（QQ: {qq}）；\n"
        "请确保已配置水鱼导入令牌/落雪API秘钥，并绑定QQ。"
    )


def _response_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("message", "detail", "error"):
        value = payload.get(key)
        if value:
            return str(value).strip()
    return ""


async def init_music_data() -> None:
    global _music_data_cache
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{WATER_FISH_BASE}/music_data")
            resp.raise_for_status()
            _music_data_cache = resp.json()
            logger.info("获取到水鱼数据了捏")
            return
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(f"从 API 加载曲库失败: {e}")

    try:
        assets_path = (
            Path(_cfg.b50_assets_path) / "music_data.json"
            if _cfg.b50_assets_path
            else Path(__file__).parent / "assets" / "music_data.json"
        )
        if assets_path.exists():
            raw = await asyncio.to_thread(assets_path.read_text, encoding="utf-8")
            _music_data_cache = json.loads(raw)
            logger.info("已从本地 assets 加载曲库数据")
            return
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        logger.error(f"加载本地曲库数据失败: {e}")

    _music_data_cache = None


def get_music_lookup() -> dict[str, dict] | None:
    if not _music_data_cache:
        return None
    lookup: dict[str, dict] = {}
    for m in _music_data_cache:
        mid = str(m.get("id") or "")
        if mid:
            lookup[mid] = m
    return lookup or None


def get_music_data() -> list[dict]:
    """Return the loaded Diving-Fish catalog without exposing the cache itself."""
    return [dict(item) for item in (_music_data_cache or []) if isinstance(item, dict)]


async def fetch_player_data(qq: str, source: str = DIVING_FISH) -> dict:
    if source == LXNS:
        result = await _fetch_lxns_b50(qq)
        result["_source"] = SOURCE_LABELS[LXNS]
        return result
    dev_token = _get_dev_token()
    if dev_token:
        result = await _fetch_dev_records(qq, dev_token)
        if result is not None:
            result["_source"] = SOURCE_LABELS[DIVING_FISH]
            return result
    result = await _fetch_public_b50(qq)
    result["_source"] = SOURCE_LABELS[DIVING_FISH]
    return result


def source_available(source: str) -> bool:
    if source == LXNS:
        return bool(_cfg.lxns_dev_token and _cfg.lxns_api_base)
    return True


async def fetch_music_records(qq: str, song: dict, source: str) -> list[dict]:
    if source == LXNS:
        return await _fetch_lxns_music_records(qq, song)
    return await _fetch_diving_fish_music_records(qq, song)


async def _fetch_public_b50(qq: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{WATER_FISH_BASE}/query/player",
            json={"qq": int(qq), "b50": True},
        )
    if resp.status_code in {400, 403}:
        raise ValueError(_missing_player_message(qq))
    resp.raise_for_status()
    return resp.json()


async def _fetch_dev_records(qq: str, dev_token: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(
                f"{WATER_FISH_BASE}/dev/player/records",
                params={"qq": int(qq)},
                headers={"developer-token": dev_token},
            )
        if resp.status_code != 200:
            detail = _response_message(resp)
            logger.warning(
                "水鱼开发者完整成绩接口返回 "
                f"HTTP {resp.status_code}{f'：{detail}' if detail else ''}；"
                "B50 将回退到公开接口"
            )
            return None
        data = resp.json()
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        logger.warning(f"水鱼开发者完整成绩接口请求失败：{exc}；B50 将回退到公开接口")
        return None

    records = data.get("records") or []
    if not records:
        return None

    lookup = get_music_lookup()
    old, new = _sort_old_new(records, lookup)
    total_ra = sum(_i(c.get("ra")) for c in old) + sum(_i(c.get("ra")) for c in new)
    return {
        "nickname": str(data.get("nickname") or f"Player({qq})"),
        "rating": _i(data.get("rating")) or total_ra,
        "additional_rating": _i(data.get("additional_rating")),
        "plate": str(data.get("plate") or ""),
        "charts": {"sd": old, "dx": new},
    }


async def _fetch_diving_fish_music_records(qq: str, song: dict) -> list[dict]:
    sid = str(song.get("id") or song.get("song_id") or "")
    dev_token = _get_dev_token()
    if dev_token:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{WATER_FISH_BASE}/dev/player/record",
                json={"qq": int(qq), "music_id": sid},
                headers={"developer-token": dev_token},
            )
        if response.status_code == 400:
            detail = _response_message(response)
            if detail.casefold() in {"no such user", "user not exists"}:
                raise ValueError(_missing_player_message(qq))
            raise ValueError(
                "水鱼开发者 Token 不可用"
                f"{f'：{detail}' if detail else '，请检查 Token 是否正确、有效且未被禁用'}"
            )
        if response.status_code == 403:
            detail = _response_message(response)
            raise ValueError(
                f"水鱼拒绝查询{f'：{detail}' if detail else '，请检查用户协议和隐私设置'}"
            )
        if response.status_code == 401:
            raise ValueError("水鱼开发者 Token 无效")
        if response.status_code == 429:
            raise ValueError("水鱼开发者 Token 请求次数已达上限，请稍后重试")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            records = payload.get(sid) or payload.get(str(int(sid))) or []
            if isinstance(records, list):
                return [dict(record) for record in records if isinstance(record, dict)]

    public = await _fetch_public_b50(qq)
    records = [
        dict(record)
        for group in (
            (public.get("charts") or {}).get("sd") or [],
            (public.get("charts") or {}).get("dx") or [],
        )
        for record in group
        if str(record.get("song_id") or record.get("music_id") or "") == sid
    ]
    if not records and not dev_token:
        raise ValueError(
            "这首歌不在当前公开 B50 中；查询完整个人单曲成绩需要在插件设置中配置 maimaidxtoken"
        )
    return records


RATE_ACHIEVEMENTS = {
    "sssp": 100.5,
    "sss": 100.0,
    "ssp": 99.5,
    "ss": 99.0,
    "sp": 98.0,
    "s": 97.0,
    "aaa": 94.0,
    "aa": 90.0,
    "a": 80.0,
    "bbb": 75.0,
    "bb": 70.0,
    "b": 60.0,
    "c": 50.0,
    "d": 0.0,
}


def _compute_ra(ds: float, achievement: float) -> int:
    thresholds = (
        (50, 7.0),
        (60, 8.0),
        (70, 9.6),
        (75, 11.2),
        (80, 12.0),
        (90, 13.6),
        (94, 15.2),
        (97, 16.8),
        (98, 20.0),
        (99, 20.3),
        (99.5, 20.8),
        (100, 21.1),
        (100.5, 21.6),
    )
    coefficient = 22.4
    for threshold, value in thresholds:
        if achievement < threshold:
            coefficient = value
            break
    return math.floor(ds * min(100.5, achievement) / 100 * coefficient)


def _lxns_record(score: dict) -> dict:
    raw_id = _i(score.get("id") or score.get("song_id"))
    kind = str(score.get("type") or "standard").casefold()
    sid = raw_id + 10000 if kind == "dx" and raw_id < 10000 else raw_id
    index = _i(score.get("level_index"), -1)
    music = (get_music_lookup() or {}).get(str(sid)) or {}
    constants = music.get("ds") or []
    levels = music.get("level") or []
    ds = _f(constants[index]) if 0 <= index < len(constants) else _f(score.get("ds"))
    rate = str(score.get("rate") or "d").casefold()
    achievements = score.get("achievements")
    if achievements is None:
        achievements = RATE_ACHIEVEMENTS.get(rate, 0.0)
    achievement = _f(achievements)
    return {
        "song_id": sid,
        "music_id": str(sid),
        "title": str(music.get("title") or score.get("song_name") or ""),
        "level": levels[index]
        if 0 <= index < len(levels)
        else str(score.get("level") or ""),
        "level_index": index,
        "level_label": ("Basic", "Advanced", "Expert", "Master", "Re:Master")[index]
        if 0 <= index <= 4
        else "",
        "ds": ds,
        "achievements": achievement,
        "achievement": achievement,
        "fc": str(score.get("fc") or ""),
        "fs": str(score.get("fs") or ""),
        "rate": rate,
        "dxScore": _i(score.get("dx_score") or score.get("dxScore")),
        "ra": _i(score.get("dx_rating")) or _compute_ra(ds, achievement),
        "type": "DX" if kind == "dx" else ("UTAGE" if kind == "utage" else "SD"),
    }


async def _lxns_get(path: str, **kwargs: Any) -> dict:
    if not _cfg.lxns_dev_token:
        raise ValueError("BOT 管理员未在插件设置中配置 lxns_dev_token")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{_cfg.lxns_api_base}{path}",
            headers={"Authorization": _cfg.lxns_dev_token},
            **kwargs,
        )
    if response.status_code == 404:
        raise PlayerNotFoundError("未查询到落雪账号")
    if response.status_code == 403:
        raise ValueError("落雪无权读取该账号，请检查隐私设置")
    if response.status_code == 401:
        raise ValueError("落雪开发者 Token 无效")
    if response.status_code == 429:
        raise ValueError("落雪请求过于频繁，请稍后重试")
    response.raise_for_status()
    payload = response.json()
    return payload.get("data", payload) if isinstance(payload, dict) else {}


async def _fetch_lxns_player(qq: str) -> dict:
    try:
        data = await _lxns_get(f"/player/qq/{int(qq)}")
    except PlayerNotFoundError as exc:
        raise ValueError(_missing_player_message(qq)) from exc
    if not isinstance(data, dict) or not data.get("friend_code"):
        raise ValueError(_missing_player_message(qq))
    return data


async def _fetch_lxns_b50(qq: str) -> dict:
    player = await _fetch_lxns_player(qq)
    bests = await _lxns_get(f"/player/{int(player['friend_code'])}/bests")
    if not isinstance(bests, dict):
        raise DataSourceResponseError("落雪返回的 B50 数据格式不正确")
    standard = [
        _lxns_record(item)
        for item in (bests.get("standard") or [])
        if isinstance(item, dict)
    ]
    dx = [
        _lxns_record(item) for item in (bests.get("dx") or []) if isinstance(item, dict)
    ]
    return {
        "nickname": str(player.get("name") or f"Player({qq})"),
        "rating": _i(player.get("rating"))
        or sum(_i(item.get("ra")) for item in standard + dx),
        "additional_rating": _i(player.get("course_rank")),
        "charts": {"sd": standard[:35], "dx": dx[:15]},
    }


async def _fetch_lxns_music_records(qq: str, song: dict) -> list[dict]:
    player = await _fetch_lxns_player(qq)
    sid = _i(song.get("id") or song.get("song_id"))
    kind = str(song.get("type") or "SD").casefold()
    if sid >= 100000:
        song_type, lxns_id = "utage", sid
    elif kind == "dx" or sid >= 10000:
        song_type = "dx"
        lxns_id = sid - 10000 if sid >= 10000 else sid
    else:
        song_type, lxns_id = "standard", sid
    data = await _lxns_get(
        f"/player/{int(player['friend_code'])}/bests",
        params={"song_id": lxns_id, "song_type": song_type},
    )
    if not isinstance(data, list):
        return []
    return [_lxns_record(item) for item in data if isinstance(item, dict)]


_NEW_VERSION_POOL = {"maimai でらっくす PRiSM PLUS"}


def _is_new(music_id: str, lookup: dict[str, dict] | None = None) -> bool:
    if not lookup:
        return False
    m = lookup.get(music_id)
    if not m:
        return False
    basic_info = m.get("basic_info", {})
    if "is_new" in basic_info:
        return bool(basic_info.get("is_new"))
    version = str(basic_info.get("from", "") or m.get("from", ""))
    return version in _NEW_VERSION_POOL


def _sort_old_new(
    records: list[dict], lookup: dict[str, dict] | None = None
) -> tuple[list[dict], list[dict]]:
    old, new = [], []
    for c in records:
        mid = str(c.get("song_id") or c.get("music_id") or "")
        is_n = _is_new(mid, lookup)
        c["is_new"] = is_n
        if is_n:
            new.append(c)
        else:
            old.append(c)
    old.sort(key=lambda x: _i(x.get("ra")), reverse=True)
    new.sort(key=lambda x: _i(x.get("ra")), reverse=True)
    return old, new


def _i(v: Any, d: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _f(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d
