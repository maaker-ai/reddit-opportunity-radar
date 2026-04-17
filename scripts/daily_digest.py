"""每日 Telegram 汇总（UTC 00:00 / UTC+8 08:00 跑一次）。

- 读过去 24 小时 SQLite 中命中的信号（confidence >= 6）
- 调 consolidator 做语义去重聚合（hourly 多次推过的同簇合并）
- 发一条汇总 Telegram 消息
- 无命中时仍发一条"昨天雷达安静"的消息（日报始终发送）
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from radar.consolidator import consolidate  # noqa: E402
from radar.notifier import _escape_md, send_message  # noqa: E402

DB = ROOT / "data" / "seen_posts.db"
REPO_URL = "https://github.com/maaker-ai/reddit-opportunity-radar"


def _load_recent_signals(conn: sqlite3.Connection, since_iso: str) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        "SELECT subreddit, permalink, category, confidence, score_json "
        "FROM seen_posts "
        "WHERE fetched_at > ? AND is_signal = 1 AND confidence >= 6",
        (since_iso,),
    )
    out = []
    for sub, permalink, category, confidence, score_json in cur.fetchall():
        score = {}
        if score_json:
            try:
                score = json.loads(score_json)
            except Exception:
                score = {}
        out.append(
            {
                "subreddit": sub,
                "permalink": permalink or "",
                "category": category or "NONE",
                "confidence": int(confidence or 0),
                "summary": score.get("summary", ""),
                "app_idea": score.get("app_idea", ""),
                "target_audience": score.get("target_audience", ""),
                "is_signal": True,
            }
        )
    return out


def _category_breakdown(conn: sqlite3.Connection, since_iso: str) -> list[tuple[str, int]]:
    cur = conn.cursor()
    cur.execute(
        "SELECT category, COUNT(*) FROM seen_posts "
        "WHERE fetched_at > ? AND is_signal = 1 AND confidence >= 6 "
        "GROUP BY category ORDER BY COUNT(*) DESC",
        (since_iso,),
    )
    return [(row[0], row[1]) for row in cur.fetchall()]


def _subreddit_distribution(
    conn: sqlite3.Connection, since_iso: str
) -> list[tuple[str, int]]:
    cur = conn.cursor()
    cur.execute(
        "SELECT subreddit, COUNT(*) FROM seen_posts "
        "WHERE fetched_at > ? AND is_signal = 1 AND confidence >= 6 "
        "GROUP BY subreddit ORDER BY COUNT(*) DESC LIMIT 5",
        (since_iso,),
    )
    return [(row[0], row[1]) for row in cur.fetchall()]


def _total_scanned(conn: sqlite3.Connection, since_iso: str) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM seen_posts WHERE fetched_at > ?", (since_iso,)
    )
    return cur.fetchone()[0] or 0


def main() -> int:
    load_dotenv(ROOT / ".env")
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        print("[digest] TELEGRAM_BOT_TOKEN not set, skipping")
        return 0

    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    if not DB.exists():
        send_message(
            "*Reddit 雷达日报*\n\n"
            "_当前无扫描历史（数据库不存在）。hourly cron 跑几次后就会有数据。_\n\n"
            f"仓库：{REPO_URL}"
        )
        print("[digest] no db yet, sent placeholder")
        return 0

    since_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    conn = sqlite3.connect(str(DB))
    try:
        total = _total_scanned(conn, since_iso)
        signals = _load_recent_signals(conn, since_iso)
        by_cat = _category_breakdown(conn, since_iso)
        by_sub = _subreddit_distribution(conn, since_iso)
    finally:
        conn.close()

    hits = len(signals)

    if hits == 0:
        msg = (
            f"*Reddit 雷达日报* · {today}\n\n"
            f"过去 24h：扫描 {total} 帖，0 命中\n\n"
            "_昨天雷达安静，没有达标的需求信号。_\n\n"
            f"完整历史：{REPO_URL}/tree/main/reports"
        )
        ok = send_message(msg)
        print(f"[digest] quiet day sent={ok} total={total}")
        return 0 if ok else 1

    consolidated: dict = {"top_opportunities": [], "other_count": 0}
    try:
        consolidated = consolidate(signals)
    except Exception as e:
        print(f"[digest] consolidator failed: {e}; falling back to flat list")

    top = consolidated.get("top_opportunities", []) or []
    other = int(consolidated.get("other_count", 0) or 0)

    lines = [
        f"📅 *Reddit 雷达日报* · {today}",
        "━━━━━━━━━━━━━━━━━━",
        f"过去 24h：扫描 {total} 帖，命中 {hits} 条",
        "",
    ]

    if top:
        lines.append(f"🔥 *Top {len(top)} 机会*（语义去重聚合）")
        lines.append("")
        for o in top:
            priority = o.get("priority", "P?")
            tag = "🔴" if priority == "P0" else ("🟡" if priority == "P1" else "⚪")
            theme = _escape_md(str(o.get("theme", "")))
            rank = o.get("rank", "?")
            post_count = o.get("post_count", 0)
            demand = _escape_md(str(o.get("demand_strength", "")))
            difficulty = _escape_md(str(o.get("tech_difficulty", "")))
            summary = _escape_md(str(o.get("summary", "")))
            app_idea = _escape_md(str(o.get("app_idea", "")))
            lines.append(f"{tag} *{rank}. {theme}* ({priority}, 聚合 {post_count} 帖)")
            lines.append(f"   需求:{demand} · 难度:{difficulty}")
            lines.append(f"   📝 {summary}")
            lines.append(f"   💡 {app_idea}")
            lines.append("")

    if by_cat:
        lines.append("*分类分布：*")
        lines.append("  " + " · ".join(f"{_escape_md(c)}:{n}" for c, n in by_cat))
        lines.append("")

    if by_sub:
        lines.append("*Top Subreddits：*")
        lines.append(
            "  " + " · ".join(f"r/{_escape_md(s)}:{n}" for s, n in by_sub)
        )
        lines.append("")

    if other > 0:
        lines.append(
            f"📋 其他 {other} 条 → [完整报告]({REPO_URL}/blob/main/reports/{today}.md)"
        )
    else:
        lines.append(f"完整历史：{REPO_URL}/tree/main/reports")

    msg = "\n".join(lines)
    ok = send_message(msg)
    print(
        f"[digest] sent={ok} total={total} hits={hits} "
        f"top={len(top)} other={other}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
