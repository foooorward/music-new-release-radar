"""各平台采集器。

统一产出结构（一条 = 某首歌在某平台某个榜单里的一次上榜记录）：

    {
      "platform":      "netease" | "kugou" | "itunes",
      "platform_label":"网易云音乐",
      "chart":         "新歌榜",
      "chart_kind":    "new" | "hot" | "rising" | "release",
      "chart_weight":  1.0,
      "chart_size":    100,
      "rank":          1,
      "title":         "自由的你",
      "artists":       ["G.E.M.邓紫棋"],
      "album":         "自由的你",
      "publish_time":  "2026-10-05" | None,
      "duration_ms":   296900 | None,
      "url":           "https://...",
      "artist_ids":    {"netease": [6452]} | {},
      "note":          ""
    }

chart_kind 说明：
  new / hot / rising —— 平台真实榜单，名次代表平台自身的热度结论，会参与热度分计算
  release            —— 按发行时间整理的清单，名次只是排序号，不参与热度分计算
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Callable

from .http_client import FetchError, fetch_json

Log = Callable[[str], None]

PLATFORM_LABELS = {
    "netease": "网易云音乐",
    "kugou": "酷狗音乐",
    "itunes": "Apple Music / iTunes",
}


def _iso_date(value: Any) -> str | None:
    """把各种时间表示统一成 YYYY-MM-DD。"""
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        # 毫秒时间戳会被误当秒，用大小区分（> 1e11 视为毫秒）
        if number > 1e11:
            number /= 1000.0
        try:
            return _dt.datetime.fromtimestamp(number).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _iso_date(int(text))
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(text[: len(fmt) + 2], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text[:10] if len(text) >= 10 else None


def _first(data: dict, keys: list[str], default: Any = None) -> Any:
    """按候选键名依次取值，用于兼容不同版本接口的字段名。"""
    for key in keys:
        if key in data and data[key] not in (None, "", 0, "0", [], {}):
            return data[key]
    return default


def _track(**kwargs: Any) -> dict:
    base = {
        "platform": "",
        "platform_label": "",
        "chart": "",
        "chart_kind": "new",
        "chart_weight": 1.0,
        "chart_size": 0,
        "rank": 0,
        "title": "",
        "artists": [],
        "album": "",
        "publish_time": None,
        "duration_ms": None,
        "url": "",
        "artist_ids": {},
        "note": "",
    }
    base.update(kwargs)
    base["artists"] = [a for a in (base["artists"] or []) if a]
    if not base["platform_label"]:
        base["platform_label"] = PLATFORM_LABELS.get(base["platform"], base["platform"])
    return base


# ---------------------------------------------------------------- 网易云音乐

NETEASE_CHART_API = "https://music.163.com/api/playlist/detail?id={chart_id}"
NETEASE_ARTIST_API = "https://music.163.com/api/artist/{artist_id}"
NETEASE_HEADERS = {"Referer": "https://music.163.com/"}


def collect_netease(cfg: dict, log: Log) -> list[dict]:
    delay = float(cfg.get("delay", 1.2))
    tracks: list[dict] = []
    for chart in cfg.get("charts") or []:
        payload = fetch_json(
            NETEASE_CHART_API.format(chart_id=chart["id"]),
            headers=NETEASE_HEADERS,
            delay=delay,
        )
        result = (payload or {}).get("result") or {}
        songs = result.get("tracks") or []
        if not songs:
            raise FetchError("榜单 %s 未返回歌曲（接口可能已变更）" % chart.get("name"))
        chart_name = chart.get("name") or result.get("name") or "网易云榜单"
        size = len(songs)
        log("  [网易云] %-6s 采集 %d 首" % (chart_name, size))
        for index, song in enumerate(songs, 1):
            artists = [a.get("name") for a in (song.get("artists") or []) if a.get("name")]
            artist_ids = [a.get("id") for a in (song.get("artists") or []) if a.get("id")]
            album = song.get("album") or {}
            # 实测约三成歌曲的 song.publishTime 为空，但 album.publishTime 都有，
            # 两者互补后发布时间覆盖率可以做到 100%
            publish_time = _iso_date(song.get("publishTime") or album.get("publishTime"))
            tracks.append(
                _track(
                    platform="netease",
                    chart=chart_name,
                    chart_kind=chart.get("kind", "new"),
                    chart_weight=float(chart.get("weight", 1.0)),
                    chart_size=size,
                    rank=index,
                    title=(song.get("name") or "").strip(),
                    artists=artists,
                    album=(album.get("name") or "").strip(),
                    publish_time=publish_time,
                    duration_ms=song.get("duration"),
                    url="https://music.163.com/#/song?id=%s" % song.get("id"),
                    artist_ids={"netease": artist_ids} if artist_ids else {},
                )
            )
    return tracks


def enrich_netease_artists(ids: list[int], cfg: dict, log: Log) -> dict[int, dict]:
    """补充歌手体量信息（作品数 / 专辑数）作为"音乐人热度"的代理指标。

    网易云不对外提供粉丝数，这里用作品体量与榜单曝光度一起近似热度。
    """
    delay = float(cfg.get("delay", 1.2))
    info: dict[int, dict] = {}
    for artist_id in ids:
        try:
            payload = fetch_json(
                NETEASE_ARTIST_API.format(artist_id=artist_id),
                headers=NETEASE_HEADERS,
                delay=delay,
            )
            artist = (payload or {}).get("artist") or {}
            if not artist:
                continue
            info[int(artist_id)] = {
                "name": artist.get("name") or "",
                "music_size": artist.get("musicSize") or 0,
                "album_size": artist.get("albumSize") or 0,
            }
        except Exception as error:  # noqa: BLE001 - 单个歌手失败不影响整体
            log("  [网易云] 歌手 %s 体量补充失败：%s" % (artist_id, error))
    return info


# -------------------------------------------------------------------- 酷狗音乐

KUGOU_RANK_API = (
    "https://mobilecdnbj.kugou.com/api/v3/rank/song"
    "?version=9108&plat=0&pagesize={size}&rankid={rank_id}&page=1"
)
KUGOU_HEADERS = {"Referer": "https://www.kugou.com/"}


def collect_kugou(cfg: dict, log: Log) -> list[dict]:
    delay = float(cfg.get("delay", 1.5))
    page_size = int(cfg.get("page_size", 100))
    insecure = bool(cfg.get("insecure_ssl", False))
    tracks: list[dict] = []
    for chart in cfg.get("charts") or []:
        payload = fetch_json(
            KUGOU_RANK_API.format(size=page_size, rank_id=chart["id"]),
            headers=KUGOU_HEADERS,
            delay=delay,
            insecure=insecure,
        )
        if not isinstance(payload, dict):
            raise FetchError("酷狗返回结构异常")
        if payload.get("error_code"):
            raise FetchError(
                "酷狗接口返回错误 %s %s" % (payload.get("error_code"), payload.get("error_msg"))
            )
        data = payload.get("data") or {}
        songs = data.get("info") or data.get("songs") or data.get("lists") or []
        if not songs:
            raise FetchError("榜单 %s 未返回歌曲" % chart.get("name"))
        size = int(data.get("total") or len(songs)) or len(songs)
        log("  [酷狗] %-10s 采集 %d 首" % (chart.get("name"), len(songs)))
        for index, song in enumerate(songs, 1):
            title = (_first(song, ["songname", "song_name", "ori_audio_name", "filename"]) or "").strip()
            # 酷狗把歌手放在 authors 数组里，老版本接口才是 singername 字段
            authors = song.get("authors") or []
            names = [a.get("author_name") for a in authors if isinstance(a, dict) and a.get("author_name")]
            singer = "、".join(names) or (_first(song, ["singername", "singer_name"]) or "").strip()
            if not title:
                continue
            # filename 里是「歌手 - 歌名」，歌手缺失时从里面拆
            if singer and " - " in title and title.startswith(singer):
                title = title.split(" - ", 1)[1].strip()
            duration = _first(song, ["duration", "timelength"])
            duration_ms = None
            if isinstance(duration, (int, float)) and duration:
                duration_ms = int(duration * 1000) if duration < 100000 else int(duration)
            song_hash = _first(song, ["hash", "audio_hash"], "")
            # 注意：酷狗的 rank_id_publish_date 是「整个榜单的发布时间」，
            # 同一榜单里上百首歌的时间戳完全相同，不能当作歌曲发行日期使用。
            # 因此这里发布时代留空（诚实留白），该曲的发行日期靠其它平台补，
            # 或通过"入选该平台新歌榜"这一事实来体现它是新歌。
            tracks.append(
                _track(
                    platform="kugou",
                    chart=chart.get("name") or "酷狗榜单",
                    chart_kind=chart.get("kind", "new"),
                    chart_weight=float(chart.get("weight", 1.0)),
                    chart_size=int(_first(song, ["rank_count"], 0) or 0) or size,
                    rank=index,
                    title=title,
                    artists=[a for a in names] if names else ([singer] if singer else []),
                    album="",
                    publish_time=None,
                    duration_ms=duration_ms,
                    url=(
                        "https://www.kugou.com/song/#hash=%s" % song_hash
                        if song_hash
                        else "https://www.kugou.com/yy/html/search.html#searchType=song&searchKeyWord=%s" % title
                    ),
                    note="酷狗接口不提供歌曲发行日期",
                )
            )
    return tracks


# ------------------------------------------------------- Apple Music / iTunes

ITUNES_SEARCH_API = (
    "https://itunes.apple.com/search"
    "?term={term}&media=music&entity=song&country={country}&limit={limit}"
)


def collect_itunes(cfg: dict, log: Log) -> list[dict]:
    """Apple 官方搜索接口。

    实测：中国大陆区域店铺（cn）的 song / album 搜索返回 0 条结果，
    中国香港（hk）、中国台湾（tw）区域店铺正常。因此默认同时查询三个区域，
    并对连续无结果的区域提前跳过，避免白白浪费请求。
    """
    delay = float(cfg.get("delay", 0.8))
    countries = cfg.get("countries") or [cfg.get("country") or "cn"]
    limit = int(cfg.get("limit", 200))
    recent_days = int(cfg.get("recent_days", 10))
    skip_after_empty = int(cfg.get("skip_after_empty", 3))
    keywords = cfg.get("keywords") or []

    reference = cfg.get("_target_date") or _dt.date.today().isoformat()
    cutoff = _dt.date.fromisoformat(reference) - _dt.timedelta(days=recent_days)

    seen: dict[str, dict] = {}
    for country in countries:
        country_kept = 0
        empty_streak = 0
        for keyword in keywords:
            payload = fetch_json(
                ITUNES_SEARCH_API.format(
                    term=_url_quote(keyword), country=country, limit=limit
                ),
                delay=delay,
            )
            results = (payload or {}).get("results") or []
            if not results:
                empty_streak += 1
                if empty_streak >= skip_after_empty:
                    log(
                        "  [Apple] %s 区连续 %d 个关键词无结果，跳过该区剩余关键词"
                        % (country, empty_streak)
                    )
                    break
                continue
            empty_streak = 0

            for item in results:
                release = _iso_date(item.get("releaseDate"))
                if not release:
                    continue
                try:
                    released_on = _dt.date.fromisoformat(release)
                except ValueError:
                    continue
                if released_on < cutoff:
                    continue
                track_id = str(item.get("trackId") or "")
                if not track_id or track_id in seen:
                    continue
                seen[track_id] = {
                    "title": (item.get("trackName") or "").strip(),
                    "artists": [item.get("artistName") or ""],
                    "album": (item.get("collectionName") or "").strip(),
                    "publish_time": release,
                    "duration_ms": item.get("trackTimeMillis"),
                    "url": item.get("trackViewUrl") or "",
                    "country": country,
                }
                country_kept += 1
        log("  [Apple] %s 区命中近 %d 天新发行 %d 首" % (country, recent_days, country_kept))

    ordered = sorted(seen.values(), key=lambda x: x["publish_time"] or "", reverse=True)
    total = len(ordered) or 1
    tracks: list[dict] = []
    for index, item in enumerate(ordered, 1):
        tracks.append(
            _track(
                platform="itunes",
                chart="Apple 新发行",
                chart_kind="release",
                chart_weight=0.0,
                chart_size=total,
                rank=index,
                title=item["title"],
                artists=item["artists"],
                album=item["album"],
                publish_time=item["publish_time"],
                duration_ms=item["duration_ms"],
                url=item["url"],
                note="按发行时间倒序，名次仅代表新旧，不参与热度分",
            )
        )
    return tracks


def _url_quote(text: str) -> str:
    import urllib.parse

    return urllib.parse.quote(text, safe="")


# ---------------------------------------------------------------------- 调度

COLLECTORS: list[tuple[str, Callable[[dict, Log], list[dict]]]] = [
    ("netease", collect_netease),
    ("kugou", collect_kugou),
    ("itunes", collect_itunes),
]


def collect_all(config: dict, log: Log, target_date: str | None = None) -> tuple[list[dict], list[dict]]:
    """依次采集所有启用的数据源。

    返回 (所有上榜记录, 各数据源状态)。单个数据源失败不影响其它数据源，
    失败原因会如实记录在状态里，最终展示在报告和看板上。
    """
    records: list[dict] = []
    status: list[dict] = []
    sources_cfg = config.get("sources") or {}

    for name, collector in COLLECTORS:
        cfg = dict(sources_cfg.get(name) or {})
        if target_date:
            cfg["_target_date"] = target_date
        label = cfg.get("label") or PLATFORM_LABELS.get(name, name)
        if not cfg.get("enabled", True):
            status.append(
                {"source": name, "label": label, "ok": False, "skipped": True,
                 "count": 0, "note": "已在 config.json 中关闭"}
            )
            continue
        try:
            got = collector(cfg, log)
            records.extend(got)
            status.append(
                {"source": name, "label": label, "ok": True, "skipped": False,
                 "count": len(got), "note": ""}
            )
        except Exception as error:  # noqa: BLE001 - 单源失败需继续跑其它源
            status.append(
                {"source": name, "label": label, "ok": False, "skipped": False,
                 "count": 0, "note": str(error)}
            )
            log("  [%s] 采集失败：%s" % (label, error))

    return records, status
