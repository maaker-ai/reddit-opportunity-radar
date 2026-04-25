"""Telegram 推送：把单条蓝海机会作为独立消息推送。

鉴权：环境变量 TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID（见 .env.example）。
失败时只打印日志、不抛异常，避免影响主扫描流程。
"""
from __future__ import annotations

import os
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


def notify_opportunity(opp: dict[str, Any]) -> bool:
    """推送单条蓝海机会消息到 Telegram。返回是否成功。"""
    theme = _escape_md(str(opp.get("theme", "")))
    summary = _escape_md(str(opp.get("summary", "") or ""))
    app_idea = _escape_md(str(opp.get("app_idea", "") or ""))
    target_audience = _escape_md(str(opp.get("target_audience", "") or ""))
    differentiation = _escape_md(str(opp.get("differentiation", "") or ""))
    competitor_landscape = _escape_md(str(opp.get("competitor_landscape", "") or ""))

    lines: list[str] = []
    lines.append(f"💡 *{theme}*")
    lines.append("")

    if summary:
        lines.append(f"📝 {summary}")

    if app_idea:
        lines.append(f"💼 *App 想法*：{app_idea}")

    if differentiation:
        lines.append(f"🎯 *差异化*：{differentiation}")

    if competitor_landscape:
        lines.append(f"🥊 *竞品*：{competitor_landscape}")

    if target_audience:
        lines.append(f"👥 {target_audience}")

    # subreddits
    subs_raw = opp.get("subreddits") or []
    if isinstance(subs_raw, list) and subs_raw:
        subs_str = " · ".join(_escape_md(str(s)) for s in subs_raw[:6])
        lines.append("")
        lines.append(f"🔗 {subs_str}")

    # 第一条证据链接
    perms = opp.get("evidence_permalinks") or []
    if isinstance(perms, list) and perms:
        top_link = str(perms[0])
        if not top_link.startswith("http"):
            top_link = f"https://reddit.com{top_link}"
        lines.append(f"📖 [查看原帖]({top_link})")

    msg = "\n".join(lines)
    ok = send_message(msg)
    print(f"[notify] opportunity sent={ok}: theme={opp.get('theme', '')!r}")
    return ok
