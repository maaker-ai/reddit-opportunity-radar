"""SQLite 存储层。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_posts (
  post_id TEXT PRIMARY KEY,
  subreddit TEXT NOT NULL,
  title TEXT NOT NULL,
  author TEXT,
  url TEXT,
  permalink TEXT,
  created_utc INTEGER,
  fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  score_json TEXT,
  is_signal INTEGER,
  category TEXT,
  confidence INTEGER
);
CREATE INDEX IF NOT EXISTS idx_subreddit ON seen_posts(subreddit);
CREATE INDEX IF NOT EXISTS idx_is_signal ON seen_posts(is_signal, confidence);
"""


@dataclass
class StoredPost:
    post_id: str
    subreddit: str
    title: str
    author: str
    url: str
    permalink: str
    created_utc: int
    fetched_at: str
    is_signal: int
    category: str
    confidence: int
    score: dict[str, Any]


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def seen_ids(self, subreddit: str) -> set[str]:
        cur = self.conn.execute(
            "SELECT post_id FROM seen_posts WHERE subreddit = ?",
            (subreddit,),
        )
        return {row["post_id"] for row in cur.fetchall()}

    def has_post(self, post_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM seen_posts WHERE post_id = ?", (post_id,)
        )
        return cur.fetchone() is not None

    def insert(
        self,
        *,
        post_id: str,
        subreddit: str,
        title: str,
        author: str,
        url: str,
        permalink: str,
        created_utc: int,
        score: dict[str, Any] | None,
        is_signal: bool,
        category: str,
        confidence: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO seen_posts
              (post_id, subreddit, title, author, url, permalink,
               created_utc, score_json, is_signal, category, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                subreddit,
                title,
                author,
                url,
                permalink,
                created_utc,
                json.dumps(score, ensure_ascii=False) if score else None,
                1 if is_signal else 0,
                category,
                confidence,
            ),
        )
        self.conn.commit()

    def query_since(self, since_iso: str) -> list[StoredPost]:
        cur = self.conn.execute(
            """
            SELECT * FROM seen_posts
            WHERE fetched_at >= ?
            ORDER BY is_signal DESC, confidence DESC, fetched_at DESC
            """,
            (since_iso,),
        )
        return [self._row_to_post(r) for r in cur.fetchall()]

    def query_by_ids(self, post_ids: Iterable[str]) -> list[StoredPost]:
        ids = list(post_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        cur = self.conn.execute(
            f"""
            SELECT * FROM seen_posts
            WHERE post_id IN ({placeholders})
            ORDER BY is_signal DESC, confidence DESC, fetched_at DESC
            """,
            ids,
        )
        return [self._row_to_post(r) for r in cur.fetchall()]

    @staticmethod
    def _row_to_post(row: sqlite3.Row) -> StoredPost:
        score: dict[str, Any] = {}
        if row["score_json"]:
            try:
                score = json.loads(row["score_json"])
            except Exception:
                score = {}
        return StoredPost(
            post_id=row["post_id"],
            subreddit=row["subreddit"],
            title=row["title"],
            author=row["author"] or "",
            url=row["url"] or "",
            permalink=row["permalink"] or "",
            created_utc=int(row["created_utc"] or 0),
            fetched_at=row["fetched_at"] or "",
            is_signal=int(row["is_signal"] or 0),
            category=row["category"] or "NONE",
            confidence=int(row["confidence"] or 0),
            score=score,
        )
