"""每日 Telegram 汇总（UTC 00:00 / UTC+8 08:00 跑一次）。

读过去 24 小时 SQLite 里的所有记录，生成一条汇总消息：
- 总扫描数 / 命中数
- 分类分布
- Top 5 高 confidence 信号（简略）
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 保证能 import src/radar/notifier.py
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from radar.notifier import send_message  # noqa: E402

DB = ROOT / "data" / "seen_posts.db"
REPO_URL = "https://github.com/maaker-ai/reddit-opportunity-radar"


def main() -> int:
    load_dotenv(ROOT / ".env")
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        print("[digest] TELEGRAM_BOT_TOKEN not set, skipping")
        return 0

    if not DB.exists():
        msg = (
            "*Reddit 雷达日报*\n\n"
            "_当前无扫描历史（数据库不存在）。hourly cron 跑几次后就会有数据。_\n\n"
            f"仓库：{REPO_URL}"
        )
        send_message(msg)
        print("[digest] no db yet, sent placeholder")
        return 0

    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    cur.execute(
        "SELECT COUNT(*) FROM seen_posts WHERE fetched_at > ?", (since,)
    )
    total = cur.fetchone()[0] or 0

    cur.execute(
        "SELECT COUNT(*) FROM seen_posts "
        "WHERE fetched_at > ? AND is_signal = 1 AND confidence >= 6",
        (since,),
    )
    hits = cur.fetchone()[0] or 0

    cur.execute(
        "SELECT category, COUNT(*) FROM seen_posts "
        "WHERE fetched_at > ? AND is_signal = 1 AND confidence >= 6 "
        "GROUP BY category ORDER BY COUNT(*) DESC",
        (since,),
    )
    by_cat = cur.fetchall()

    cur.execute(
        "SELECT subreddit, title, confidence, permalink FROM seen_posts "
        "WHERE fetched_at > ? AND is_signal = 1 AND confidence >= 6 "
        "ORDER BY confidence DESC, fetched_at DESC LIMIT 5",
        (since,),
    )
    top = cur.fetchall()
    conn.close()

    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    lines = [f"*Reddit 雷达日报* · {today}\n"]
    lines.append(f"过去 24h：扫描 {total} 帖，命中 {hits} 条\n")

    if by_cat:
        lines.append("*分类：*")
        for cat, n in by_cat:
            lines.append(f"  - {cat}: {n}")
        lines.append("")

    if top:
        lines.append("*Top 5 信号：*")
        for sub, title, conf, link in top:
            safe_title = (
                (title or "")[:50]
                .replace("*", "")
                .replace("_", "")
                .replace("[", "")
                .replace("]", "")
                .replace("`", "")
            )
            lines.append(f"  · [{conf}] r/{sub}: {safe_title}")
        lines.append("")

    lines.append(f"完整历史：{REPO_URL}/tree/main/reports")

    msg = "\n".join(lines)
    ok = send_message(msg)
    print(f"[digest] sent={ok} total={total} hits={hits} top={len(top)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
