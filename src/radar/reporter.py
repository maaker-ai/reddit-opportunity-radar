"""Markdown 报告生成。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from radar.storage import StoredPost

CATEGORY_ZH = {
    "NEED": "NEED（明确需求）",
    "COMPLAINT": "COMPLAINT（现有工具吐槽）",
    "PAIN": "PAIN（日常痛点）",
    "REQUEST": "REQUEST（直接提需求）",
    "NONE": "NONE（无信号）",
}

CATEGORY_ORDER = ["NEED", "REQUEST", "COMPLAINT", "PAIN", "NONE"]


def write_report(
    report_path: Path,
    *,
    run_date: str,
    subreddits_scanned: int,
    posts: list[StoredPost],
    min_confidence: int,
) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)

    signals = [p for p in posts if p.is_signal and p.confidence >= min_confidence]
    grouped: dict[str, list[StoredPost]] = {c: [] for c in CATEGORY_ORDER}
    for p in signals:
        grouped.setdefault(p.category, []).append(p)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = []
    lines.append(f"# Reddit 机会雷达 — {run_date}")
    lines.append("")
    lines.append(f"生成时间：{now}")
    lines.append(f"扫描 subreddit：{subreddits_scanned} 个")
    lines.append(f"新帖子数：{len(posts)}")
    lines.append(f"命中信号（confidence >= {min_confidence}）：{len(signals)}")
    lines.append("")

    lines.append("## 按类别")
    lines.append("")
    any_signal = False
    for cat in CATEGORY_ORDER:
        items = grouped.get(cat, [])
        if cat == "NONE" or not items:
            continue
        any_signal = True
        lines.append(f"### {CATEGORY_ZH.get(cat, cat)}（{len(items)} 条）")
        lines.append("")
        for p in items:
            lines.extend(_render_signal(p))
        lines.append("")

    if not any_signal:
        lines.append("_本次运行未发现 confidence 达标的需求信号_")
        lines.append("")

    lines.append("## 完整已评估列表")
    lines.append("")
    lines.append("| Subreddit | 标题 | 类别 | Conf | 信号 |")
    lines.append("|-----------|------|------|------|------|")
    for p in posts:
        title = p.title.replace("|", "\\|")[:80]
        flag = "✅" if p.is_signal else "—"
        lines.append(
            f"| r/{p.subreddit} | [{title}]({p.permalink}) | "
            f"{p.category} | {p.confidence} | {flag} |"
        )

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def _render_signal(p: StoredPost) -> list[str]:
    created = (
        datetime.fromtimestamp(p.created_utc, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M")
        if p.created_utc
        else "未知"
    )
    s = p.score
    summary = s.get("summary", "")
    app_idea = s.get("app_idea", "")
    audience = s.get("target_audience", "")
    out = [
        f"#### [{p.title}]({p.permalink})",
        f"- **Subreddit**：r/{p.subreddit}",
        f"- **作者**：u/{p.author} | **发布时间**：{created}",
    ]
    if summary:
        out.append(f"- **需求总结**：{summary}")
    if app_idea:
        out.append(f"- **App 方案**：{app_idea}")
    if audience:
        out.append(f"- **目标用户**：{audience}")
    out.append(f"- **Confidence**：{p.confidence}/10")
    out.append("")
    return out
