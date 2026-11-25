# newsbrief · 每日 20:00 大众向要闻简报（轻量版）

每天跑一次，出一份 **5 工作表 / 12 列** 的 `daily_news_YYYYMMDD_20h.xlsx`。无常驻进程、无数据库、无 Web 框架、无前端。

## 架构借鉴

| 项目 | 取了什么 | 落地 |
|---|---|---|
| **NewsNow** | `sources.json` 单一真值源 + 每源一模块 + glob 自动注册 + 抓取失败回吐旧缓存 | `sources.json` / `sources/` / `registry.py` |
| **DailyHotApi** | 目录扫描、6 秒短超时、单源失败只空自己那张卡片 | `registry.discover()` / `TIMEOUT` / `failed` |
| **Horizon** | fetch→clean→score→sheet 阶段流水线 | `pipeline.py` |
| **Fusion / Miniflux** | 砍鉴权、插件、Redis、独立 DB、前端；单进程单数据文件 | 全包无服务端代码 |
| **feedparser / trafilatura / httpx** | ETag 条件请求 + bozo 探针 / 正文与 date 抽取 / `MockTransport` 零网络测试 | `sources/rss.py` / `html.py` / `tests/` |

否决 RSSHub：31 类源不需要构建期路由生成；Excel 段自写 71 行。

## 目录

`sources.json`（31 类白名单唯一真值）、`config.json`、`data/feed.json.gz`（11-21 定稿语料）、`newsbrief/`（8 模块 + 3 采集器）、`tests/`、`deploy.py`/`deploy.bat`、`daily.yml`。加信源 = 配置加一行 + 形态特殊时加一个 `@source` 文件，核心零改动。

依赖锁版本：`feedparser` / `httpx` / `openpyxl`；`trafilatura` 可选；`pytest` 仅开发期。

## 运行

```bash
pip install -e ".[dev,extract]"
pytest -q                              # 零网络自检，必须先过
python -m newsbrief --mode offline      # 回放 11-21 定稿语料，逐格可复算
python -m newsbrief --mode live        # 按 sources.json 真联网采集
```

顺序：流水线 → 出表 → **18 项规格验收** → 通过后覆盖 `docs/downloads/latest.xlsx`。形态硬闸 FAIL 返回 1 且不覆盖；条数少属产能不足（照常出表并标注），编排类不足只标注不判红。工程包内不预置 xlsx（产物约 90 KB），解压后跑一次 `--mode offline` 即生成。

## 一键部署（只需 Token，免登录）

双击 `deploy.bat`，**只粘贴一次 GitHub Token**（classic PAT 勾 `repo`）回车即可。脚本用 Token 调 `GET /user` 自动认出 owner，项目名固定 `get-news`；仓库不存在就用同一个 Token 自动创建，不用登录网页、不用填仓库名、不用配置 git 账号。全程不弹凭据窗口（`credential.helper=` / `GIT_TERMINAL_PROMPT=0`），Token 只走 HTTP 头，不写 remote、不落 `.git/config`、不打印。

连那一次粘贴也可省：把 Token 单独写进同目录 `token.txt`，或先 `set GITHUB_TOKEN=...`，双击后零交互完成。`token.txt` 已进 `.gitignore`，永不入仓。推完到仓库页 **Actions → Run workflow** 跑一次，之后每天 20:00 自动出表。

重复部署会自动清掉仓库里旧版残留文件（旧测试、旧模块等），同时保留 `docs/downloads/latest.xlsx` 固定下载地址、页面文件与远端提交历史；不用手动删文件、不用登录网页、不产生空提交。推完会**自动触发一次 workflow**，约 1–3 分钟后页面与 Excel 一起更新；若 Token 缺 workflow 权限会明确提示去 Actions 点一次 Run workflow。

## 页面与时间口径

`docs/index.html` + `docs/news-data.js` 由 `newsbrief.site` 从**同一批 Batch** 现算，规格验收通过后才写入；页面与 `docs/downloads/latest.xlsx` 是同一批对象的两个视图，生成时刻与采集窗口都由本批现算，绝不沿用历史构建戳。验收不达标时页面字节不变、保留上一批。

`--date` 不传时：live 取 Asia/Shanghai 当天（每天 20:00 的窗口即「昨 20:00 → 今 20:00」），offline 固定回放 11-21 定稿语料以逐格复算；也可显式指定。workflow `cron: 0 12 * * *` = 北京时间 20:00，跑完 `git add docs/` 提交页面与 xlsx，并由官方 Pages 三步发布。

## 数据质量

离线产物与提供的全渠道定稿**逐格 0 差异**：5 表（54×12 / 11×12 / 130×12 / 250×5 / 54×15）、摘要 2 段/LF/全角缩进/≤100 字/整句、`渠道 · 渠道`、`Final_adj = Final − 4.2`、等级按原始 Final、候选池评级列全 `—`、附录 1/3/4/15/16/17 齐备。

`--mode offline` 是**回放既有定稿语料**，按文档 §11 不声明当日抓取；`--mode live` 才发真实请求。live 链路含：短摘要逐条回查独立原文正文（限量、失败隔离）、整页导航/菜单拼盘在构造期拒绝、栏目页逐篇抓文章页、结构化源只组合真实字段、8 大类分类与前 10 打散、单源失败回吐旧缓存并逐类申报缺口。

真外网：追加 4 个当天新鲜的中新网 RSS，gov.cn 政策页换成真有正文的栏目页。最终真跑（39 端点）候选池 101 / 主表 12 / 口径表 254 行、18/18 达标；16 个端点 403/412 不可达，逐类申报。版式：数据逐格、全量样式、LibreOffice 42 页渲染均 0 差异；3.10/3.11/3.12 三版本全套通过；零缓存全不可达 0 编造、仍出 5 表与 31 类申报。

## 边界

- 会话内补跑 ≠ 调度器执行回执；top-N 不构成按窗口时间倒排的全量枚举，真全量需服务端采集器与检索配额。
- 微信搜一搜无官方免鉴权 API；GDELT `seendate` 不是发布时间；Bing News API 已退役。
- 无搜索 key / 端点不可达时如实申报「未取得当日明细」，产物是采集子集，不伪装全渠道全量。
- 按仲裁顺序，最高优先级是 `01_定时任务_instruction_现行全文.md`（8 个上位大类、附录 1–17、表 R 八组）；本轻量版主要实现「31 类白名单 + 5 表 12 列」那套口径，未实现议题簇去重、表 R 八组、H 通道、社交四时点快照等条款。
