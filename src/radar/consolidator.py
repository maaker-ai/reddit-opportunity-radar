"""把多条原始信号二次加工为 Top N 机会摘要。

输入：scorer 输出的 is_signal=1 帖子列表
输出：{"top_opportunities": [...], "other_count": int}
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

LLM_ENDPOINT = "https://rxcrvznwlqcaqrlaxmdf.supabase.co/functions/v1/llm-chat"

CONSOLIDATE_PROMPT = """你是 App 机会分析师。用户给你一批已评分的 Reddit 需求信号（从多个 subreddit 扫来），你要：

1. 语义聚类：把"需求类别 × 目标用户"都相似的帖子归为同一机会簇（例如 3 个 "AI 辅助简历" 给职场人的帖子 = 1 个簇）
2. 打机会分：`机会分 = 簇内帖子数 × 平均 confidence × 类别权重`
   - 类别权重：NEED=1.2, REQUEST=1.1, PAIN=1.0, COMPLAINT=0.9
3. 评级：P0（机会分 ≥ 20）/ P1（≥ 10）/ P2（其他）
4. 排序：按机会分降序
5. Top 5：只输出前 5 个聚类（不足 5 全输出），其余合并到 other_count

严格返回以下 JSON（不要任何额外文字、不要 markdown fence）：
{{
  "top_opportunities": [
    {{
      "rank": 1,
      "priority": "P0",
      "theme": "简短主题，8 字内",
      "post_count": 3,
      "avg_confidence": 8.3,
      "opportunity_score": 29.88,
      "summary": "需求总结，一句中文",
      "app_idea": "可能的 App 方案，一句中文",
      "target_audience": "目标用户画像",
      "tech_difficulty": "低" 或 "中" 或 "高",
      "demand_strength": "高" 或 "中高" 或 "中" 或 "低",
      "subreddits": ["r/xxx", "r/yyy"],
      "evidence_permalinks": ["/r/.../comments/.../"]
    }}
  ],
  "other_count": 23
}}

输入信号列表：
{signals_json}
"""


def consolidate(
    signals: list[dict[str, Any]],
    model: str = "gemini-2.5-flash",
    endpoint: str = LLM_ENDPOINT,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Consolidate raw signals into prioritized opportunities.

    signals: list of dicts with keys subreddit, permalink, category,
             confidence, summary, app_idea, target_audience, is_signal
    """
    if not signals:
        return {"top_opportunities": [], "other_count": 0}

    compact = [
        {
            "subreddit": s.get("subreddit", ""),
            "permalink": s.get("permalink", ""),
            "category": s.get("category", "NONE"),
            "confidence": s.get("confidence", 0),
            "summary": (s.get("summary", "") or "")[:200],
            "app_idea": (s.get("app_idea", "") or "")[:200],
            "target_audience": (s.get("target_audience", "") or "")[:150],
        }
        for s in signals
        if s.get("is_signal")
    ]

    if not compact:
        return {"top_opportunities": [], "other_count": 0}

    prompt = CONSOLIDATE_PROMPT.format(
        signals_json=json.dumps(compact, ensure_ascii=False)
    )

    secret = os.getenv("LLM_CHAT_SECRET")
    if not secret:
        raise RuntimeError(
            "LLM_CHAT_SECRET env var required for consolidator"
        )

    resp = httpx.post(
        endpoint,
        headers={"Content-Type": "application/json", "X-API-Key": secret},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16000,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    choice = data["choices"][0]
    content = choice["message"]["content"]
    finish_reason = choice.get("finish_reason", "")
    if finish_reason == "length":
        print(
            f"[consolidator] WARN: response truncated (finish_reason=length, "
            f"content_len={len(content)}); attempting salvage"
        )
    return _parse_json_lenient(content)


def _parse_json_lenient(text: str) -> dict[str, Any]:
    """宽松解析：去 markdown fence + 截取第一个平衡的 {...} 块。
    响应被截断时尝试抢救：从 top_opportunities 数组中保留已完整的元素。
    """
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()

    start = t.find("{")
    if start < 0:
        raise ValueError(f"no JSON object found in consolidator response: {text[:200]}")

    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(t)):
        ch = t[i]
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
                return json.loads(t[start : i + 1])

    # 抢救：响应被截断。从第一个 `"top_opportunities": [` 开始，
    # 手动解析已完整的数组元素（到最后一个平衡的 }），拼出合法 JSON。
    salvaged = _salvage_truncated(t[start:])
    if salvaged is not None:
        print(f"[consolidator] salvaged {len(salvaged.get('top_opportunities', []))} items from truncated response")
        return salvaged
    raise ValueError("unbalanced JSON in consolidator response")


def _salvage_truncated(body: str) -> dict[str, Any] | None:
    """Truncated response salvage: extract completed top_opportunities items."""
    marker = '"top_opportunities"'
    m_idx = body.find(marker)
    if m_idx < 0:
        return None
    arr_start = body.find("[", m_idx)
    if arr_start < 0:
        return None

    items: list[dict[str, Any]] = []
    depth = 0
    in_str = False
    escape = False
    obj_start = -1
    for i in range(arr_start + 1, len(body)):
        ch = body[i]
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
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start >= 0:
                try:
                    items.append(json.loads(body[obj_start : i + 1]))
                except Exception:
                    break
                obj_start = -1
        elif ch == "]" and depth == 0:
            break

    if not items:
        return None
    return {"top_opportunities": items, "other_count": 0}
