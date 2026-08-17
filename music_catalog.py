from __future__ import annotations

import asyncio
import json
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
from astrbot.api import logger

from .fetch import get_music_data


def _key(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _song_id(song: dict) -> str:
    return str(song.get("id") or song.get("song_id") or "").strip()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _unwrap_alias_payload(payload: Any) -> list[dict]:
    if isinstance(payload, dict) and isinstance(payload.get("content"), list):
        payload = payload["content"]
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


class MusicCatalog:
    """Small read-only song/alias index based on the upstream query bots."""

    def __init__(self) -> None:
        self.songs: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.aliases_by_id: dict[str, set[str]] = defaultdict(set)
        self.ids_by_alias: dict[str, set[str]] = defaultdict(set)

    def set_songs(self, songs: list[dict]) -> None:
        self.aliases_by_id.clear()
        self.ids_by_alias.clear()
        self.songs = [item for item in songs if _song_id(item)]
        self.by_id = {_song_id(item): item for item in self.songs}
        for sid, song in self.by_id.items():
            title = str(song.get("title") or "").strip()
            if title:
                self._add_alias(sid, title)

    def _add_alias(self, sid: str, alias: object) -> None:
        value = str(alias or "").strip()
        key = _key(value)
        if not sid or not key:
            return
        self.aliases_by_id[sid].add(value)
        self.ids_by_alias[key].add(sid)

    def merge_alias_payload(self, payload: Any) -> None:
        if isinstance(payload, dict) and not isinstance(payload.get("content"), list):
            for sid, aliases in payload.items():
                if isinstance(aliases, str):
                    aliases = [aliases]
                for alias in aliases if isinstance(aliases, list) else []:
                    self._add_alias(str(sid), alias)
            return

        for item in _unwrap_alias_payload(payload):
            sid = str(item.get("SongID") or item.get("song_id") or item.get("id") or "")
            name = item.get("Name") or item.get("name")
            if name:
                self._add_alias(sid, name)
            aliases = item.get("Alias") or item.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            for alias in aliases if isinstance(aliases, list) else []:
                self._add_alias(sid, alias)

    def song(self, sid: object) -> dict | None:
        raw = str(sid or "").strip()
        if raw in self.by_id:
            return self.by_id[raw]
        normalized = raw.lstrip("0") or "0"
        return self.by_id.get(normalized)

    def search(self, query: str, limit: int = 50) -> list[dict]:
        needle = _key(query)
        if not needle:
            return []
        if needle.isdigit() and (song := self.song(needle)):
            return [song]
        exact = [song for song in self.songs if _key(song.get("title")) == needle]
        if exact:
            return sorted(exact, key=lambda item: int(_song_id(item)))[:limit]
        matches = [song for song in self.songs if needle in _key(song.get("title"))]
        return sorted(matches, key=lambda item: int(_song_id(item)))[:limit]

    def exact_title(self, query: str) -> list[dict]:
        needle = _key(query)
        return [song for song in self.songs if _key(song.get("title")) == needle]

    def resolve_alias(self, alias: str) -> list[dict]:
        ids = sorted(self.ids_by_alias.get(_key(alias), set()), key=int)
        return [self.by_id[sid] for sid in ids if sid in self.by_id]

    def aliases(self, value: str) -> list[tuple[dict, list[str]]]:
        songs = [self.song(value)] if value.isdigit() else self.resolve_alias(value)
        result: list[tuple[dict, list[str]]] = []
        for song in songs:
            if not song:
                continue
            sid = _song_id(song)
            title_key = _key(song.get("title"))
            aliases = sorted(
                (a for a in self.aliases_by_id.get(sid, set()) if _key(a) != title_key),
                key=_key,
            )
            result.append((song, aliases))
        return result


catalog = MusicCatalog()


async def initialize_catalog(assets_path: str, data_dir: Path, alias_api: str) -> None:
    catalog.set_songs(get_music_data())
    if not catalog.songs:
        logger.warning("歌曲详情与别名查询暂不可用：曲库数据未加载")
        return

    assets = Path(assets_path)
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_path = data_dir / "music_alias.json"
    remote_payload: Any = None
    if alias_api:
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                response = await client.get(f"{alias_api.rstrip('/')}/maimaidxalias")
                response.raise_for_status()
                remote_payload = response.json()
            if _unwrap_alias_payload(remote_payload):
                serialized = json.dumps(remote_payload, ensure_ascii=False, indent=2)
                await asyncio.to_thread(
                    cache_path.write_text, serialized, encoding="utf-8"
                )
                logger.info("已更新柚子歌曲别名缓存")
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.warning(f"更新歌曲别名失败，改用本地缓存: {exc}")

    sources = [
        remote_payload,
        _read_json(cache_path),
        _read_json(assets / "music_alias.json"),
        _read_json(assets / "data" / "music_alias.json"),
        _read_json(assets / "local_music_alias.json"),
        _read_json(assets / "data" / "local_music_alias.json"),
    ]
    for payload in sources:
        if payload is not None:
            catalog.merge_alias_payload(payload)
    logger.info(
        f"歌曲查询已加载 {len(catalog.songs)} 首曲目、"
        f"{len(catalog.ids_by_alias)} 个别名键"
    )


def song_id(song: dict) -> str:
    return _song_id(song)
