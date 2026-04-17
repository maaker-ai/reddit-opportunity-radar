"""Telegram 推送：命中高分信号时主动通知。

鉴权：环境变量 TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID（见 .env.example）。
失败时只打印日志、不抛异常，避免影响主扫描流程。
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Mapping

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
                "text": text[:4000],  # Telegram 单消息 4096 上限，留余量
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
    """简单去掉 Markdown 特殊符号，避免消息解析失败。"""
    return (
        text.replace("*", "")
        .replace("_", "")
        .replace("[", "")
        .replace("]", "")
        .replace("`", "")
    )


def notify_signals(signals: Iterable[Mapping[str, Any]]) -> int:
    """
    signals 是本次扫描命中的帖子列表，每个 dict 至少含:
      subreddit, title, permalink, category, confidence, summary, app_idea, target_audience
    逐条发送（每条一个 Telegram 消息，便于阅读和单独分享）。
    返回发送成功的条数。
    """
    count = 0
    for s in signals:
        title = _escape_md(str(s.get("title", "")))[:100]
        subreddit = _escape_md(str(s.get("subreddit", "")))
        summary = _escape_md(str(s.get("summary", "")))
        app_idea = _escape_md(str(s.get("app_idea", "")))
        audience = _escape_md(str(s.get("target_audience", "")))
        category = _escape_md(str(s.get("category", "")))
        confidence = int(s.get("confidence", 0))
        permalink = str(s.get("permalink", ""))

        msg = (
            f"*Reddit 机会雷达* [{category}] conf:{confidence}/10\n\n"
            f"*r/{subreddit}*: {title}\n\n"
            f"*需求*：{summary}\n"
            f"*方案*：{app_idea}\n"
            f"*用户*：{audience}\n\n"
            f"[查看原帖]({permalink})"
        )
        if send_message(msg):
            count += 1
    return count
