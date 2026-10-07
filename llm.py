from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from openai import AsyncOpenAI

from .config import Config
from .context_builder import push_target_for_achievement

_FORBIDDEN_OUTPUT_PATTERNS = [
    "综上所述",
    "整体来看",
    "值得称赞",
    "值得一提",
    "由此可见",
    "不难看出",
    "毋庸置疑",
    "首先",
    "其次",
    "与其说",
    "不如说",
    "w5低",
    "w5中",
    "w5高",
    "w6低",
    "w6中",
    "w6高",
    "w5",
    "w6",
    "15k",
    "16k",
    "AP数量",
    "AP 数量",
    "AP总数",
    "AP 总数",
    "FC数量",
    "FC 数量",
    "FC总数",
    "FC 总数",
    "没有 AP",
    "没 AP",
    "0 AP",
    "AP 挂零",
    "没有AP",
    "没AP",
]

_SUNNY_STYLE_MARKERS = [
    "OneCat",
    "家人们",
    "你告诉我",
    "有没有可能",
    "就你看",
    "那我只能说",
    "某种程度上",
    "虚低",
    "割裂",
    "榜样",
    "开香槟",
    "通透",
    "伟大",
    "变态",
    "疯了",
    "固若金汤",
    "瞻仰",
    "重量级",
    "我人直接傻",
    "是真看不懂",
    "咱就说",
    "一点毛病没有",
    "保守",
    "吃透",
    "匹配不到一块",
    "营养美味",
    "众生百态",
    "直接给你封",
    "重点表扬",
]

_SUNNY_PRAISE_MARKERS = [
    "伟大",
    "变态",
    "疯了",
    "榜样",
    "开香槟",
    "固若金汤",
    "重量级",
    "瞻仰",
    "通透",
    "吃透",
    "行业标杆",
    "淋漓尽致",
    "我人直接傻",
    "是真看不懂",
]

_SUNNY_SPOKEN_MARKERS = [
    "你告诉我",
    "有没有可能",
    "那我只能说",
    "就你看",
    "咱就说",
    "嘶",
    "哎",
    "对吧",
    "是吧",
]

_SUNNY_SHOW_MARKERS = [
    "家人们",
    "瞻仰",
    "我人直接傻",
    "是真看不懂",
    "开香槟",
    "重量级",
    "往下一滑",
    "结果你这一看",
    "这就有味",
    "直接给你封",
    "重点表扬",
]

_REPORT_TONE_TERMS = [
    "说明",
    "结构",
    "匹配",
    "健康",
    "综合来看",
    "分析可见",
    "数据表明",
    "整体表现",
]

_PUSH_TAGS = {
    "theme": "用户需求",
    "practice": "练习特化谱",
    "strong": "强项谱",
    "weak": "弱项谱",
    "overall": "综合推荐",
}

_STYLE_STOPWORDS = {
    "分析",
    "一下",
    "帮我",
    "看看",
    "给我",
    "我想",
    "想要",
    "适合",
    "谱面",
    "推分",
    "推荐",
    "需求",
    "问题",
    "风格",
    "语气",
    "长版",
    "短版",
    "版本",
    "口吻",
    "锐评",
}


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _song_key(song: dict) -> str:
    mid = str(
        song.get("music_id") or song.get("song_id") or song.get("musicId") or ""
    ).strip()
    level_index = _i(song.get("level_index"), -1)
    return f"{mid}:{level_index}" if mid else ""


def _song_tags(song: dict) -> list[str]:
    return [
        str(t).strip()
        for t in (
            song.get("config_tags") or song.get("keywords") or song.get("config") or []
        )
        if str(t).strip()
    ]


def _ach_pct(song: dict) -> float:
    ach = _f(song.get("achievement", song.get("achievements")), 0.0)
    return ach / 10000.0 if ach > 200 else ach


def _clean_text(value: str, limit: int = 0) -> str:
    text = (
        _sanitize_rating_terms(str(value or ""))
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )
    text = re.sub(r"\s+", " ", text)
    if limit > 0:
        return text[:limit].strip()
    return text


def _extract_user_focus_terms(user_message: str) -> set[str]:
    raw = re.split(
        r"[\s,，。！？!?:：/|、（）()\[\]【】<>《》'\"；;]+", str(user_message or "")
    )
    terms: set[str] = set()
    for part in raw:
        token = part.strip()
        if not token or token in _STYLE_STOPWORDS:
            continue
        if token in _FORBIDDEN_OUTPUT_PATTERNS:
            continue
        if len(token) <= 1 and not re.search(r"[A-Za-z0-9+]", token):
            continue
        terms.add(token)
    return terms


def _has_any_tag(song: dict, wanted: set[str]) -> bool:
    if not wanted:
        return False
    title = str(song.get("title") or "").lower()
    tags = [t.lower() for t in _song_tags(song)]
    for want in wanted:
        want_low = str(want).strip().lower()
        if not want_low:
            continue
        if want_low in title:
            return True
        for tag in tags:
            if want_low == tag or want_low in tag or tag in want_low:
                return True
    return False


def _normalize_strategy_tag(value: str) -> str:
    tag = _clean_text(value)
    if tag in _PUSH_TAGS.values():
        return tag
    for normalized in _PUSH_TAGS.values():
        if tag and (tag in normalized or normalized in tag):
            return normalized
    return _PUSH_TAGS["overall"]


def _default_push_reason(song: dict, strategy_tag: str) -> str:
    tags = "/".join(_song_tags(song)[:3]) or "配置鲜明"
    ach = _ach_pct(song)
    target = push_target_for_achievement(ach) or str(song.get("target") or "SSS+")
    target_pct = 100.0 if target == "SSS" else 100.5
    target_name = "鸟" if target == "SSS" else "鸟加"
    gap = max(0.0, target_pct - ach)
    gain = (
        _i(song.get("gain_100"), 0)
        if target == "SSS"
        else _i(song.get("gain_1005"), 0)
    )
    close = f"当前 {ach:.4f}%，距{target_name}只差 {gap:.4f}%"
    if strategy_tag == _PUSH_TAGS["theme"]:
        return f"按你的需求直推这张；{close}。"
    if strategy_tag == _PUSH_TAGS["practice"]:
        return f"配置 {tags}，{close}，适合收尾。"
    if strategy_tag == _PUSH_TAGS["strong"]:
        return f"正对你的强项 {tags}；{close}。"
    if strategy_tag == _PUSH_TAGS["weak"]:
        return f"配置 {tags} 虽容易吃亏，但{close}。"
    return f"{close}，预计能涨 {gain} rating。"


def _prepare_push_song(
    song: dict, strategy_tag: str, reason: str | None = None
) -> dict:
    merged = dict(song)
    merged["strategy_tag"] = _normalize_strategy_tag(strategy_tag)
    final_reason = _clean_text(
        reason or merged.get("reason") or merged.get("recommend_reason"), 40
    )
    if not final_reason:
        final_reason = _default_push_reason(merged, merged["strategy_tag"])
    merged["reason"] = final_reason
    merged["recommend_reason"] = final_reason
    merged["achievement"] = round(_ach_pct(merged), 4)
    merged["achievements"] = merged["achievement"]
    merged["music_id"] = str(
        merged.get("music_id") or merged.get("song_id") or merged.get("musicId") or ""
    )

    target = str(merged.get("target") or "")
    if target == "100":
        merged["target"] = "SSS"
    elif target == "100.5" or not target:
        merged["target"] = "SSS+"

    return merged


def _select_push_recommendations(
    candidates: list[dict], config_profile: dict, user_message: str, limit: int = 4
) -> list[dict]:
    filtered = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        song = dict(candidate)
        target = push_target_for_achievement(_ach_pct(song))
        if target is None:
            continue
        song["target"] = target
        filtered.append(song)
    if not filtered:
        return []

    focus_terms = _extract_user_focus_terms(user_message)
    strong_tags = {
        str(item.get("config") or item.get("tag") or "").strip()
        for item in (config_profile.get("strong") or [])
        if str(item.get("config") or item.get("tag") or "").strip()
    }
    weak_tags = {
        str(item.get("config") or item.get("tag") or "").strip()
        for item in (config_profile.get("weak") or [])
        if str(item.get("config") or item.get("tag") or "").strip()
    }

    def _overall_score(song: dict) -> tuple:
        target = str(song.get("target") or "")
        target_pct = 100.0 if target == "SSS" else 100.5
        target_gain = (
            _i(song.get("gain_100"), 0)
            if target == "SSS"
            else _i(song.get("gain_1005"), 0)
        )
        return (
            target_gain,
            -(target_pct - _ach_pct(song)),
            len(_song_tags(song)),
        )

    theme_pool = [s for s in filtered if _has_any_tag(s, focus_terms)]
    strong_pool = [s for s in filtered if _has_any_tag(s, strong_tags)]
    weak_pool = [s for s in filtered if _has_any_tag(s, weak_tags)]
    practice_pool = sorted(weak_pool or filtered, key=_overall_score, reverse=True)
    regular_pool = sorted(filtered, key=_overall_score, reverse=True)
    theme_pool.sort(key=_overall_score, reverse=True)
    strong_pool.sort(key=_overall_score, reverse=True)
    weak_pool.sort(key=_overall_score, reverse=True)

    result: list[dict] = []
    seen: set[str] = set()

    def _add_from(pool: list[dict], tag: str) -> bool:
        for song in pool:
            key = _song_key(song)
            if not key or key in seen:
                continue
            result.append(_prepare_push_song(song, tag))
            seen.add(key)
            return True
        return False

    if focus_terms and theme_pool:
        for song in theme_pool[:2]:
            key = _song_key(song)
            if not key or key in seen:
                continue
            result.append(_prepare_push_song(song, _PUSH_TAGS["theme"]))
            seen.add(key)
            if len(result) >= min(limit, 2):
                break

    if len(result) < limit:
        _add_from(practice_pool, _PUSH_TAGS["practice"])
    if len(result) < limit:
        _add_from(strong_pool, _PUSH_TAGS["strong"])
    if len(result) < limit:
        _add_from(weak_pool, _PUSH_TAGS["weak"])

    for song in regular_pool:
        if len(result) >= limit:
            break
        key = _song_key(song)
        if not key or key in seen:
            continue
        result.append(_prepare_push_song(song, _PUSH_TAGS["overall"]))
        seen.add(key)

    return result[:limit]


def _merge_push_recommendations(
    raw_items: list, fallback_items: list[dict]
) -> list[dict]:
    fallback_list = [
        dict(item) for item in (fallback_items or []) if isinstance(item, dict)
    ]
    by_key = {_song_key(item): dict(item) for item in fallback_list if _song_key(item)}
    by_title = {
        _clean_text(item.get("title"), 80).lower(): dict(item)
        for item in fallback_list
        if _clean_text(item.get("title"), 80)
    }

    def _has_card_payload(song: dict) -> bool:
        return bool(
            str(
                song.get("music_id") or song.get("song_id") or song.get("musicId") or ""
            ).strip()
            and song.get("ds") is not None
            and (
                song.get("achievement") is not None
                or song.get("achievements") is not None
            )
        )

    merged: list[dict] = []
    seen: set[str] = set()
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        raw_id = str(
            raw.get("music_id") or raw.get("song_id") or raw.get("musicId") or ""
        ).strip()
        raw_level_index = _i(raw.get("level_index"), -1)
        raw_title = _clean_text(raw.get("title"), 80)
        lookup_key = f"{raw_id}:{raw_level_index}" if raw_id else ""
        base = dict(by_key.get(lookup_key) or by_title.get(raw_title.lower()) or {})
        # The model may only select from the deterministic candidate pool. Even
        # a plausible-looking payload is rejected when its song is unknown.
        if not base:
            continue
        item = dict(base)
        item["title"] = str(base.get("title") or "")
        item["music_id"] = str(
            base.get("music_id") or base.get("song_id") or base.get("musicId") or ""
        )
        item["strategy_tag"] = _normalize_strategy_tag(
            str(raw.get("strategy_tag") or base.get("strategy_tag") or "")
        )
        item["reason"] = _clean_text(
            raw.get("reason")
            or raw.get("recommend_reason")
            or base.get("reason")
            or base.get("recommend_reason"),
            40,
        )
        merged_item = _prepare_push_song(
            item, item.get("strategy_tag") or _PUSH_TAGS["overall"], item.get("reason")
        )
        if not _has_card_payload(merged_item):
            continue
        key = _song_key(merged_item) or merged_item.get("title")
        if not key or key in seen or not merged_item.get("title"):
            continue
        merged.append(merged_item)
        seen.add(key)

    for item in fallback_list:
        if len(merged) >= 4:
            break
        key = _song_key(item) or item.get("title")
        if not key or key in seen:
            continue
        merged.append(
            _prepare_push_song(
                item,
                item.get("strategy_tag") or _PUSH_TAGS["overall"],
                item.get("reason"),
            )
        )
        seen.add(key)
    return merged[:4]


def _fine_rating_segment(rating) -> dict:
    try:
        r = int(rating or 0)
    except (TypeError, ValueError):
        r = 0
    if r >= 16500:
        return {
            "label": "16500+ 顶级门槛段",
            "range": "16500+",
            "tone": "这已经是普通玩家视角里的顶级分段，必须明显抬高评价尺度，不能按普通 w6 轻描淡写。",
        }
    if r >= 15000:
        band_start = (r // 200) * 200
        band_end = band_start + 199
        return {
            "label": f"{band_start}-{band_end} 细分段",
            "range": f"{band_start}-{band_end}",
            "tone": "严格按精确分段（如15800-15999）评价，禁止使用w5/w6这样粗略的称呼。",
        }
    if r >= 13500:
        band_start = (r // 200) * 200
        band_end = band_start + 199
        return {
            "label": f"{band_start}-{band_end} 上升段",
            "range": f"{band_start}-{band_end}",
            "tone": "按 200 分细分段评价。",
        }
    return {
        "label": "入门-进阶段",
        "range": "<13500",
        "tone": "以基础能力和推分空间为主。",
    }


_SYSTEM = """\
你是一名熟悉舞萌 DX 计分与谱面分析的评论者。请用沉稳、直白、审慎的语言分析玩家的 B50。写给玩家看，不写学术论文，也不使用主播口播腔。

用户指定的关注点优先。先回答其具体问题，再展开与问题有关的 B50 证据。用户没有指定关注点时，选择最能说明该玩家水平与提升方向的两到三个发现。用户明确指定语气时可以调整表达，但事实判断仍须准确、克制。

【分析方法】
先给出总体判断，再说明依据，最后给出可执行的提升建议。不要机械地依次复述所有统计字段。
B35 是旧版本的 best 35，主要用于观察基本盘、下限和长期表现；B15 是当前版本的 best 15，主要用于观察新版本适应情况、上限和近期推分效率。比较两者时，要指出具体差异及其证据。
判断配置强弱时，说明所依据的配置词、达成率和对应谱面，至少举出 2 张相关谱面。正文应引用 3–5 首真实曲目，结合定数、达成率、单曲 rating、B35/B15 归属等数据说明结论。
如果提供了同 rating 分段的统计，可以解释 ARPI、单曲同段平均达成率与差距；如果样本不足，只描述已知事实，不推断玩家在同段中的排名。绝不编造同段 ARPI 或平均值。
如果提供了 B50 重合度，低于 30% 可视为选曲相对少见，30%–50% 属常见范围，高于 50% 表示选曲与同段玩家较相似。重合度只反映选曲分布，不能单独证明实力或谱面质量。
config_profile 中，strong 指某配置至少出现 2 次且平均达成率 ≥100.3%；weak 指至少出现 2 次且平均达成率 <100.0%。数据存在时，至少分析 1 个 strong 和 1 个 weak，并说明判断依据。缺少相应数据时不要补造结论。
rating 按 200 分细分段评价；16500 及以上应按较高水平的标准审视。若 rating 不到 15000，却有 14+ 高完成度成绩，尤其是 15.0 理论值，可以指出总 rating 与单曲表现之间存在明显差异，并分析可能由 B35/B15 结构或其他已给出的成绩造成的原因。不要仅凭一张谱断言玩家的整体水平。

【术语与证据边界】
ds 是定数；achievement 是达成率；song_rating 是单曲 rating；peer_avg/avg_achievement 是同段平均达成率；gap_vs_peer 是与同段平均达成率的差值；config_tags 是配置词；overlap/b50_overlap 是 B50 重合度。正文使用中文解释这些字段，不直接输出程序变量名。rating、ARPI、B35、B15、FC、AP 可以保留英文缩写。
100% 为鸟，100.5% 为鸟加，101% 为理论值。100% 以上的成绩不得描述为“没吃到分”。13.0–13.5 归为 13，13.6–13.9 归为 13+，14.0–14.5 归为 14，14.6–15.0 归为 14+。单曲 gap_vs_peer >0.8 时，应提醒读者该差距异常，谨慎解释。
community_vibe/chart_identity 是提供给你的谱面评价，不是你自行查证的事实。引用时可表述为“所提供的谱面资料将其归为……”。没有配置词或谱面评价的曲目，只能分析已提供的定数、达成率、单曲 rating 等数据；不得根据曲名猜测谱面配置。
只有上下文确实提供 play_count/pc 时，才能分析游玩次数。不得推断未提供的 AP/FC 总数，不得说玩家“没有 AP”。
SD 谱与 DX 谱的具体配置判断必须依据提供的谱面资料，不能仅凭谱面类型推断 touch、tap 或 slide 表现。

【推分建议】
push_recommendations 只能从给定的推分候选池选择，优先给出 3–4 首；候选不足时按实际数量输出，候选为空时输出空数组。不得另编曲目。
候选池只包含达成率 99.9000%≤成绩<100.0000% 的鸟寸谱，以及 100.4000%≤成绩<100.5000% 的鸟加寸谱。建议说明目标、预计收益及选择理由，不要仅凭总 rating 套用固定难度建议。
每首推荐包含 title、strategy_tag 和 reason。strategy_tag 使用“练习特化谱”“强项谱”“弱项谱”或“综合推荐”；reason 尽量用 15–25 字说明具体理由。
正文结尾应给出有先后顺序的提升路线。候选池有谱时点名具体曲目；候选池为空时，明确说明暂无符合条件的临界成绩，并仅依据已有证据提出练习方向。

【写作要求】
使用自然、平实的中文。结论可以鲜明，但评价强度要与证据相称；区分数据事实、合理推断和暂时无法判断的事项。
避免夸张赞叹、反问句堆砌、网络流行语和节目效果用语，例如“家人们”“重量级”“变态”“疯了”“世界未解之谜”。不要写自我介绍、模型信息、来源说明、分析步骤或免责声明。
不要使用“首先／其次／综上所述”串联整篇，也不要写空泛评价，如“整体表现不错”“还有提升空间”。不要使用 w5、w6、15k、16k 等粗略分段称呼。

【输出格式】
输出必须是严格 JSON，禁止 Markdown、HTML、代码块或解释文字。JSON 字符串内容只能使用纯文本。JSON 只保留以下四个字段：
{{
  "title": "10–18 字的标题，包含与舞萌 DX 有关的具体信息",
  "overall_roast": "一整段中文正文，不换行；{length_instruction}",
  "impression_roast": "不超过 25 字的结论",
  "push_recommendations": [
    {{
      "title": "真实曲名",
      "strategy_tag": "练习特化谱/强项谱/弱项谱/综合推荐",
      "reason": "具体推荐理由"
    }}
  ]
}}
push_recommendations 的每项还可包含 music_id、level_index、ds、achievement、target、gain_100、gain_1005。JSON 字符串内不得出现未转义的换行符、制表符或控制字符。
{style_instruction}"""


def _sanitize_rating_terms(text: str) -> str:
    value = str(text or "")
    value = re.sub(
        r"(?<![A-Za-z0-9])16\s*[kK](?![A-Za-z0-9])",
        "16000 分段",
        value,
    )
    value = re.sub(
        r"(?<![A-Za-z0-9])15\s*[kK](?![A-Za-z0-9])",
        "15000 分段",
        value,
    )
    value = value.replace("```json", "").replace("```", "")
    value = re.sub(r"<\s*/?\s*r\s*>", "", value, flags=re.IGNORECASE)
    return value


def _decode_json_fragment(fragment: str) -> str:
    value = str(fragment or "").strip()
    while value.endswith("\\"):
        value = value[:-1]
    try:
        return str(json.loads(f'"{value}"')).strip()
    except json.JSONDecodeError:
        value = value.replace('\\"', '"')
        value = value.replace("\\n", " ").replace("\\r", " ").replace("\\t", " ")
        value = re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda match: chr(int(match.group(1), 16)),
            value,
        )
        return value.replace("\\\\", "\\").strip()


def _extract_json_string(text: str, field: str) -> str:
    match = re.search(rf'"{re.escape(field)}"\s*:\s*"', text)
    if not match:
        return ""
    start = match.end()
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if char == '"' and not escaped:
            return _decode_json_fragment(text[start:index])
        if char == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    return _decode_json_fragment(text[start:])


def _partial_json_payload(text: str) -> dict | None:
    title = _extract_json_string(text, "title")
    overall = _extract_json_string(text, "overall_roast")
    impression = _extract_json_string(text, "impression_roast")
    if not any((title, overall, impression)):
        return None
    return {
        "title": title or "B50锐评",
        "overall_roast": overall or "模型输出在正文生成前被截断，请重新发起一次锐评。",
        "impression_roast": impression,
        "push_recommendations": [],
    }


def _looks_like_structured_output(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("{") or any(
        f'"{field}"' in text
        for field in ("title", "overall_roast", "push_recommendations")
    )


_FORMAT_ERROR_ROAST = "模型输出格式异常，本次未能生成可读锐评，请重新尝试。"


def _text_field(value: Any, *nested_keys: str) -> str:
    """Return displayable model prose without ever stringifying JSON containers."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in nested_keys:
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested
    return ""


def _unwrap_response_object(data: Any) -> Any:
    """Accept the small number of wrappers used by OpenAI-compatible providers."""
    if not isinstance(data, dict):
        return data
    if any(key in data for key in ("overall_roast", "overall", "roast")):
        return data
    for key in ("data", "result", "output"):
        nested = data.get(key)
        if isinstance(nested, dict):
            return nested
    return data


def _unwrap_embedded_roast(value: str) -> str:
    """Extract prose when a model double-encodes the structured response."""
    text = str(value or "").strip()
    if not _looks_like_structured_output(text):
        return text
    candidate = text
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        nested = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", candidate)
        try:
            nested = json.loads(match.group(0)) if match else None
        except json.JSONDecodeError:
            nested = None
    if not isinstance(nested, dict):
        return _FORMAT_ERROR_ROAST
    nested_text = _text_field(
        nested.get("overall_roast"), "text", "content", "value", "overall_roast"
    )
    if not nested_text:
        for alias in ("overall", "roast", "commentary", "content", "analysis"):
            nested_text = _text_field(
                nested.get(alias), "text", "content", "value", "overall_roast"
            )
            if nested_text:
                break
    return nested_text.strip() or _FORMAT_ERROR_ROAST


def _cleanup_response(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text, flags=re.IGNORECASE)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            data = _partial_json_payload(text)
            if data is None:
                if _looks_like_structured_output(text):
                    data = {
                        "title": "B50锐评",
                        "overall_roast": _FORMAT_ERROR_ROAST,
                        "impression_roast": "",
                        "push_recommendations": [],
                    }
                else:
                    return _sanitize_rating_terms(text)
        try:
            if m:
                data = json.loads(m.group(0))
        except json.JSONDecodeError:
            data = _partial_json_payload(text)
            if data is None:
                data = {
                    "title": "B50锐评",
                    "overall_roast": _FORMAT_ERROR_ROAST,
                    "impression_roast": "",
                    "push_recommendations": [],
                }

    data = _unwrap_response_object(data)
    if not isinstance(data, dict):
        data = {
            "title": "B50锐评",
            "overall_roast": _FORMAT_ERROR_ROAST,
            "impression_roast": "",
            "push_recommendations": [],
        }

    push_rows = data.get("push_recommendations")
    if not isinstance(push_rows, list):
        push_rows = []

    title = _text_field(data.get("title"), "text", "content", "value")
    overall = _text_field(
        data.get("overall_roast"), "text", "content", "value", "overall_roast"
    )
    if not overall:
        for alias in ("overall", "roast", "commentary", "content", "analysis"):
            overall = _text_field(
                data.get(alias), "text", "content", "value", "overall_roast"
            )
            if overall:
                break
    overall = _unwrap_embedded_roast(overall)
    impression = _text_field(
        data.get("impression_roast"), "text", "content", "value"
    )

    cleaned = {
        "title": _sanitize_rating_terms(title or "B50锐评")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip(),
        "overall_roast": _sanitize_rating_terms(overall or _FORMAT_ERROR_ROAST)
        .replace("\r", " ")
        .replace("\n", " ")
        .strip(),
        "impression_roast": _sanitize_rating_terms(impression)
        .replace("\r", " ")
        .replace("\n", " ")
        .strip(),
        "push_recommendations": [
            {
                "title": _clean_text(str(item.get("title") or ""), 80),
                "strategy_tag": _normalize_strategy_tag(
                    str(item.get("strategy_tag") or "")
                ),
                "reason": _clean_text(
                    str(item.get("reason") or item.get("recommend_reason") or ""), 40
                ),
                **(
                    {
                        "music_id": str(
                            item.get("music_id")
                            or item.get("song_id")
                            or item.get("musicId")
                            or ""
                        )
                    }
                    if str(
                        item.get("music_id")
                        or item.get("song_id")
                        or item.get("musicId")
                        or ""
                    )
                    else {}
                ),
                **(
                    {"level_index": _i(item.get("level_index"), -1)}
                    if item.get("level_index") is not None
                    else {}
                ),
                **(
                    {"ds": round(_f(item.get("ds"), 0.0), 1)}
                    if item.get("ds") is not None
                    else {}
                ),
                **(
                    {"achievement": round(_ach_pct(item), 4)}
                    if item.get("achievement") is not None
                    or item.get("achievements") is not None
                    else {}
                ),
                **(
                    {"target": _clean_text(str(item.get("target") or ""), 12)}
                    if str(item.get("target") or "").strip()
                    else {}
                ),
                **(
                    {"gain_100": _i(item.get("gain_100"), 0)}
                    if item.get("gain_100") is not None
                    else {}
                ),
                **(
                    {"gain_1005": _i(item.get("gain_1005"), 0)}
                    if item.get("gain_1005") is not None
                    else {}
                ),
            }
            for item in push_rows
            if isinstance(item, dict)
        ],
    }
    return json.dumps(cleaned, ensure_ascii=False)


def _analysis_length_instruction(max_tokens: int) -> str:
    if max_tokens <= 1024:
        return "控制在 350-500 个中文字，避免 JSON 在推荐列表中途被截断"
    if max_tokens <= 2048:
        return "控制在 600-850 个中文字，给推荐列表预留完整 JSON 空间"
    return "建议 800-1000 个中文字，同时确保整个 JSON 完整结束"


def _fmt(context: dict) -> str:
    player = context.get("player") or {}
    summary = context.get("summary") or {}
    peer = context.get("peer_stats") or {}
    pack = context.get("b50_evidence_pack") or {}

    rating_val = player.get("rating")
    fine_seg = _fine_rating_segment(rating_val)

    lines = [
        f"玩家：{player.get('nickname')}  Rating：{rating_val}",
        f"分段判断：{fine_seg.get('label')}  {fine_seg.get('tone')}",
        f"B35 RA：{summary.get('b35_ra')}  B15 RA：{summary.get('b15_ra')}",
        f"全B50平均达成：{summary.get('avg_achievement')}%  平均定数：{summary.get('avg_ds')}",
        f"B35均值：{(summary.get('b35') or {}).get('avg_achievement')}%  B15均值：{(summary.get('b15') or {}).get('avg_achievement')}%",
    ]

    arpi = peer.get("arpi")
    overlap = (peer.get("b50_overlap") or {}).get("value")
    if arpi is not None:
        lines.append(f"ARPI：{arpi:+.4f}  B50重合度：{overlap:.2f}%")

    # ARPI bucket stats
    arpi_bucket = context.get("arpi_bucket_stats") or {}
    if arpi_bucket.get("sufficient"):
        pos = arpi_bucket.get("position", "")
        pos_label = {
            "above_p75": "同段上四分位/稳手",
            "around_median": "典型画风",
            "below_p25": "下四分位/靠选谱拉分",
        }.get(pos, pos)
        lines.append(
            f"ARPI同段位置：{pos_label}  均值：{arpi_bucket.get('mean')}  中位：{arpi_bucket.get('median')}"
        )
    elif arpi_bucket:
        lines.append("ARPI同段：样本不足，先不硬下判断")

    # B50 overlap interpretation
    b50_overlap = context.get("b50_overlap") or {}
    if isinstance(b50_overlap, dict) and b50_overlap.get("value") is not None:
        ov = float(b50_overlap.get("value") or 0)
        if ov < 30:
            ov_desc = "选曲小众/口味独到/谱面含金量高（正面）"
        elif ov <= 50:
            ov_desc = "正常区间"
        else:
            ov_desc = "偏模板/跟风攻略"
        lines.append(f"B50重合度：{ov:.2f}%  解读：{ov_desc}")

    peer_comp = pack.get("peer_comparison") or {}
    if peer_comp.get("matched") is not None:
        lines.append(
            f"同段匹配：{peer_comp.get('matched')}  同段桶：{peer_comp.get('rating_bucket')}"
        )
    if peer_comp.get("available") is False:
        lines.append("同段统计：不可用时不要硬写 ARPI/gap")

    rating_split = pack.get("rating_split") or {}
    fine_segment = rating_split.get("fine_segment") or {}
    if fine_segment:
        lines.append(
            f"分段判断（pack）：{fine_segment.get('label')}  {fine_segment.get('tone')}"
        )

    def _fmt_tags(tags: list) -> str:
        items = [str(t).strip() for t in (tags or []) if str(t).strip()]
        return "/".join(items[:4])

    def _chart_line(c: dict) -> str:
        gap = c.get("gap_vs_peer")
        peer_avg = c.get("peer_avg")
        tags = _fmt_tags(c.get("config_tags") or c.get("config") or [])
        parts = [f"[{c.get('bucket', '')} {c.get('ds', '')}] {c.get('title', '')}"]
        parts.append(f"{c.get('achievement', 0):.4f}%")
        parts.append(f"RA {c.get('song_rating', 0)}")
        if peer_avg is not None:
            parts.append(f"同段均值 {peer_avg:.4f}%")
        if gap is not None:
            parts.append(f"同段差距 {gap:+.4f}")
        if tags:
            parts.append(f"配置 {tags}")
        return "  ".join(parts)

    # Config profile (strong/weak)
    config_profile = context.get("config_profile") or {}
    if config_profile.get("strong") or config_profile.get("weak"):
        lines.append("")
        lines.append("配置画像：")
        for item in (config_profile.get("strong") or [])[:3]:
            kw = item.get("kw") or item.get("tag") or ""
            cnt = item.get("count", 0)
            avg = item.get("avg_ach") or item.get("avg_achievement") or 0
            lines.append(f"  擅长 {kw}：{cnt} 张，均值 {avg}%")
        for item in (config_profile.get("weak") or [])[:2]:
            kw = item.get("kw") or item.get("tag") or ""
            cnt = item.get("count", 0)
            avg = item.get("avg_ach") or item.get("avg_achievement") or 0
            lines.append(f"  短板 {kw}：{cnt} 张，均值 {avg}%")

    config_focus = pack.get("config_focus") or {}
    if config_focus.get("strong") or config_focus.get("weak"):
        lines.append("")
        lines.append("配置切入：")
        for item in (config_focus.get("strong") or [])[:3]:
            lines.append(
                f"  擅长 {item.get('tag')}：{item.get('count')} 张，均值 {item.get('avg_achievement')}%，同段差距 {item.get('avg_gap_vs_peer')}"
            )
        for item in (config_focus.get("weak") or [])[:2]:
            lines.append(
                f"  相对较弱 {item.get('tag')}：{item.get('count')} 张，均值 {item.get('avg_achievement')}%，同段差距 {item.get('avg_gap_vs_peer')}"
            )

    b35b15 = pack.get("b35_b15_structure") or {}
    if b35b15:
        lines.append("")
        lines.append("B35/B15：")
        for key in ("b35", "b15"):
            sec = b35b15.get(key) or {}
            if sec:
                lines.append(
                    f"  {key.upper()}：{sec.get('count')} 张，均值 {sec.get('avg_achievement')}%，RA {sec.get('avg_song_rating')}，同段差距 {sec.get('avg_gap_vs_peer')}"
                )

    picked = []
    for key in (
        "same_rating_average_entry_points",
        "selected_evidence",
        "strongest_vs_peer",
        "highest_song_rating",
    ):
        for c in (pack.get(key) or [])[:3]:
            if c not in picked:
                picked.append(c)
            if len(picked) >= 6:
                break
        if len(picked) >= 6:
            break

    if picked:
        lines.append("")
        lines.append("关键谱：")
        lines.extend(_chart_line(c) for c in picked)

    for label, key in (
        ("重点分析曲目", "roast_targets"),
        ("同分入口", "same_rating_average_entry_points"),
        ("强证据", "strongest_vs_peer"),
        ("弱证据", "weakest_vs_peer"),
    ):
        rows = pack.get(key) or []
        if rows:
            lines.append("")
            lines.append(f"{label}：")
            for c in rows[:4]:
                pieces = [str(c.get("title") or "")]
                if c.get("ds") is not None:
                    pieces.append(f"定数 {c.get('ds')}")
                if c.get("achievement") is not None:
                    pieces.append(f"达成率 {c.get('achievement'):.4f}%")
                if c.get("song_rating") is not None:
                    pieces.append(f"RA {c.get('song_rating')}")
                if c.get("peer_avg") is not None:
                    pieces.append(f"同段均值 {c.get('peer_avg'):.4f}%")
                if c.get("gap_vs_peer") is not None:
                    pieces.append(f"同段差距 {c.get('gap_vs_peer'):+.4f}")
                tag_text = _fmt_tags(c.get("config_tags") or c.get("config") or [])
                if tag_text:
                    pieces.append(f"配置 {tag_text}")
                lines.append("  " + "  ".join(pieces))

    for label, key in (
        ("理论值/高光", "theory_cards"),
        ("15理论", "impossible_15_theory"),
        ("14+AP", "level_14_plus_ap"),
        ("高定数AP", "high_ds_ap"),
        ("异常同段差距", "abnormal_peer_gaps"),
    ):
        rows = pack.get(key) or []
        if rows:
            lines.append("")
            lines.append(f"{label}：")
            for c in rows[:4]:
                pieces = [str(c.get("title") or "")]
                if c.get("ds") is not None:
                    pieces.append(f"定数 {c.get('ds')}")
                if c.get("achievement") is not None:
                    pieces.append(f"达成率 {c.get('achievement'):.4f}%")
                if c.get("song_rating") is not None:
                    pieces.append(f"RA {c.get('song_rating')}")
                if c.get("peer_avg") is not None:
                    pieces.append(f"同段均值 {c.get('peer_avg'):.4f}%")
                if c.get("gap_vs_peer") is not None:
                    pieces.append(f"同段差距 {c.get('gap_vs_peer'):+.4f}")
                lines.append("  " + "  ".join(pieces))

    ds_summary = pack.get("ds_band_summary") or {}
    if ds_summary:
        lines.append("")
        lines.append("定数段：")
        for band in ("<13", "13", "13+", "14", "14+"):
            item = ds_summary.get(band)
            if item:
                lines.append(
                    f"  {band}：均值 {item.get('avg_achievement')}% / 同段差距 {item.get('avg_gap_vs_peer')} / RA {item.get('avg_song_rating')}"
                )

    evidence = pack.get("selected_evidence") or []
    if evidence:
        lines.append("")
        lines.append("核心证据：")
        for c in evidence[:6]:
            pieces = [f"{c.get('title', '')}"]
            if c.get("ds") is not None:
                pieces.append(f"定数 {c.get('ds')}")
            if c.get("achievement") is not None:
                pieces.append(f"达成率 {c.get('achievement'):.4f}%")
            if c.get("song_rating") is not None:
                pieces.append(f"RA {c.get('song_rating')}")
            if c.get("peer_avg") is not None:
                pieces.append(f"同段均值 {c.get('peer_avg'):.4f}%")
            if c.get("gap_vs_peer") is not None:
                pieces.append(f"同段差距 {c.get('gap_vs_peer'):+.4f}")
            tag_text = _fmt_tags(c.get("config_tags") or c.get("config") or [])
            if tag_text:
                pieces.append(f"配置 {tag_text}")
            lines.append("  " + "  ".join(pieces))

    # Push candidates (供给 LLM 选曲)
    push_candidates = context.get("push_candidates") or []
    if push_candidates:
        lines.append("")
        lines.append("推分候选池（仅含鸟寸/鸟加寸，从中选 3-4 首）：")
        for i, c in enumerate(push_candidates[:15], 1):
            tag_text = _fmt_tags(c.get("config_tags") or [])
            extra = []
            if c.get("bucket"):
                extra.append(str(c.get("bucket")))
            if c.get("peer_avg") is not None:
                extra.append(f"同段均值{c.get('peer_avg'):.4f}%")
            if c.get("gap_vs_peer") is not None:
                extra.append(f"同段差距{c.get('gap_vs_peer'):+.4f}")
            if tag_text:
                extra.append(f"配置{tag_text}")
            target = str(c.get("target") or "")
            target_gain = (
                c.get("gain_100", 0) if target == "SSS" else c.get("gain_1005", 0)
            )
            lines.append(
                f"  {i}. {c.get('title', '')}  定数{c.get('ds', '')}  达成率{c.get('achievement', c.get('achievements', ''))}%  目标{target}  预计+{target_gain} rating  {c.get('level_label', '')}"
                + (f"  {'  '.join(extra)}" if extra else "")
            )

    # Chart summaries (community_vibe / chart_identity)
    chart_summaries = context.get("chart_summaries") or {}
    if chart_summaries:
        lines.append("")
        lines.append(
            "谱面资料（community_vibe/chart_identity，引用时注明来自所提供资料）："
        )
        for title, s in list(chart_summaries.items())[:6]:
            if not isinstance(s, dict):
                continue
            vibe = s.get("community_vibe") or s.get("chart_identity") or ""
            tags = _fmt_tags(s.get("config_tags") or [])
            if vibe or tags:
                lines.append(f"  {title}：{vibe}  配置 {tags}")

    return "\n".join(lines)


def _usage_token_count(usage: Any, *names: str) -> int:
    if usage is None:
        return 0
    for name in names:
        if isinstance(usage, dict):
            value = usage.get(name)
        else:
            value = getattr(usage, name, None)
            if value is None:
                extra = getattr(usage, "model_extra", None)
                value = extra.get(name) if isinstance(extra, dict) else None
        try:
            if value is not None:
                return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _cached_prompt_tokens(usage: Any) -> int:
    direct = _usage_token_count(usage, "prompt_cache_hit_tokens", "cached_tokens")
    if direct:
        return direct
    if isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    else:
        details = getattr(usage, "prompt_tokens_details", None)
        if details is None:
            extra = getattr(usage, "model_extra", None)
            details = (
                extra.get("prompt_tokens_details") if isinstance(extra, dict) else None
            )
    return _usage_token_count(details, "cached_tokens")


def _snowflakes_from_usage(usage: Any, config: Config) -> float:
    """Convert RMB token cost to snowflakes, where RMB 0.01 equals one."""
    if usage is None:
        return 0.0
    prompt_tokens = _usage_token_count(usage, "prompt_tokens", "input_tokens")
    billable_prompt_tokens = max(0, prompt_tokens - _cached_prompt_tokens(usage))
    completion_tokens = _usage_token_count(
        usage, "completion_tokens", "output_tokens"
    )
    try:
        input_price = Decimal(str(config.b50_llm_input_price_per_million))
        output_price = Decimal(str(config.b50_llm_output_price_per_million))
        cost = (
            Decimal(billable_prompt_tokens) * input_price
            + Decimal(completion_tokens) * output_price
        ) / Decimal(1_000_000)
        snowflakes = (cost / Decimal("0.01")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, TypeError, ValueError):
        return 0.0
    return float(max(Decimal(0), snowflakes))


async def generate_analysis(
    context: dict, config: Config, style: str = ""
) -> tuple[str, float]:
    style_instruction = f"\n- 请用以下风格/语气/需求进行锐评：{style}" if style else ""
    system = _SYSTEM.format(
        style_instruction=style_instruction,
        length_instruction=_analysis_length_instruction(config.b50_llm_max_tokens),
    )

    client = AsyncOpenAI(
        api_key=config.b50_llm_key,
        base_url=config.b50_llm_url.rstrip("/"),
    )
    resp = await client.chat.completions.create(
        model=config.b50_llm_model,
        messages=[
            {"role": "user", "content": f"{system}\n\n{_fmt(context)}"},
        ],
        temperature=0.8,
        max_tokens=config.b50_llm_max_tokens,
    )
    content = (resp.choices[0].message.content or "").strip()
    snowflakes = _snowflakes_from_usage(getattr(resp, "usage", None), config)

    try:
        cleaned_content = _cleanup_response(content)
        try:
            cleaned = json.loads(cleaned_content)
        except json.JSONDecodeError:
            cleaned = {
                "title": "B50锐评",
                "overall_roast": cleaned_content,
                "impression_roast": "",
                "push_recommendations": [],
            }
    except Exception:  # noqa: BLE001 - malformed provider output must use fallback
        cleaned = {
            "title": "B50锐评",
            "overall_roast": "模型输出格式异常，本次未能生成可读锐评，请重新尝试。",
            "impression_roast": "",
            "push_recommendations": [],
        }
    fallback_push = _select_push_recommendations(
        context.get("push_candidates") or [],
        context.get("config_focus") or {},
        style,
        4,
    )
    cleaned["push_recommendations"] = _merge_push_recommendations(
        cleaned.get("push_recommendations") or [],
        fallback_push,
    )
    return json.dumps(cleaned, ensure_ascii=False), snowflakes
