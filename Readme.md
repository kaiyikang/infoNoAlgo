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
- proxy 对外是 RSS endpoint，译文全文放在 item 的 content 里。Miniflux 拉取它和拉取任何 RSS 一样，不走 API。
- Miniflux REST API 只给 ranker 用（`X-Auth-Token` 鉴权）。

## 已做的设计决定

- 后台线程填缓存，endpoint 只读缓存。原因：Miniflux 抓取有超时，翻译一篇要几十秒（默认超时值未核实）。
- `MAX_NEW_PER_RUN` 限制每轮翻译数量，控制成本。
- ranker 的过滤方式选"写回 Miniflux"（标已读或归类），可逆。不在 proxy 端直接丢弃，避免越滤越窄。
- embedding 用外部 API，不在 Pi 上跑本地模型。
- `app` 容器不发布端口，Miniflux 经 compose 内网访问 `http://app:8090/pg.xml`。
- Miniflux 绑定 `127.0.0.1:8080`，再用 `tailscale serve` 暴露到 tailnet。
- 密钥只放 `.env`（已 gitignore），`app` 容器只传入必要变量。
- X 内容来源的计划：先按作者订阅（私密 X List），再考虑 embedding 过滤。

## 仓库结构与状态

```
infoNoAlgo/
├── docker-compose.yml   # db + miniflux + app，未在 Pi 上运行过
├── .env.example         # DB_PASSWORD, ADMIN_USER, ADMIN_PASSWORD,
│                        # OPENROUTER_API_KEY, TRANSLATE_MODEL, MINIFLUX_PORT(可选)
├── .gitignore           # 含 .env, *.db, data/
└── app/
    ├── pyproject.toml   # uv 依赖：fastapi uvicorn feedparser httpx
    │                    #   beautifulsoup4 feedgen
    ├── uv.lock
    ├── main.py          # PG 博客测试版 proxy，本地已跑通
    └── Dockerfile       # uv sync --frozen，只 COPY 需要的文件
```

## 当前测试用例

用 Paul Graham 的博客验证"补全文 + 翻译 + 重排"。
- 上游：社区抓取版 feed `https://paulgrahamessays.val.run/`，只有标题、链接、日期和短摘要。该 feed 是第三方维护，可能失效。备选是直接抓取 `https://paulgraham.com/articles.html`。
- `main.py` 提供 `/pg.xml` 和 `/health`。标题格式"中文 | English"。没有 `/` 路由，访问会 404。

## 已验证（2026-10-23，本地 macOS）

- 本地跑通：`powerful.html` 拆成 6 段，全部翻译成功，写入 `cache.db`，`/pg.xml` 能输出。
- 测试模型：`qwen/qwen3.7-flash`（付费，便宜）。请求里设了 `reasoning: {enabled: false}`：一句话的测试中，耗时从 18 秒降到 0.8 秒，推理 token 从 1516 降到 0。
- 免费模型（`:free`）不稳定：多个返回 429（上游限流）或 404（提供方端点失效），不适合用。
- 日志：每段打印 `translating <url> chunk i/n`，成功打印 `translated: <url>`。OpenRouter 返回任何 HTTP 错误都会打印响应体，并中止本轮，不再逐篇重试。
- `/pg.xml` 的 Content-Type 带 `charset=utf-8`，否则浏览器会把中文显示成乱码（RSS 阅读器不受影响）。

## 未验证事项（不要当作事实）

- `extract()` 按 `<br>` 拆段：只检查了一篇的开头，没逐段对照原文。
- Miniflux 较新版本可能默认拒绝抓取私有网络地址，出现时需查其配置文档。
- Miniflux API 的 starred 过滤参数名要以官方文档为准。
- Reeder 支持哪些同步后端，以用户当前版本为准。
- Postgres 和 Miniflux 镜像的 arm64 支持，依赖在 Pi 上能否装上。
- Pi 上 3000、9998 已被占用。本项目只发布 Miniflux 的 `127.0.0.1:${MINIFLUX_PORT:-8080}`，8080 是否空闲未确认。

## X 数据源（未实现）

- X 没有官方 RSS，Nitter 类免费方案基本已失效。
- 计划：自建，用付费读取 API（如 twitterapi.io 这类第三方，或官方 API 按量付费）拉取，再输出 RSS。价格来自厂商博客，需自行核实。
- 多账号用一次 `advanced_search`（`from:A OR from:B`）合并；增量用 `since_time`，省钱。
- 第三方抓取有被封的风险，不要当作永久方案。
- RSS 输出的坑：`<author>` 需邮箱格式，作者名放 `dc:creator`；`pubDate` 用 RFC 822；`guid` 用推文 URL。

## 下一步（按顺序）

1. ~~`uv lock`~~ 已完成。
2. ~~本地跑通~~ 已完成。命令（在 `app/` 下）：`DB_PATH=./cache.db TRANSLATE_MODEL=qwen/qwen3.7-flash MAX_NEW_PER_RUN=1 uv run uvicorn main:app --port 8090`（`OPENROUTER_API_KEY` 从 shell 环境读取）。
3. Pi 上先 `ss -ltn | grep 8080`、`uname -m`，再 `docker compose up -d --build`，Miniflux 订阅 `http://app:8090/pg.xml`。
4. Reeder 连 Miniflux，确认能读到译文。
5. 实现 X 来源 proxy（先 10 个左右必看账号，一个合并 feed）。
6. 实现 ranker：读加星条目，embedding 打分，写回 Miniflux。
7. 补 D2 架构图。之前 `arch.d2` 用本地 SVG 图标编译失败（`failed to bundle local images`），原因待查：先确认 `icons/` 文件存在、内容是有效 SVG、在 `.d2` 所在目录运行。

## 对 agent 的要求

- 用户偏好：简短、直接、短句，不要客套和废话。
- 不要索要或展示密钥；发现密钥泄露要提醒轮换。
- 优先最小权限、可回滚、可测试。大改动前先给简短计划。
- 没验证过的东西要明说，不要编造数字、价格或版本信息。