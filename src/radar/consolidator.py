"""把多条原始信号二次加工为蓝海机会列表。

输入：scorer 输出的 is_signal=1 帖子列表 + 近期已推主题（去重用）
输出：{"opportunities": [...]}
每个机会包含 is_worth_telling 字段，由 LLM 判断是否蓝海/有差异化空间。
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

LLM_ENDPOINT = "https://rxcrvznwlqcaqrlaxmdf.supabase.co/functions/v1/llm-chat"

CONSOLIDATE_PROMPT = """你是出海独立开发 App 的机会分析师。用户给你一批从 Reddit 扫来的需求信号，你要：

1. **语义聚类**：把"核心需求 × 目标用户"都相似的帖子归为同一机会簇。
2. **对每个簇判断 `is_worth_telling`**：
   - **true（值得推送）的标准**：存在蓝海空间，或现有竞品/方案有明显差异化切入点（功能空白、定价空间、平台空白、地区空白等）。
   - **false（不推送）的标准**：
     a. 已经被现有成熟产品充分满足，且没有明显差异化空间；
     b. 与下方"近期已推过的主题列表"中任意一条**语义重复**（即使措辞不同，实质相同就算重复）；
     c. 信号太弱（只有 1 条吐槽且无明确需求表达）。
   - 当 `is_worth_telling=false` 时，**必须填 `skip_reason`**，其他字段可留空字符串。
3. **必须填写**（仅当 `is_worth_telling=true` 时）：
   - `differentiation`：具体的差异化切入点，2-3 句，要具体不要泛泛（如"现有 X 产品缺少 Y 功能，且定价对 Z 群体不友好"）
   - `competitor_landscape`：你了解的竞品简评，1-2 句（如"已有 Resume.io、Teal HQ，但均为通用模板，缺少 JD 定向优化"）
4. **theme 格式**：稳定可读的中文短句，格式为「核心功能 给 目标用户」，例如"AI 简历优化 给 求职者"。同一类需求每次应生成相同或高度相似的 theme，便于跨期去重。
5. 输入会包含两部分：
   - 本批次 signals（JSON 数组）
   - 近期已推主题列表 recent_themes（如有）——用于语义去重判断

严格返回以下 JSON（不要任何额外文字，不要 markdown fence，不要代码块标记）：
{{
  "opportunities": [
    {{
      "theme": "AI 简历优化 给 求职者",
      "is_worth_telling": true,
      "skip_reason": "",
      "summary": "求职者希望 AI 能基于岗位 JD 自动优化简历，现有工具都是通用模板...",
      "app_idea": "根据岗位 JD 一键生成定向简历，含 ATS 友好度评分",
      "target_audience": "求职者、职场新人、应届生",
      "differentiation": "现有 Resume.io / Kickresume 都是固定模板，无法针对具体 JD 定向改写；ATS 评分功能在免费工具中几乎空白",
      "competitor_landscape": "Resume.io、Teal HQ、Kickresume 主打通用模板；ChatGPT 虽可优化但缺结构化评分和一键导出",
      "subreddits": ["r/jobs", "r/resumes"],
      "evidence_permalinks": ["/r/jobs/comments/xxx/"]
    }}
  ]
}}

---

本批次信号：
{signals_json}
{recent_themes_section}"""


def consolidate(
    signals: list[dict[str, Any]],
    recent_themes: list[dict[str, Any]] | None = None,
    model: str = "gemini-2.5-flash",
    endpoint: str = LLM_ENDPOINT,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Consolidate raw signals into blue-ocean opportunity candidates.

    signals: list of dicts with keys subreddit, permalink, category,
             confidence, summary, app_idea, target_audience, is_signal
    recent_themes: list of dicts with keys theme, summary, pushed_at
                   (used for cross-period deduplication)
    """
    if not signals:
        return {"opportunities": []}

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
        return {"opportunities": []}

    # 构建近期已推主题段落（最多 50 条，只取 theme + summary）
    recent_themes_section = ""
    if recent_themes:
        trimmed = recent_themes[:50]
        recent_list = [
            {"theme": t.get("theme", ""), "summary": (t.get("summary", "") or "")[:100]}
            for t in trimmed
        ]
        recent_themes_section = (
            "\n近期已推主题列表（语义重复的请标记 is_worth_telling=false）：\n"
            + json.dumps(recent_list, ensure_ascii=False)
        )

    prompt = CONSOLIDATE_PROMPT.format(
        signals_json=json.dumps(compact, ensure_ascii=False),
        recent_themes_section=recent_themes_section,
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
    响应被截断时尝试抢救：从 opportunities 数组中保留已完整的元素。
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

    # 抢救：响应被截断。从第一个 `"opportunities": [` 开始，
    # 手动解析已完整的数组元素（到最后一个平衡的 }），拼出合法 JSON。
    salvaged = _salvage_truncated(t[start:])
    if salvaged is not None:
        print(f"[consolidator] salvaged {len(salvaged.get('opportunities', []))} items from truncated response")
        return salvaged
    raise ValueError("unbalanced JSON in consolidator response")


def _salvage_truncated(body: str) -> dict[str, Any] | None:
    """Truncated response salvage: extract completed opportunities items."""
    marker = '"opportunities"'
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
    return {"opportunities": items}
