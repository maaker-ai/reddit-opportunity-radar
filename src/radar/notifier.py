"""Telegram 推送：把 LLM 二次加工后的机会摘要作为单条消息推送。

鉴权：环境变量 TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID（见 .env.example）。
失败时只打印日志、不抛异常，避免影响主扫描流程。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

TG_API = "https://api.telegram.org/bot{token}/sendMessage"


def _token() -> str:
    t = os.getenv("TELEGRAM_BOT_TOKEN")
    if not t:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env var required")
    return t


def _chat_id() -> str:
    c = os.getenv("TELEGRAM_CHAT_ID")
    if not c:
        raise RuntimeError("TELEGRAM_CHAT_ID env var required")
    return c


def send_message(text: str, parse_mode: str = "Markdown") -> bool:
    """发送一条 Telegram 消息。失败不抛异常只 print，返回是否成功。"""
    try:
        r = httpx.post(
            TG_API.format(token=_token()),
            data={
                "chat_id": _chat_id(),
                "text": text[:4000],
                "parse_mode": parse_mode,
                "disable_web_page_preview": "false",
            },
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[telegram] non-200: {r.status_code} {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"[telegram] send failed: {e}")
        return False


def _escape_md(text: str) -> str:
    return (
        text.replace("*", "")
        .replace("_", "")
        .replace("[", "")
        .replace("]", "")
        .replace("`", "")
    )


def notify_digest(
    consolidated: dict[str, Any],
    scan_time: str,
    new_count: int,
    total_signals: int,
) -> bool:
    """Send one consolidated digest. Returns True if pushed, False if suppressed."""
    top = consolidated.get("top_opportunities", []) or []
    other = int(consolidated.get("other_count", 0) or 0)

    push_worthy = [o for o in top if o.get("priority") in ("P0", "P1")]
    if not push_worthy:
        print(
            f"[notify] suppressed: no P0/P1 opportunities "
            f"(total={len(top)}, other={other})"
        )
        return False

    report_date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    lines = [
        f"🎯 *Reddit 雷达* · {scan_time}",
        "━━━━━━━━━━━━━━━━━━",
        f"📊 {new_count} 新帖 → {total_signals} 信号 → {len(push_worthy)} 热门机会",
        "",
        f"🔥 *Top {len(push_worthy)}*",
        "",
    ]

    for o in push_worthy:
        tag = "🔴" if o.get("priority") == "P0" else "🟡"
        theme = _escape_md(str(o.get("theme", "")))
        priority = o.get("priority", "P?")
        post_count = o.get("post_count", 0)
        rank = o.get("rank", "?")
        demand = _escape_md(str(o.get("demand_strength", "")))
        difficulty = _escape_md(str(o.get("tech_difficulty", "")))
        summary = _escape_md(str(o.get("summary", "")))
        app_idea = _escape_md(str(o.get("app_idea", "")))
        audience = _escape_md(str(o.get("target_audience", "")))

        lines.append(
            f"{tag} *{rank}. {theme}* ({priority}, 聚合 {post_count} 帖)"
        )
        lines.append(f"   需求:{demand} · 难度:{difficulty}")
        lines.append(f"   📝 {summary}")
        lines.append(f"   💡 {app_idea}")
        lines.append(f"   👥 {audience}")

        subs_raw = o.get("subreddits") or []
        if isinstance(subs_raw, list):
            subs = " ".join(_escape_md(str(s)) for s in subs_raw[:4])
            lines.append(f"   🔗 {subs}")

        perms = o.get("evidence_permalinks") or []
        if isinstance(perms, list) and perms:
            top_link = str(perms[0])
            if top_link.startswith("http"):
                url = top_link
            else:
                url = f"https://reddit.com{top_link}"
            lines.append(f"   📖 [查看原帖]({url})")
        lines.append("")

    if other > 0:
        lines.append(
            f"📋 其他 {other} 条 → "
            f"[GitHub reports](https://github.com/maaker-ai/reddit-opportunity-radar/blob/main/reports/{report_date}.md)"
        )

    msg = "\n".join(lines)
    ok = send_message(msg)
    p0 = len([o for o in push_worthy if o.get("priority") == "P0"])
    p1 = len([o for o in push_worthy if o.get("priority") == "P1"])
    print(
        f"[notify] digest sent={ok}: P0={p0}, P1={p1}, other={other}"
    )
    return ok
