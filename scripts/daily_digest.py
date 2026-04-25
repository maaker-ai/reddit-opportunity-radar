"""每日 Telegram 汇总（UTC 00:00 / UTC+8 08:00 跑一次）。

- 读过去 24 小时 SQLite 中命中的信号（confidence >= 6）
- 调 consolidator 做语义聚类 + 蓝海判断（同样传 recent_themes 去重）
- 把 is_worth_telling=true 的机会逐条推送（作为 hourly 漏推的兜底）
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
from radar.notifier import _escape_md, notify_opportunity, send_message  # noqa: E402
from radar.storage import Storage  # noqa: E402

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

    # 拉 30 天已推主题做去重（daily 和 hourly 共享同一张表）
    storage = Storage(DB)
    try:
        recent = storage.recent_themes(days=30)
    finally:
        storage.close()

    opportunities: list[dict] = []
    try:
        consolidated = consolidate(signals, recent_themes=recent)
        opportunities = consolidated.get("opportunities", []) or []
    except Exception as e:
        print(f"[digest] consolidator failed: {e}; will only send summary")

    worth_telling = [o for o in opportunities if o.get("is_worth_telling")]

    # 发一条简洁的日报头
    header = (
        f"📅 *Reddit 雷达日报* · {today}\n"
        f"过去 24h：扫描 {total} 帖，命中 {hits} 条"
    )
    if worth_telling:
        header += f"，发现 {len(worth_telling)} 条蓝海机会"
    else:
        header += "，无新蓝海机会（hourly 已推或无差异化空间）"

    send_message(header)

    # 逐条推送蓝海机会（作为 hourly 漏推的兜底）
    storage2 = Storage(DB)
    try:
        pushed_count = 0
        for opp in worth_telling:
            ok = notify_opportunity(opp)
            if ok:
                storage2.record_pushed(
                    theme=str(opp.get("theme", "")),
                    summary=str(opp.get("summary", "")),
                    app_idea=str(opp.get("app_idea", "")),
                    target_audience=str(opp.get("target_audience", "")),
                    differentiation=str(opp.get("differentiation", "")),
                    evidence_permalinks=opp.get("evidence_permalinks") or [],
                )
                pushed_count += 1
    finally:
        storage2.close()

    print(
        f"[digest] sent summary + {pushed_count} opportunities; "
        f"total={total} hits={hits} clusters={len(opportunities)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
