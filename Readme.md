# infoNoAlgo


## 目标

用 Reeder 主动阅读自己选的内容，而不是 X 的推荐算法。
用户的使用方式：在 X 推荐流里偶然看到好内容，希望之后只读到类似的内容。

## 环境约束

- 部署在 Raspberry Pi 4（内存大小未确认，用户记得是 4 代），arm64
- 网络用 Tailscale，不暴露公网
- Pi 上已有两个 Docker 服务，本项目是第三个，必须独立 compose 项目，避免端口冲突
- Python 用 uv 管理
- LLM 调用走 OpenRouter（不是 Anthropic 官方 API）
- 架构图用 D2

## 架构

```
上游(X 读取 API / 博客 RSS)
  → feed proxy (FastAPI): 抓正文、翻译、重排、SQLite 缓存、输出 RSS
  → Miniflux (+ Postgres): 定时 HTTP 拉取 proxy 的 RSS，存库
  → Reeder: 通过 Miniflux 的同步接口读取（走 Tailscale）
taste ranker (未实现): 通过 Miniflux REST API 读取加星条目，
  用 embedding 相似度给新条目打分，回写 Miniflux
```

关键事实：
- RSS 是单向拉取，proxy 看不到 Reeder 的状态。已读、加星只存在于 Miniflux，所以 Reeder 必须连 Miniflux，不能直接订阅 proxy。
- proxy 对外是 RSS endpoint，双语全文（每段原文后接译文）放在 item 的 content 里。Miniflux 拉取它和拉取任何 RSS 一样，不走 API。
- Miniflux REST API 只给 ranker 用（`X-Auth-Token` 鉴权）。

## 已做的设计决定

- 后台线程填缓存，endpoint 只读缓存。原因：Miniflux 抓取有超时，翻译一篇要几十秒（默认超时值未核实）。
- `MAX_NEW_PER_RUN` 限制每个博客每轮的翻译篇数，控制成本。X 用 `MAX_PAGES=5`（每页最多 20 条）限制。
- 来源模块化：`main.py` 只管后台循环和挂路由，每个 `sources/*.py` 提供 `refresh()` 和 `router`，加入 `SOURCES` 列表即可。共享的缓存和翻译放 `common.py`（子模块 import `main` 会循环引用）。
- 普通博客不单独写文件，加到 `blog.py` 的 `FEEDS` 表：`"feed"` 模式直接用 RSS 自带全文，`"page"` 模式抓原网页（`extract()` 只适配 PG 的 HTML）。
- 双语对齐：按段落一一配对；模型合并或拆分段落导致段数对不上时，该块退回整块原文 + 整块译文。
- OpenRouter 返回 HTTP 错误时中止整轮（所有博客），下轮再试。一个来源抛异常不影响其他来源。
- ranker 的过滤方式选"写回 Miniflux"（标已读或归类），可逆。不在 proxy 端直接丢弃，避免越滤越窄。
- embedding 用外部 API，不在 Pi 上跑本地模型。
- `app` 容器不发布端口，Miniflux 经 compose 内网访问 `http://app:8090/...`。
- Miniflux 绑定 `127.0.0.1:8080`，再用 `tailscale serve` 暴露到 tailnet。
- 密钥只放 `.env`（已 gitignore），`app` 容器只传入必要变量。
- X 内容来源的计划：先按作者订阅（私密 X List），再考虑 embedding 过滤。

## 仓库结构与状态

```
infoNoAlgo/
├── docker-compose.yml   # db + miniflux + app，未在 Pi 上运行过
├── .env.example         # DB_PASSWORD, ADMIN_USER, ADMIN_PASSWORD,
│                        # OPENROUTER_API_KEY, TRANSLATE_MODEL, X_API_KEY, X_QUERY,
│                        # MINIFLUX_PORT(可选)
├── .gitignore           # 含 .env, *.db, data/
└── app/
    ├── pyproject.toml   # uv 依赖：fastapi uvicorn feedparser httpx
    │                    #   beautifulsoup4 feedgen
    ├── uv.lock
    ├── main.py          # 后台循环、挂载各来源路由、/health
    ├── common.py        # conn()、translate()，各来源共用
    ├── sources/
    │   ├── blog.py      # 博客 RSS：FEEDS 表（pg, newport, koe），/blog/{slug}.xml，/pg.xml 为别名
    │   └── x.py         # X via twitterapi.io，/x.xml，X_QUERY 为空时不运行
    └── Dockerfile       # uv sync --frozen，只 COPY 需要的文件
```

## 当前来源

| 路由 | 来源 | 正文 |
|---|---|---|
| `/blog/pg.xml`（别名 `/pg.xml`） | Paul Graham，社区 feed `https://paulgrahamessays.val.run/`（第三方维护，可能失效；备选直接抓 `https://paulgraham.com/articles.html`） | `page`：feed 只有摘要，抓原网页 |
| `/blog/newport.xml` | Cal Newport，`https://calnewport.com/feed/` | `feed`：RSS 自带全文，去掉 WordPress 的 "The post … appeared first on …" 尾注 |
| `/blog/koe.xml` | Dan Koe，Substack「future/proof」`https://letters.thedankoe.com/feed`（`thedankoe.com/feed/`、`/letters/feed/` 已分别于 2024-07、2025-08 停更，不用） | `feed`：RSS 自带全文，无需过滤 |
| `/x.xml` | X，twitterapi.io `advanced_search`，查询 `(X_QUERY) since_time:<最新已存推文时间>` | 只翻译 `lang == "en"` 的推文，原文在上、译文在下；作者放 `dc:creator` |

- 博客标题格式"English | 中文"，正文每段原文后接译文。
- 另有 `/health`。没有 `/` 路由，访问会 404。

## 已验证（2026-10-23，本地 macOS）

- 本地跑通：`powerful.html` 拆成 6 段，全部翻译成功，写入 `cache.db`，`/pg.xml` 能输出。
- 测试模型：`qwen/qwen3.7-flash`（付费，便宜）。请求里设了 `reasoning: {enabled: false}`：一句话的测试中，耗时从 18 秒降到 0.8 秒，推理 token 从 1516 降到 0。
- 免费模型（`:free`）不稳定：多个返回 429（上游限流）或 404（提供方端点失效），不适合用。
- 日志：每段打印 `translating <url> chunk i/n`，成功打印 `translated: <url>`。OpenRouter 返回任何 HTTP 错误都会打印响应体，并中止本轮，不再逐篇重试。
- 双语配对：Newport 一块真实正文 13 段 → 13 对，未触发退回。
- 模块拆分后：各模块自检（`uv run python -m sources.blog` / `sources.x`）通过；`/pg.xml`、`/blog/pg.xml`、`/blog/newport.xml` 返回 200，未知 slug 返回 404；`items` 表加 `source` 列的迁移在 `cache.db` 副本上验证过。
- RSS 的 Content-Type 带 `charset=utf-8`，否则浏览器会把中文显示成乱码（RSS 阅读器不受影响）。

## 未验证事项（不要当作事实）

- `extract()` 按 `<br>` 拆段：只检查了一篇的开头，没逐段对照原文。
- Newport 整篇翻译没跑过（只测了一块）。
- `cache.db` 里旧的 2 篇 PG 是旧格式（只有译文、标题中文在前），要新格式需删掉重翻。
- X 模块没调过真实 API：查询里的括号分组 twitterapi.io 是否支持未确认。价格来自官方 pricing 页（$0.15/1k 条，每请求最低 $0.00015），需自行核实。
- Miniflux 较新版本可能默认拒绝抓取私有网络地址，出现时需查其配置文档。
- Miniflux API 的 starred 过滤参数名要以官方文档为准。
- Reeder 支持哪些同步后端，以用户当前版本为准。
- Postgres 和 Miniflux 镜像的 arm64 支持，依赖在 Pi 上能否装上。
- Pi 上 3000、9998 已被占用。本项目只发布 Miniflux 的 `127.0.0.1:${MINIFLUX_PORT:-8080}`，8080 是否空闲未确认。

## X 数据源（已实现基础版，未联调）

- X 没有官方 RSS，Nitter 类免费方案基本已失效。
- 用 twitterapi.io：`GET https://api.twitterapi.io/twitter/tweet/advanced_search`，鉴权头 `X-API-Key`，参数 `query`、`queryType=Latest`、`cursor`；返回 `tweets`、`has_next_page`、`next_cursor`。时间过滤用 `since_time:<unix 秒>`（文档说 `since:日期` 不支持）。
- 账号来源：现在用 `X_QUERY`（如 `from:a OR from:b`），私密 X List 之后再接。
- 多账号用一次 `advanced_search`（`from:A OR from:B`）合并；增量用 `since_time`，省钱。
- 第三方抓取有被封的风险，不要当作永久方案。
- RSS 输出的坑：`<author>` 需邮箱格式，作者名放 `dc:creator`；`pubDate` 用 RFC 822；`guid` 用推文 URL。

## 下一步（按顺序）

1. ~~`uv lock`~~ 已完成。
2. ~~本地跑通~~ 已完成。命令（在 `app/` 下）：`DB_PATH=./cache.db TRANSLATE_MODEL=qwen/qwen3.7-flash MAX_NEW_PER_RUN=1 uv run uvicorn main:app --port 8090`（`OPENROUTER_API_KEY` 从 shell 环境读取）。
3. Pi 上先 `ss -ltn | grep 8080`、`uname -m`，再 `docker compose up -d --build`，Miniflux 订阅 `http://app:8090/blog/pg.xml`、`/blog/newport.xml`、`/blog/koe.xml`、`/x.xml`。
4. Reeder 连 Miniflux，确认能读到译文。
5. ~~X 来源 proxy~~ 基础版已写。待做：填 `X_API_KEY`、`X_QUERY`，先一个账号联调；之后接私密 List（约 10 个必看账号）。
6. 实现 ranker：读加星条目，embedding 打分，写回 Miniflux。
7. 补 D2 架构图。之前 `arch.d2` 用本地 SVG 图标编译失败（`failed to bundle local images`），原因待查：先确认 `icons/` 文件存在、内容是有效 SVG、在 `.d2` 所在目录运行。

## 对 agent 的要求

- 用户偏好：简短、直接、短句，不要客套和废话。
- 不要索要或展示密钥；发现密钥泄露要提醒轮换。
- 优先最小权限、可回滚、可测试。大改动前先给简短计划。
- 没验证过的东西要明说，不要编造数字、价格或版本信息。