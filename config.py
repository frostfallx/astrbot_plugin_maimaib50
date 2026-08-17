from __future__ import annotations

from dataclasses import dataclass


def _int_value(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_value(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _positive_price(value: object, default: float) -> float:
    parsed = _float_value(value, default)
    return parsed if parsed > 0 else default


@dataclass(slots=True)
class Config:
    b50_llm_url: str = "https://api.openai.com/v1"
    b50_llm_key: str = ""
    b50_llm_model: str = "your-model-name"
    b50_llm_max_tokens: int = 2048
    b50_llm_input_price_per_million: float = 1.0
    b50_llm_output_price_per_million: float = 2.0
    b50_moderation_model: str = ""
    b50_assets_path: str = ""
    b50_daily_limit: int = 0
    maimaidxtoken: str = ""
    lxns_dev_token: str = ""
    lxns_api_base: str = "https://maimai.lxns.net/api/v0/maimai"
    maimai_alias_api: str = "https://www.yuzuchan.moe/api/maimaidx"
    query_timeout_seconds: int = 60
    analysis_timeout_seconds: int = 60

    @classmethod
    def from_dict(cls, raw: dict | None) -> Config:
        data = raw or {}
        defaults = cls()
        assets_path = str(data.get("b50_assets_path", "")).strip()
        return cls(
            b50_llm_url=str(data.get("b50_llm_url", defaults.b50_llm_url)).strip(),
            b50_llm_key=str(data.get("b50_llm_key", "")).strip(),
            b50_llm_model=str(
                data.get("b50_llm_model", defaults.b50_llm_model)
            ).strip(),
            b50_llm_max_tokens=max(
                1,
                min(
                    _int_value(
                        data.get("b50_llm_max_tokens"),
                        2048,
                    ),
                    65536,
                ),
            ),
            b50_llm_input_price_per_million=_positive_price(
                data.get("b50_llm_input_price_per_million"),
                defaults.b50_llm_input_price_per_million,
            ),
            b50_llm_output_price_per_million=_positive_price(
                data.get("b50_llm_output_price_per_million"),
                defaults.b50_llm_output_price_per_million,
            ),
            b50_moderation_model=str(data.get("b50_moderation_model", "")).strip(),
            b50_assets_path=assets_path,
            b50_daily_limit=max(
                0,
                _int_value(data.get("b50_daily_limit"), 0),
            ),
            maimaidxtoken=str(data.get("maimaidxtoken", "")).strip(),
            lxns_dev_token=str(data.get("lxns_dev_token", "")).strip(),
            lxns_api_base=str(data.get("lxns_api_base", defaults.lxns_api_base))
            .strip()
            .rstrip("/"),
            maimai_alias_api=str(
                data.get("maimai_alias_api", defaults.maimai_alias_api)
            )
            .strip()
            .rstrip("/"),
            query_timeout_seconds=max(
                10,
                min(
                    _int_value(
                        data.get("query_timeout_seconds"),
                        60,
                    ),
                    180,
                ),
            ),
            analysis_timeout_seconds=max(
                15,
                min(
                    _int_value(
                        data.get("analysis_timeout_seconds"),
                        60,
                    ),
                    300,
                ),
            ),
        )
