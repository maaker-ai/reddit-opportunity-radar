"""入口：python -m radar"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from radar.notifier import notify_signals
from radar.reddit_client import Post, RedditClient
from radar.reporter import write_report
from radar.scorer import Score, Scorer
from radar.storage import Storage

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config.yaml"
DEFAULT_DB = ROOT / "data" / "seen_posts.db"
REPORTS_DIR = ROOT / "reports"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="radar", description="Reddit 机会雷达")
    p.add_argument(
        "--subreddits",
        help="逗号分隔的 subreddit 列表（覆盖 config）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只拉帖子、不调 LLM、不写 DB",
    )
    p.add_argument(
        "--limit",
        type=int,
        help="每个 subreddit 处理的帖子数上限（覆盖 config）",
    )
    p.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="config.yaml 路径",
    )
    return p.parse_args(argv)


def load_config(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args(argv)
    cfg = load_config(args.config)

    subreddits: list[str] = (
        [s.strip() for s in args.subreddits.split(",") if s.strip()]
        if args.subreddits
        else list(cfg.get("subreddits", []))
    )
    if not subreddits:
        print("未配置任何 subreddit，退出。", file=sys.stderr)
        return 2

    fetch_cfg = cfg.get("fetch", {})
    scoring_cfg = cfg.get("scoring", {})
    limit = args.limit or fetch_cfg.get("posts_per_subreddit", 25)
    fetch_comments = bool(fetch_cfg.get("fetch_comments_for_candidates", True))
    max_comments = int(fetch_cfg.get("max_comments_per_post", 20))
    qpm = int(fetch_cfg.get("rate_limit_qpm", 6))
    user_agent = str(fetch_cfg.get("user_agent", "opportunity-radar/0.1 by placeholder"))
    model = str(scoring_cfg.get("model", "anthropic/claude-sonnet-4.6"))
    min_conf = int(scoring_cfg.get("min_confidence", 6))

    run_date = datetime.now().strftime("%Y-%m-%d")

    print(
        f"[start] 日期={run_date}  subs={len(subreddits)}  limit={limit}  "
        f"dry_run={args.dry_run}  model={model}"
    )

    storage = Storage(DEFAULT_DB) if not args.dry_run else None
    scorer: Scorer | None = None
    if not args.dry_run:
        scorer = Scorer(model=model)

    reddit = RedditClient(user_agent=user_agent, qpm=qpm)

    total_fetched = 0
    total_new = 0
    total_signals = 0
    processed_ids: list[str] = []  # 本次运行评分过的所有帖子
    try:
        for sub in subreddits:
            try:
                posts = reddit.fetch_new_posts(sub, limit=limit)
            except Exception as e:
                print(f"[r/{sub}] 拉取失败: {e}")
                continue
            seen = storage.seen_ids(sub) if storage else set()
            new_posts = [p for p in posts if p.post_id not in seen]
            total_fetched += len(posts)
            total_new += len(new_posts)

            sub_signals = 0
            for post in new_posts:
                if args.dry_run:
                    print(f"  [dry] r/{sub} | {post.post_id} | {post.title[:70]}")
                    continue
                comments: list[str] = []
                if fetch_comments:
                    try:
                        comments = reddit.fetch_comments(
                            post.post_id, limit=max_comments
                        )
                    except Exception as e:
                        print(f"  [warn] 评论拉取失败 {post.post_id}: {e}")
                try:
                    score = scorer.score(post.title, post.selftext, comments)  # type: ignore[union-attr]
                except Exception as e:
                    print(f"  [warn] 评分失败 {post.post_id}: {e}")
                    continue
                _persist(storage, post, score)  # type: ignore[arg-type]
                processed_ids.append(post.post_id)
                if score.is_signal and score.confidence >= min_conf:
                    sub_signals += 1
                    total_signals += 1

            print(
                f"[r/{sub}] {len(posts)} posts fetched, {len(new_posts)} new, "
                f"{sub_signals} signals detected"
            )
    finally:
        reddit.close()
        if scorer is not None:
            scorer.close()

    if args.dry_run:
        print(
            f"[done-dry] fetched={total_fetched} new={total_new}（dry-run 不写库不生成报告）"
        )
        return 0

    assert storage is not None
    rows = storage.query_by_ids(processed_ids)
    report_path = REPORTS_DIR / f"{run_date}.md"
    write_report(
        report_path,
        run_date=run_date,
        subreddits_scanned=len(subreddits),
        posts=rows,
        min_confidence=min_conf,
    )

    # Telegram 推送：只推本次扫描新命中的高分信号
    new_signals = [
        {
            "subreddit": p.subreddit,
            "title": p.title,
            "permalink": p.permalink,
            "category": p.category,
            "confidence": p.confidence,
            "summary": p.score.get("summary", ""),
            "app_idea": p.score.get("app_idea", ""),
            "target_audience": p.score.get("target_audience", ""),
        }
        for p in rows
        if p.is_signal and p.confidence >= min_conf
    ]
    if new_signals and os.getenv("TELEGRAM_BOT_TOKEN"):
        print(f"[notify] pushing {len(new_signals)} signals to Telegram")
        sent = notify_signals(new_signals)
        print(f"[notify] sent {sent}/{len(new_signals)}")
    elif new_signals:
        print(
            f"[notify] {len(new_signals)} signals detected but "
            "TELEGRAM_BOT_TOKEN not set; skipping push"
        )
    else:
        print("[notify] 0 signals to push")

    storage.close()
    print(
        f"[done] fetched={total_fetched} new={total_new} "
        f"signals>={min_conf}:{total_signals}"
    )
    print(f"[report] {report_path}")
    return 0


def _persist(storage: Storage, post: Post, score: Score) -> None:
    storage.insert(
        post_id=post.post_id,
        subreddit=post.subreddit,
        title=post.title,
        author=post.author,
        url=post.url,
        permalink=post.permalink,
        created_utc=post.created_utc,
        score=score.raw,
        is_signal=score.is_signal,
        category=score.category,
        confidence=score.confidence,
    )


if __name__ == "__main__":
    sys.exit(main())
