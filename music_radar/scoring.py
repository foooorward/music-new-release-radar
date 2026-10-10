"""聚合去重 + 综合热度分模型。

背景（务必看这里，影响你怎么理解分数）：
国内音乐平台都不对外公开真实播放量。网易云、酷狗的接口里，播放量/热度字段
（如 pop）实测返回空值，页面上的"热度"是平台内部算法值。真正公开播放量的
只有 YouTube（viewCount）和 Last.fm（playcount），本模块预留了 external 接口位。

所以综合热度分由四个可得的维度加权而成：

    rank     榜单名次   —— 代表平台自己的热度结论，权重最高
    cross    跨平台覆盖 —— 同一首歌出现在几个平台，反映发行力度与铺量
    artist   音乐人热度 —— 歌手本次榜单曝光度 + 作品体量（代理指标）
    momentum 榜单爬升   —— 相对上一次快照的名次变化，只有连续采集才有

任一维度缺失时会自动把它的权重按比例分摊给其它维度，不会因为某个平台
没采到数据就把所有歌的分数拉低。
"""

from __future__ import annotations

import datetime as _dt
import math
import re

_PUNCT = re.compile(r"[\s\-_·、,，.。!！?？'\"“”‘’()（）\[\]【】/\\|~～&+]+")


def normalize_text(text: str) -> str:
    """用于跨平台比对的轻量归一化：去标点、去空格、统一小写。"""
    return _PUNCT.sub("", (text or "").strip().lower())


def track_key(title: str, artists: list[str]) -> str:
    """一首歌的唯一键：主歌手 + 歌名。"""
    primary = normalize_text(artists[0]) if artists else ""
    return "%s::%s" % (primary, normalize_text(title))


def _rank_point(rank: int, size: int) -> float:
    """名次换算成 0~1 的得分，第 1 名 = 1。"""
    if size <= 1:
        return 1.0
    return max(0.0, min(1.0, 1.0 - (rank - 1) / (size - 1)))


def aggregate(records: list[dict]) -> dict[str, dict]:
    """把同一首歌在多平台多榜单的上榜记录合并成一条。"""
    entries: dict[str, dict] = {}
    for record in records:
        title = (record.get("title") or "").strip()
        if not title:
            continue
        artists = record.get("artists") or []
        key = track_key(title, artists)
        entry = entries.get(key)
        if entry is None:
            entry = {
                "key": key,
                "title": title,
                "artists": artists,
                "album": record.get("album") or "",
                "publish_time": record.get("publish_time"),
                "duration_ms": record.get("duration_ms"),
                "artist_ids": record.get("artist_ids") or {},
                "charts": [],
                "platforms": [],
                "urls": {},
            }
            entries[key] = entry

        # 补齐缺失字段：发行时间取最早的那个（其它平台的真实发行日期
        # 通常早于酷狗的"进榜时间"，取最早能还原真实发行日期）
        incoming = record.get("publish_time")
        if incoming:
            current = entry.get("publish_time")
            if not current or incoming < current:
                entry["publish_time"] = incoming
        if not entry.get("album") and record.get("album"):
            entry["album"] = record["album"]
        if not entry.get("duration_ms") and record.get("duration_ms"):
            entry["duration_ms"] = record["duration_ms"]

        platform = record.get("platform") or ""
        if platform and platform not in entry["platforms"]:
            entry["platforms"].append(platform)
        if platform and record.get("url"):
            entry["urls"].setdefault(platform, record["url"])

        entry["charts"].append(
            {
                "platform": platform,
                "platform_label": record.get("platform_label") or platform,
                "chart": record.get("chart") or "",
                "chart_kind": record.get("chart_kind") or "new",
                "chart_weight": float(record.get("chart_weight") or 0.0),
                "chart_size": int(record.get("chart_size") or 0),
                "rank": int(record.get("rank") or 0),
                "note": record.get("note") or "",
            }
        )

    for entry in entries.values():
        entry["charts"].sort(key=lambda c: (c["rank"] or 9999, -c["chart_weight"]))
    return entries


def _rank_score(entry: dict) -> float | None:
    """加权平均的榜单名次分；只有真实榜单参与，按发行时间排的清单不参与。"""
    total_weight = 0.0
    acc = 0.0
    for chart in entry["charts"]:
        if chart["chart_kind"] == "release" or chart["chart_weight"] <= 0:
            continue
        if chart["rank"] <= 0:
            continue
        acc += chart["chart_weight"] * _rank_point(chart["rank"], chart["chart_size"])
        total_weight += chart["chart_weight"]
    if total_weight <= 0:
        return None
    return acc / total_weight


def build_artist_table(
    entries: dict[str, dict],
    artist_info: dict[int, dict],
    config: dict,
) -> dict[str, dict]:
    """统计每位歌手本次的榜单曝光度，并结合作品体量给出音乐人热度分。"""
    weights = ((config.get("scoring") or {}).get("weights") or {})
    scale = float((config.get("scoring") or {}).get("artist_size_scale", 800))
    table: dict[str, dict] = {}

    for entry in entries.values():
        rank_point = _rank_score(entry)
        for artist in entry["artists"]:
            name_key = normalize_text(artist)
            if not name_key:
                continue
            row = table.setdefault(
                name_key,
                {
                    "name": artist,
                    "songs": 0,
                    "best_rank_point": 0.0,
                    "music_size": None,
                    "album_size": None,
                    "platforms": set(),
                },
            )
            row["songs"] += 1
            if rank_point is not None:
                row["best_rank_point"] = max(row["best_rank_point"], rank_point)
            for platform in entry["platforms"]:
                row["platforms"].add(platform)
            for ids in (entry.get("artist_ids") or {}).values():
                for artist_id in ids:
                    info = artist_info.get(int(artist_id))
                    if info and info.get("music_size"):
                        row["music_size"] = max(row["music_size"] or 0, info["music_size"])
                        row["album_size"] = max(row["album_size"] or 0, info.get("album_size") or 0)

    for key, row in table.items():
        # 曝光度：本次上榜单曲数量 + 最好的名次
        exposure = 0.6 * min(row["songs"] / 4.0, 1.0) + 0.4 * row["best_rank_point"]
        # 体量：作品数做对数归一；拿不到就给中性值 0.5，不冤枉也不抬高
        if row["music_size"]:
            size_score = min(1.0, math.log10(row["music_size"] + 1) / math.log10(scale + 1))
        else:
            size_score = 0.5
        row["exposure"] = exposure
        row["size_score"] = size_score
        row["hot"] = 0.65 * exposure + 0.35 * size_score
        row["platform_count"] = len(row["platforms"])
        row["platforms"] = sorted(row["platforms"])
        row.pop("best_rank_point", None)
        _ = weights  # 位保留：未来给不同平台/维度单独加权用

    return table


def score_entries(
    entries: dict[str, dict],
    artist_table: dict[str, dict],
    previous_best_rank: dict[str, int],
    config: dict,
    today: str,
) -> list[dict]:
    """给每首歌算综合热度分，返回按分数降序的列表。"""
    scoring_cfg = config.get("scoring") or {}
    weights = {
        "rank": 0.45,
        "cross": 0.25,
        "artist": 0.15,
        "momentum": 0.15,
    }
    weights.update(scoring_cfg.get("weights") or {})
    new_release_days = int(scoring_cfg.get("new_release_days", 14))

    active_platforms: set[str] = set()
    for entry in entries.values():
        active_platforms.update(entry["platforms"])
    platform_total = len(active_platforms)

    today_date = _dt.date.fromisoformat(today)
    scored: list[dict] = []

    for entry in entries.values():
        rank_score = _rank_score(entry)

        # 跨平台覆盖：本次只有单平台可用时给中性值，避免全体同分
        if platform_total <= 1:
            cross_score = 0.5
        else:
            cross_score = (len(entry["platforms"]) - 1) / (platform_total - 1)

        # 音乐人热度：取主歌手，取不到则回退到所有歌手的最大值
        artist_score = 0.5
        primary = normalize_text(entry["artists"][0]) if entry["artists"] else ""
        if primary and primary in artist_table:
            artist_score = artist_table[primary]["hot"]
        elif entry["artists"]:
            candidates = [
                artist_table[normalize_text(a)]["hot"]
                for a in entry["artists"]
                if normalize_text(a) in artist_table
            ]
            if candidates:
                artist_score = max(candidates)

        # 榜单爬升：拿上一次快照的该曲最佳名次做对比
        ranked_charts = [c for c in entry["charts"] if c["chart_kind"] != "release" and c["rank"] > 0]
        best_rank = min((c["rank"] for c in ranked_charts), default=0)
        best_chart = min(ranked_charts, key=lambda c: c["rank"]) if ranked_charts else None
        prev_rank = previous_best_rank.get(entry["key"])
        rank_delta = None
        if prev_rank and best_rank:
            rank_delta = prev_rank - best_rank
            improvement = max(-1.0, min(1.0, rank_delta / float(prev_rank)))
            momentum_score = 0.5 + 0.5 * improvement
        else:
            momentum_score = 0.5  # 首次出现：中性，等有历史再拉开差距

        components = [("cross", cross_score), ("artist", artist_score), ("momentum", momentum_score)]
        if rank_score is not None:
            components.append(("rank", rank_score))
        weight_sum = sum(weights[name] for name, _ in components)
        raw = sum(weights[name] * value for name, value in components) / weight_sum

        published_on = entry.get("publish_time")
        is_new_release = False
        if published_on:
            try:
                is_new_release = (today_date - _dt.date.fromisoformat(published_on)).days <= new_release_days
            except ValueError:
                is_new_release = False

        primary_artist = entry["artists"][0] if entry["artists"] else ""
        artist_row = artist_table.get(normalize_text(primary_artist)) or {}
        # 被任一平台的新歌榜收录，这是"它是新歌"的一个可靠旁证——
        # 尤其对酷狗这类不提供发行日期的平台，这个标记能补回发布时间缺失的信息
        in_new_chart = any(chart["chart_kind"] == "new" for chart in entry["charts"])

        scored.append(
            {
                "key": entry["key"],
                "title": entry["title"],
                "artists": entry["artists"],
                "artist": primary_artist,
                "album": entry["album"],
                "publish_time": published_on,
                "is_new_release": is_new_release,
                "in_new_chart": in_new_chart,
                "platforms": [PLATFORM_NAME.get(p, p) for p in entry["platforms"]],
                "platform_keys": entry["platforms"],
                "platform_count": len(entry["platforms"]),
                "charts": entry["charts"],
                "best_rank": best_rank,
                "best_chart": (best_chart["chart"] if best_chart else ""),
                "best_platform": (best_chart["platform_label"] if best_chart else ""),
                "prev_rank": prev_rank,
                "rank_delta": rank_delta,
                "urls": entry["urls"],
                "duration_ms": entry.get("duration_ms"),
                "artist_hot": round(artist_score * 100, 1),
                "artist_music_size": artist_row.get("music_size"),
                "parts": {
                    "rank": None if rank_score is None else round(rank_score * 100, 1),
                    "cross": round(cross_score * 100, 1),
                    "artist": round(artist_score * 100, 1),
                    "momentum": round(momentum_score * 100, 1),
                },
                "score": round(raw * 100, 1),
            }
        )

    scored.sort(key=lambda item: (-item["score"], item["best_rank"] or 9999, item["title"]))
    for index, item in enumerate(scored, 1):
        item["rank"] = index
    return scored


PLATFORM_NAME = {
    "netease": "网易云音乐",
    "kugou": "酷狗音乐",
    "itunes": "Apple Music",
}
