"""每日新歌雷达 —— 入口。

用法：
    python run.py                    # 采集今天的数据并产出报告
    python run.py --date 2026-10-09  # 指定日期（补跑/回填用）
    python run.py --no-enrich        # 跳过多请求的歌手体量补充，快很多
    python run.py --only netease     # 只跑指定数据源，调试用

每次运行会产出：
    output/新歌雷达-YYYY-MM-DD.csv     表格（Excel 可直接打开）
    output/新歌雷达-YYYY-MM-DD.xlsx    装了 openpyxl 才会生成
    output/dashboard.html              自包含网页看板
    site/index.html                    看板的部署副本
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from music_radar import report, scoring, sources, store  # noqa: E402

DEFAULT_CONFIG = BASE_DIR / "config.json"


def load_config(path: Path) -> dict:
    import json

    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def resolve_date(raw: str | None) -> str:
    if not raw:
        return _dt.date.today().isoformat()
    try:
        return _dt.date.fromisoformat(raw).isoformat()
    except ValueError:
        raise SystemExit("日期格式应为 YYYY-MM-DD，收到：%s" % raw) from None


def apply_blacklist(records: list[dict], keywords: list[str]) -> tuple[list[dict], int]:
    if not keywords:
        return records, 0
    kept, dropped = [], 0
    for record in records:
        title = record.get("title") or ""
        if any(keyword and keyword in title for keyword in keywords):
            dropped += 1
            continue
        kept.append(record)
    return kept, dropped


def pick_artist_ids(records: list[dict], top_n: int) -> list[int]:
    counter: Counter[int] = Counter()
    for record in records:
        for ids in (record.get("artist_ids") or {}).values():
            for artist_id in ids:
                counter[int(artist_id)] += 1
    return [artist_id for artist_id, _ in counter.most_common(top_n)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集各平台公开的新歌数据并按综合热度排序")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    parser.add_argument("--date", default=None, help="采集日期，默认今天（YYYY-MM-DD）")
    parser.add_argument("--no-enrich", action="store_true", help="跳过多请求的音乐人体量补充")
    parser.add_argument("--only", default=None, help="只运行指定数据源，逗号分隔，如 netease,itunes")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="对所有数据源关闭 TLS 证书校验（仅当所在网络有代理拦截导致证书主机名不匹配时使用）",
    )
    args = parser.parse_args(argv)

    config = load_config(Path(args.config))
    if args.insecure:
        for cfg in (config.get("sources") or {}).values():
            cfg["insecure_ssl"] = True
        print("警告：已关闭 TLS 证书校验（--insecure），仅建议在网络代理环境排查时使用")
    today = resolve_date(args.date)
    collected_at = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = (config.get("output") or {}).get("dashboard_title", "每日新歌雷达")

    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        for name, cfg in (config.get("sources") or {}).items():
            cfg["enabled"] = name in wanted

    print("=" * 78)
    print("%s  |  采集日期 %s  |  开始时间 %s" % (title, today, collected_at))
    print("=" * 78)

    # 1) 采集
    print("\n[1/6] 采集各平台公开数据")
    records, source_status = sources.collect_all(config, log=print, target_date=today)

    blacklist = (config.get("output") or {}).get("title_blacklist") or []
    records, dropped = apply_blacklist(records, blacklist)
    if dropped:
        print("  已按标题黑名单过滤 %d 条（%s）" % (dropped, "、".join(blacklist)))

    if not records:
        print("\n没有采集到任何数据。请检查网络、或查看上方各数据源失败原因。")
        return 1
    ok_sources = [item for item in source_status if item["ok"]]
    print("  合计 %d 条上榜记录，来自 %d 个可用数据源" % (len(records), len(ok_sources)))

    # 2) 聚合去重
    print("\n[2/6] 跨平台聚合去重")
    entries = scoring.aggregate(records)
    multi = sum(1 for entry in entries.values() if len(entry["platforms"]) > 1)
    print("  去重后 %d 首作品，其中 %d 首跨平台收录" % (len(entries), multi))

    # 3) 音乐人体量补充
    print("\n[3/6] 补充音乐人信息")
    artist_info: dict[int, dict] = {}
    netease_cfg = (config.get("sources") or {}).get("netease") or {}
    if args.no_enrich:
        print("  已跳过（--no-enrich）")
    elif not netease_cfg.get("enrich_artists", True):
        print("  已跳过（config.json 中关闭）")
    else:
        top_n = int(netease_cfg.get("enrich_top_n", 30))
        ids = pick_artist_ids(records, top_n)
        if ids:
            print("  查询榜单曝光最高的 %d 位歌手的作品体量…" % len(ids))
            artist_info = sources.enrich_netease_artists(ids, netease_cfg, log=print)
            print("  成功补充 %d 位" % len(artist_info))
        else:
            print("  没有可补充的歌手 ID")

    # 4) 打分
    print("\n[4/6] 计算综合热度分")
    database = BASE_DIR / "data" / "music_radar.db"
    storage = store.Store(database)
    artist_table = scoring.build_artist_table(entries, artist_info, config)
    previous = storage.previous_best_rank(today)
    last_date = storage.last_snapshot_date(today)
    if last_date:
        print("  对比基准：%s 的快照（%d 首作品）" % (last_date, len(previous)))
    else:
        print("  这是第一次采集，榜单爬升维度使用中性值，明天起才有真实变化")
    scored = scoring.score_entries(entries, artist_table, previous, config, today)

    # top_n <= 0 表示不截断：榜单前列常被经典老歌占据，一旦截断，
    # 排名靠后的新歌会被整体丢掉，看板的"近期新发行"筛选就空了
    top_n = int((config.get("output") or {}).get("top_n", 0))
    if top_n > 0:
        scored = scored[:top_n]
        print("  已按配置截取前 %d 条" % top_n)
    else:
        print("  已保留全部 %d 条（config.json 的 output.top_n 设为 0 即不截断）" % len(scored))

    # 5) 落库
    print("\n[5/6] 写入快照数据库")
    storage.save_snapshot(today, records, collected_at)
    storage.save_scores(today, scored)
    storage.save_artists(today, artist_table)
    print("  已写入 %s" % database.relative_to(BASE_DIR))

    # 6) 产出
    print("\n[6/6] 生成报告")
    output_dir = BASE_DIR / "output"
    csv_path = report.write_csv(scored, output_dir / ("新歌雷达-%s.csv" % today))
    print("  全量表格：%s（%d 条）" % (csv_path.relative_to(BASE_DIR), len(scored)))

    fresh = [item for item in scored if item.get("in_new_chart")]
    if fresh:
        fresh_path = report.write_csv(fresh, output_dir / ("新歌雷达-新发行-%s.csv" % today))
        print("  新发行榜：%s（%d 条）" % (fresh_path.relative_to(BASE_DIR), len(fresh)))

    xlsx_path, xlsx_note = report.write_xlsx(scored, output_dir / ("新歌雷达-%s.xlsx" % today))
    if xlsx_path:
        print("  Excel：%s" % xlsx_path.relative_to(BASE_DIR))
    else:
        print("  Excel：跳过（%s）" % xlsx_note)

    meta = {
        "title": title,
        "date": today,
        "total": len(records),
        "collected_at": collected_at,
    }
    dash = report.write_dashboard(scored, artist_table, source_status, meta, output_dir / "dashboard.html")
    site = report.write_dashboard(
        scored, artist_table, source_status, meta, BASE_DIR / "site" / "index.html"
    )
    print("  看板：%s" % dash.relative_to(BASE_DIR))
    print("  部署副本：%s" % site.relative_to(BASE_DIR))

    print()
    print(report.write_summary_text(scored, source_status, meta, top_n=10))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
