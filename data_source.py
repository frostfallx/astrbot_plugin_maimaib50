from __future__ import annotations

import asyncio
import json
from pathlib import Path

DIVING_FISH = "divingfish"
LXNS = "lxns"
SOURCE_LABELS = {
    DIVING_FISH: "水鱼查分器（Diving-Fish）",
    LXNS: "落雪查分器（Lxns-Network）",
}
SOURCE_ALIASES = {
    "0": DIVING_FISH,
    "水鱼": DIVING_FISH,
    "diving-fish": DIVING_FISH,
    "divingfish": DIVING_FISH,
    "df": DIVING_FISH,
    "1": LXNS,
    "落雪": LXNS,
    "lxns": LXNS,
    "lxns-network": LXNS,
    "lx": LXNS,
}


class SourceStore:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.values: dict[str, str] = {}
        self.lock = asyncio.Lock()

    def configure(self, data_dir: Path) -> None:
        self.path = data_dir / "user_sources.json"
        self.values = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.values = {
                    str(user_id): source
                    for user_id, source in raw.items()
                    if source in SOURCE_LABELS
                }
        except (OSError, json.JSONDecodeError):
            pass

    def get(self, user_id: str) -> str:
        return self.values.get(str(user_id), DIVING_FISH)

    async def set(self, user_id: str, source: str) -> None:
        if source not in SOURCE_LABELS:
            raise ValueError(f"未知数据源：{source}")
        async with self.lock:
            self.values[str(user_id)] = source
            if self.path is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(self.values, ensure_ascii=False, indent=2)
            await asyncio.to_thread(self.path.write_text, text, encoding="utf-8")


source_store = SourceStore()


def parse_source(value: str) -> str | None:
    return SOURCE_ALIASES.get(value.strip().casefold())
