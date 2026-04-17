# Reddit 机会雷达 (reddit-opportunity-radar)

每天自动扫描一批 Reddit subreddit，用 Claude 判断哪些帖子/评论里藏着 App 开发机会（明确需求、对现有工具的吐槽、日常痛点等），生成 Markdown 报告。

**关键设计**：Reddit 2025-11 后关闭了自助 API 申请。本项目**不走 OAuth**，直接读 Reddit 的公开 `.json` 端点（任何 URL 后缀加 `.json` 返回结构化数据，无需鉴权），配合保守限速确保合规。

## 技术栈

- Python 3.11+
- `httpx` — HTTP 客户端（同步）
- `PyYAML` — 配置
- `python-dotenv` — 读 `.env`
- OpenRouter — 调用 Claude（`anthropic/claude-sonnet-4.6`）
- SQLite — 去重 + 历史存档

## Quick Start

```bash
# 1. 进入项目目录
cd reddit-opportunity-radar

# 2. 安装依赖（uv 或 pip 任选）
uv sync
# 或
pip install -e .

# 3. 配置 OpenRouter API Key
cp .env.example .env
# 然后编辑 .env，把 OPENROUTER_API_KEY 换成你的 key

# 4. 编辑 config.yaml，把 user_agent 里的 `placeholder` 换成你真实的 Reddit 用户名
#    Reddit 会通过 User-Agent 识别身份，保留 placeholder 可能被风控
#    格式示例：opportunity-radar/0.1 by your_reddit_username

# 5. 跑一次
uv run python -m radar
# 或安装后使用 entry point
uv run radar
```

命令行参数：

```bash
python -m radar --help
# --subreddits sub1,sub2   覆盖 config 里的 subreddit 列表
# --dry-run                只拉帖子、不调 LLM、不写 DB（排障用）
# --limit N                每个 subreddit 最多处理 N 条
```

## 配置说明 (`config.yaml`)

```yaml
subreddits:
  - SomebodyMakeThis     # 直接提需求的地方，含金量最高
  - AppIdeas
  # ...

fetch:
  posts_per_subreddit: 25       # 每个 sub 拉最新 25 条
  fetch_comments_for_candidates: true
  max_comments_per_post: 20
  rate_limit_qpm: 6             # 客户端限速，官方限 10 QPM，我们留余量
  user_agent: "opportunity-radar/0.1 by <你的 Reddit 用户名>"

scoring:
  model: "anthropic/claude-sonnet-4.6"
  min_confidence: 6             # 只把 confidence >= 6 写入报告「命中」部分
```

### 加一个新的 subreddit

直接在 `subreddits:` 列表下加一行即可（不需要带 `r/` 前缀）。下次跑会自动拉，SQLite 会按 `post_id` 全局去重，不会重复评分。

## 输出

- `data/seen_posts.db` — SQLite，所有扫描过的帖子 + 评分
- `reports/YYYY-MM-DD.md` — 当天报告（每次运行会 **覆盖** 同一天的报告）

## FAQ

**Q: 为什么不用 PRAW 或官方 API？**
A: Reddit 在 2025-11 后关闭了自助 API 申请，新账号拿不到 OAuth credentials。但公开 `.json` 端点仍开放，只读、无鉴权、官方 10 QPM 限速，对机会嗅探这种低频场景够用。

**Q: 会被 ban 吗？**
A: 我们客户端限速 6 QPM（低于官方 10），始终带合法 User-Agent，只读不发帖，风险极低。如果仍想更保守，把 `rate_limit_qpm` 调到 3–4。

**Q: 政策合规？**
A: 只做以下场景：
- 非商业使用 / 个人选品辅助
- 只读，不抓取用户个人信息做画像
- 不用 Reddit 数据训练任何模型
- 不公开转发原帖完整内容（报告里只保留标题 + 链接 + AI 总结）

**Q: Claude 为什么走 OpenRouter 而不是官方 API？**
A: 统一网关、一把 Key 访问多家模型、方便切换模型对比效果。这是用户的全局规范。

**Q: 遇到 429 怎么办？**
A: 客户端会自动 sleep 60s 重试一次，若仍失败会打印错误并继续下一条。把 `rate_limit_qpm` 调低即可。

## 项目结构

```
reddit-opportunity-radar/
  README.md
  pyproject.toml
  .env.example              # 填 OPENROUTER_API_KEY
  .gitignore
  config.yaml
  src/radar/
    __init__.py
    __main__.py             # python -m radar 入口
    main.py                 # CLI + 主流程
    reddit_client.py        # .json 端点 + 限速
    scorer.py               # OpenRouter 调用
    storage.py              # SQLite schema + CRUD
    reporter.py             # Markdown 报告
  data/                     # 运行时创建，SQLite 文件
  reports/                  # 运行时创建，每日报告
```

## TODO

- [ ] cron / launchd / systemd timer 每日自动跑（macOS 可以 `crontab -e` 加一行）
- [ ] Telegram Bot 推送命中信号（报告生成后发消息到 channel）
- [ ] 多语言报告（英文版同步生成）
- [ ] 历史趋势：同一主题持续多天出现时自动聚类
- [ ] 用 `r/subreddit/top.json?t=day` 补充热门帖（目前只拉 `/new.json`）
- [ ] 评分改为两阶段：先用便宜模型（Haiku/GPT-4o-mini）粗筛，再用 Sonnet 细评
