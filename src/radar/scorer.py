"""调用 shared-backend 的 llm-chat Edge Function（OpenAI 兼容接口，后端走 Gemini）评分 Reddit 帖子。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

# shared-backend Supabase 项目的 llm-chat Edge Function（OpenAI 兼容 → Gemini）
# 注册表参考：~/.claude/skills/backend-services/SKILL.md 1.4 节
LLM_ENDPOINT = "https://rxcrvznwlqcaqrlaxmdf.supabase.co/functions/v1/llm-chat"

PROMPT_TEMPLATE = """你是一个 App 开发机会嗅探器。给你一个 Reddit 帖子（可能附带评论），判断它是否包含创业/App 开发的需求信号。

需求信号定义：
- NEED: 用户明确说"希望有个 App/工具能..."、"Is there a way to..."
- COMPLAINT: 用户吐槽现有 App/工具的明显缺陷
- PAIN: 用户描述重复性的日常痛点（暗示工具缺失）
- REQUEST: 直接在 r/SomebodyMakeThis 这类地方提需求

请严格返回以下 JSON 格式（不要任何额外文字、不要 markdown fence）：
{{
  "is_signal": true 或 false,
  "category": "NEED" | "COMPLAINT" | "PAIN" | "REQUEST" | "NONE",
  "confidence": 0-10 的整数,
  "summary": "一句中文总结用户的需求/痛点",
  "app_idea": "可能的 App 解决方案（一句中文）",
  "target_audience": "目标用户画像（中文）"
}}

帖子标题：{title}
帖子正文：{body}
Top 评论：
{top_comments}
"""


@dataclass
class Score:
    is_signal: bool
    category: str          # NEED / COMPLAINT / PAIN / REQUEST / NONE
    confidence: int        # 0-10
    summary: str
    app_idea: str
    target_audience: str
    raw: dict[str, Any]    # 原始返回，存库用

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Score":
        return cls(
            is_signal=bool(d.get("is_signal", False)),
            category=str(d.get("category", "NONE")).upper(),
            confidence=int(d.get("confidence", 0)),
            summary=str(d.get("summary", "")),
            app_idea=str(d.get("app_idea", "")),
            target_audience=str(d.get("target_audience", "")),
            raw=d,
        )


class Scorer:
    """调 shared-backend 的 llm-chat。

    鉴权：调用方必须提供 FUNCTION_SHARED_SECRET（通过环境变量 LLM_CHAT_SECRET 注入）
    作为 X-API-Key header。Secret 存在 Supabase Secrets 里，本地 .env 存一份副本。
    """

    def __init__(self, model: str, endpoint: str = LLM_ENDPOINT, timeout: float = 60.0):
        self.model = model
        self.endpoint = endpoint
        self.client = httpx.Client(timeout=timeout)
        self.secret = os.environ.get("LLM_CHAT_SECRET")
        if not self.secret:
            raise RuntimeError(
                "LLM_CHAT_SECRET env var required to call shared-backend llm-chat. "
                "Ask the project owner for FUNCTION_SHARED_SECRET and put it in .env."
            )

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def score(self, title: str, body: str, top_comments: list[str]) -> Score:
        comments_text = "\n".join(
            f"- {c.strip()[:500]}" for c in top_comments[:20]
        ) or "（无评论）"
        prompt = PROMPT_TEMPLATE.format(
            title=title.strip(),
            body=(body or "（空）").strip()[:4000],
            top_comments=comments_text[:6000],
        )
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        # llm-chat 需要 X-API-Key 鉴权（FUNCTION_SHARED_SECRET）
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self.secret,
        }
        resp = self.client.post(self.endpoint, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = _parse_json_lenient(content)
        return Score.from_dict(parsed)


def _parse_json_lenient(text: str) -> dict[str, Any]:
    """宽松解析：模型偶尔会包 ```json ... ```、带前后杂项、或平级多 JSON。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # 截取第一个 { 到对应 } 的块（支持嵌套）
    start = text.find("{")
    if start < 0:
        raise ValueError(f"没有找到 JSON 对象: {text[:200]}")
    depth = 0
    end = -1
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        # 退化到粗暴截取
        end = text.rfind("}")
    return json.loads(text[start : end + 1])
