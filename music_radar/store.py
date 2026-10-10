"""SQLite 快照存储。

每天都存一份原始上榜记录和一份打分结果，这样"榜单爬升"才有对比基准——
这是整套系统里唯一无法从任何单个接口直接拿到、只能靠自己连续采集积累的信号。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    date          TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chart         TEXT NOT NULL,
    chart_kind    TEXT,
    rank          INTEGER,
    title         TEXT NOT NULL,
    artists       TEXT,
    album         TEXT,
    publish_time  TEXT,
    duration_ms   INTEGER,
    url           TEXT,
    collected_at  TEXT,
    PRIMARY KEY (date, platform, chart, rank, title)
);

CREATE TABLE IF NOT EXISTS daily_score (
    date          TEXT NOT NULL,
    track_key     TEXT NOT NULL,
    title         TEXT NOT NULL,
    artists       TEXT,
    album         TEXT,
    publish_time  TEXT,
    platforms     TEXT,
    platform_count INTEGER,
    best_rank     INTEGER,
    best_chart    TEXT,
    prev_rank     INTEGER,
    rank_delta    INTEGER,
    artist_hot    REAL,
    score         REAL,
    parts         TEXT,
    is_new_release INTEGER,
    PRIMARY KEY (date, track_key)
);

CREATE TABLE IF NOT EXISTS artist_daily (
    date        TEXT NOT NULL,
    artist_key  TEXT NOT NULL,
    name        TEXT,
    songs       INTEGER,
    hot         REAL,
    music_size  INTEGER,
    platforms   TEXT,
    PRIMARY KEY (date, artist_key)
);

CREATE INDEX IF NOT EXISTS idx_score_date ON daily_score(date);
CREATE INDEX IF NOT EXISTS idx_score_key ON daily_score(track_key);
CREATE INDEX IF NOT EXISTS idx_artist_date ON artist_daily(date);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------- 写入

    def save_snapshot(self, date: str, records: list[dict], collected_at: str) -> None:
        rows = [
            (
                date,
                record.get("platform", ""),
                record.get("chart", ""),
                record.get("chart_kind", ""),
                int(record.get("rank") or 0),
                record.get("title", ""),
                json.dumps(record.get("artists") or [], ensure_ascii=False),
                record.get("album", ""),
                record.get("publish_time"),
                record.get("duration_ms"),
                record.get("url", ""),
                collected_at,
            )
            for record in records
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO snapshot "
            "(date, platform, chart, chart_kind, rank, title, artists, album, "
            " publish_time, duration_ms, url, collected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

    def save_scores(self, date: str, scored: list[dict]) -> None:
        self.conn.execute("DELETE FROM daily_score WHERE date = ?", (date,))
        self.conn.executemany(
            "INSERT OR REPLACE INTO daily_score "
            "(date, track_key, title, artists, album, publish_time, platforms, "
            " platform_count, best_rank, best_chart, prev_rank, rank_delta, "
            " artist_hot, score, parts, is_new_release) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    date,
                    item["key"],
                    item["title"],
                    json.dumps(item["artists"], ensure_ascii=False),
                    item["album"],
                    item["publish_time"],
                    json.dumps(item["platforms"], ensure_ascii=False),
                    item["platform_count"],
                    item["best_rank"] or None,
                    item["best_chart"],
                    item["prev_rank"],
                    item["rank_delta"],
                    item["artist_hot"],
                    item["score"],
                    json.dumps(item["parts"], ensure_ascii=False),
                    1 if item["is_new_release"] else 0,
                )
                for item in scored
            ],
        )
        self.conn.commit()

    def save_artists(self, date: str, artist_table: dict[str, dict]) -> None:
        self.conn.execute("DELETE FROM artist_daily WHERE date = ?", (date,))
        self.conn.executemany(
            "INSERT OR REPLACE INTO artist_daily "
            "(date, artist_key, name, songs, hot, music_size, platforms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    date,
                    key,
                    row["name"],
                    row["songs"],
                    round(row["hot"] * 100, 1),
                    row.get("music_size"),
                    json.dumps(row.get("platforms") or [], ensure_ascii=False),
                )
                for key, row in artist_table.items()
            ],
        )
        self.conn.commit()

    # ------------------------------------------------------------- 读取

    def previous_best_rank(self, date: str) -> dict[str, int]:
        """取上一次采集里每首歌的最佳名次，用于计算榜单爬升。"""
        row = self.conn.execute(
            "SELECT MAX(date) AS d FROM daily_score WHERE date < ?", (date,)
        ).fetchone()
        if not row or not row["d"]:
            return {}
        prev_date = row["d"]
        return {
            item["track_key"]: item["best_rank"]
            for item in self.conn.execute(
                "SELECT track_key, best_rank FROM daily_score "
                "WHERE date = ? AND best_rank IS NOT NULL",
                (prev_date,),
            )
        }

    def last_snapshot_date(self, before: str) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(date) AS d FROM daily_score WHERE date < ?", (before,)
        ).fetchone()
        return row["d"] if row and row["d"] else None

    def recent_dates(self, limit: int = 14) -> list[str]:
        return [
            row["date"]
            for row in self.conn.execute(
                "SELECT DISTINCT date FROM daily_score ORDER BY date DESC LIMIT ?", (limit,)
            )
        ]

    def top_artists(self, date: str, limit: int = 15) -> list[dict]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT name, songs, hot, music_size, platforms FROM artist_daily "
                "WHERE date = ? ORDER BY hot DESC, songs DESC LIMIT ?",
                (date, limit),
            )
        ]

    def score_stats(self, date: str) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "       SUM(CASE WHEN is_new_release = 1 THEN 1 ELSE 0 END) AS fresh, "
            "       AVG(score) AS avg_score "
            "FROM daily_score WHERE date = ?",
            (date,),
        ).fetchone()
        platforms = self.conn.execute(
            "SELECT COUNT(DISTINCT platform) AS c FROM snapshot WHERE date = ?", (date,)
        ).fetchone()
        return {
            "total": (row["total"] or 0) if row else 0,
            "fresh": (row["fresh"] or 0) if row else 0,
            "avg_score": round(row["avg_score"] or 0.0, 1) if row else 0.0,
            "platforms": (platforms["c"] or 0) if platforms else 0,
        }
