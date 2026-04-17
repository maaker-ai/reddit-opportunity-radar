# Reddit 机会雷达 (reddit-opportunity-radar)

![Hourly Scan](https://github.com/maaker-ai/reddit-opportunity-radar/actions/workflows/hourly-scan.yml/badge.svg)
![Daily Digest](https://github.com/maaker-ai/reddit-opportunity-radar/actions/workflows/daily-digest.yml/badge.svg)

每小时自动扫描一批 Reddit subreddit，用 LLM 判断哪些帖子/评论里藏着 App 开发机会（明确需求、对现有工具的吐槽、日常痛点等），把高分信号主动推送到 Telegram，并把 Markdown 报告归档到本仓库 `reports/` 目录。

**关键设计**：Reddit 2025-11 后关闭了自助 API 申请。本项目**不走 OAuth**，直接读 Reddit 的公开 `.json` 端点（任何 URL 后缀加 `.json` 返回结构化数据，无需鉴权），配合保守限速确保合规。

## Deployment

本仓库默认部署在 GitHub Actions，**不依赖任何本地机器**：

- `.github/workflows/hourly-scan.yml` — 每小时（UTC 整点）跑一次扫描
  - SQLite 去重状态通过 `actions/cache` 跨运行持久化
  - 新报告自动 commit 到 `reports/YYYY-MM-DD.md`
  - confidence ≥ 6 的信号逐条推送到 Telegram
- `.github/workflows/daily-digest.yml` — 每天 UTC 00:00（北京时间 08:00）跑一次
  - 汇总过去 24h 的扫描 / 命中 / 分类分布 / Top 5 信号
  - 发一条 Telegram 汇总消息

### 环境变量 / Secrets

| 变量 | 用途 | 本地 `.env` | GitHub Actions Secret |
|------|------|-------------|------------------------|
| `LLM_CHAT_SECRET` | shared-backend `llm-chat` Edge Function 的 `X-API-Key` | 必填 | 以 `FUNCTION_SHARED_SECRET` 存储 |
| `TELEGRAM_BOT_TOKEN` | Bot 令牌（@BotFather 获取） | 可选，没填则跳过推送 | 必填，否则不推送 |
| `TELEGRAM_CHAT_ID` | 收消息的个人/群 Chat ID | 同上 | 同上 |
| `REDDIT_PROXY_URL` | Cloudflare Worker 透明代理 URL | 可选（住宅 IP 直连可跳过） | 必填（数据中心 IP 被 Reddit 拦） |
| `REDDIT_PROXY_SECRET` | Worker 鉴权 secret | 同上 | 同上 |

值请向 project owner 索取，公开仓库严禁写入源码 / commit / 日志。

### Reddit 代理（CF Worker）

Reddit 2026 起封锁了 Azure/GCP/AWS 的 IP 段，GitHub Actions runner 直连会全部返回 403。本项目通过 Cloudflare Worker 做**透明代理**绕过——CF 边缘 IP 不在 Reddit 黑名单里。

Worker 源码独立维护在 `reddit-proxy-worker` 项目。客户端自动检测：`REDDIT_PROXY_URL` 和 `REDDIT_PROXY_SECRET` 都设置时走代理（用浏览器 User-Agent 伪装），否则直连（用 config.yaml 里的 UA + DoH DNS 回退）。

## 技术栈

- Python 3.11+
- `httpx` — HTTP 客户端（同步）
- `PyYAML` — 配置
- `python-dotenv` — 读 `.env`（当前无必填项，保留仅为未来扩展）
- **shared-backend `llm-chat` Edge Function** — OpenAI 兼容接口，后端走 Gemini 2.5 Flash；本地需要一份 `FUNCTION_SHARED_SECRET` 作为 `X-API-Key`，不需要直接的 LLM API Key
- SQLite — 去重 + 历史存档

## Quick Start

```bash
# 1. 进入项目目录
cd reddit-opportunity-radar

# 2. 安装依赖（uv 或 pip 任选）
uv sync
# 或
pip install -e .

# 3. 配置 Secret：llm-chat 已启用 X-API-Key 鉴权
#     cp .env.example .env
#     # 向项目 owner 索取 FUNCTION_SHARED_SECRET
#     # 写入 .env 的 LLM_CHAT_SECRET=<secret>
#     # .env 已在 .gitignore，不会进 git

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
  model: "gemini-2.5-flash"     # shared-backend llm-chat 白名单内，后端走 Gemini
  min_confidence: 6             # 只把 confidence >= 6 写入报告「命中」部分
```

**可选模型**（写进 `scoring.model`）：
- `gemini-2.5-flash`（默认）
- `gemini-2.0-flash`
- `gemini-2.5-pro`

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

**Q: 为什么不直接调 Gemini / OpenRouter？**
A: 统一经 `shared-backend` 的 `llm-chat` Edge Function（OpenAI 兼容），好处是：
- 本地无需直接持有 LLM 厂商的 API Key，Key 集中在 Supabase Secrets
- 后端可平滑替换模型（当前 Gemini，未来换其他家也不改本项目代码）
- 多项目共用一个接口，便于监控和限额
接口注册在 `~/.claude/skills/backend-services/SKILL.md` 的 §1.4。
注意：本地仍需一个 `LLM_CHAT_SECRET`（对应 Supabase 的 `FUNCTION_SHARED_SECRET`）作为 `X-API-Key` 调用鉴权，避免公开端点被滥用。

**Q: 遇到 429 怎么办？**
A: 客户端会自动 sleep 60s 重试一次，若仍失败会打印错误并继续下一条。把 `rate_limit_qpm` 调低即可。

## 项目结构

```
reddit-opportunity-radar/
  README.md
  pyproject.toml
  .env.example              # 必填 LLM_CHAT_SECRET；复制为 .env 后填入
  .gitignore
  config.yaml
  src/radar/
    __init__.py
    __main__.py             # python -m radar 入口
    main.py                 # CLI + 主流程
    reddit_client.py        # .json 端点 + 限速
    scorer.py               # shared-backend llm-chat 调用（OpenAI 兼容 → Gemini）
    storage.py              # SQLite schema + CRUD
    reporter.py             # Markdown 报告
  data/                     # 运行时创建，SQLite 文件
  reports/                  # 运行时创建，每日报告
```

## Disclaimer

本项目仅用于个人非商业的选品/机会嗅探，遵循以下原则：

- 只读 Reddit 公开 `.json` 端点，不做身份抓取，不绕过任何鉴权
- 客户端限速 6 QPM（低于 Reddit 官方 10 QPM 限额）
- 不转发原帖完整正文（报告内仅保留标题 / 链接 / AI 总结）
- 不用任何数据训练模型、不二次分发、不商业化

如果你在 Reddit 侧发现本项目的行为不符合你的预期，欢迎开 issue。

## TODO

- [ ] 多语言报告（英文版同步生成）
- [ ] 历史趋势：同一主题持续多天出现时自动聚类
- [ ] 用 `r/subreddit/top.json?t=day` 补充热门帖（目前只拉 `/new.json`）
- [ ] 评分改为两阶段：先用便宜模型（Haiku/GPT-4o-mini）粗筛，再用 Sonnet 细评
