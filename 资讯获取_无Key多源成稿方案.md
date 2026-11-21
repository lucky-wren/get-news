# 资讯获取 · 不配 Serper Key 也能达到样例水准的实现说明

> 【批次记录】本文件记录 2026-11-21 之前某一次运行/某一轮修复的实测数字，**不代表当前交付包状态**；当前状态见 `output/full_audit.json`、`output/branding_verify.json` 与 `资讯获取_全面排查与调整报告_v2.md`。

> 结论先给：**Serper 不是必需品，只是更快的备选项。**
> 「34 条全部多渠道来源 / 两段摘要 / 9 个 A」那种成品，由「检索 → 独立原文 → 事件合并 → 成稿」这条链路决定，
> 不由某一把 API Key 决定。本包把这条链路用**全部免费、公开、无需签名**的入口重写了一遍，
> 并新增了自建开源元搜索（SearXNG）的可选加速通道。**不需要任何 Secret 即可跑。**

---

## 1. 为什么之前做不到（根因复盘）

| 项 | 旧值 | 样例需要 | 后果 |
|---|---:|---:|---|
| 整批检索上限 `search_api.max_calls` | 48 | 34 条 × 3~6 源 ≈ 120+ 次回查 | 预算差 4 倍 |
| 社交词条逐条回查 `social_main.max_lookup` | 24（更早是 16） | 30 条快照 → 14 条成稿 | 50+ 词条被挤进附录 15 |
| 正文补采 `max_body_fetch` | 32 篇 | 主表 ≈122 篇 / 候选池 ≈260 篇 | 名额被前 3 个源吃满 |
| 检索层顺序 | RSS 抓完 `raw` 之后当「补录」 | 词表 + 词条先造数据，RSS 作补充证据 | 主次倒置 |
| 产出模型 | 一个 URL = 一条 | 一个事件 = 一条 + N 个独立原文 | 来源列恒单源，A 档数学上恒空 |

模块其实一直都在（`search_api.py` / `social_main.py` / `quality_v3.py` / `cluster.py`），
**差的是预算、顺序和「按事件成稿」这一层**。本轮只补这三件事。

---

## 2. 别人怎么做的（公开资料核实）

| 层 | 代表 | 关键点 | 我们的取舍 |
|---|---|---|---|
| 发现 | NewsNow（MIT）、DailyHotApi（MIT）、RSSHub、60s | 只负责「现在什么在热」，给词条与 URL，不产正文 | 作线索来源，不作成品 |
| 检索 | **Google News RSS** | 免费无 key，单次查询约 99–100 条，带精确 `pubDate` 与 `<source>`；支持 `when:1d`、`site:`、精确短语；link 是 Google 重定向，2024 起不能 base64 解码，必须跟随重定向 | **主检索通道 1** |
| 检索 | **Bing RSS** (`format=rss`) | 免费无 key，带 `pubDate`；本沙盒实测 HTTP 200 / 10 条 | **主检索通道 2**（受限网络兜底） |
| 检索 | **GDELT DOC 2.0** | 免费无 key，65 语言，`maxrecords=250`、`timespan=24h`、`sort=datedesc`，返回 `url/title/seendate/domain`；服务端硬要求 ≥5 秒/请求，限流时返回纯文本而非 JSON | **主检索通道 3**；注意全文匹配噪声与通稿重复 |
| 检索 | **SearXNG**（开源自建） | 元搜索聚合 70+ 引擎，`/search?q=&format=json` 返回 `title/url/content/publishedDate/engine`，无 key 无配额；`settings.yml` 需开启 `formats: [html, json]`；常驻约 145MB 内存 | **可选通道 4**，自建后填 `SEARXNG_URL` |
| 正文 | trafilatura + **r.jina.ai** | Reader 无 key 20 RPM，把 URL 转纯文本 | 抽不到正文时兜底 |
| 事件 | Google News story cluster / Event Registry / AIHOT | 同一现实事件折叠成一条；热度按事件算不按文章算、每个独立来源只算一次、≥2 参与方入围 | `keyless_compose.compose_event` |
| 成稿 | 编辑流水线（时间窗口先于摘要、三层去重、发布前回指原文） | 数字冲突显式并列、失败逐条留痕、自动化不发布未经核实内容 | 纯规则实现，不调模型 |

明确**不采用**：
- `s.jina.ai` 搜索端点：Jina 官方最新速率表把「无 API key」标为 **block**，不能作为无 key 方案依赖；
- Mwmbl / Marginalia / YaCy：AGPL 开源可自建，但索引偏非商业站点与历史页面，不适合「当日中文新闻」；
- 付费 News API（GNews / NewsAPI / Perigon 等）：有免费额度但延迟与配额都不如上述通道，且仍需注册。

---

## 3. 这轮自己写的代码

### `news_pipeline/keyless_compose.py`（新增，666 行）

纯标准库 + 项目既有 `article.fetch_article_text` / `timewindow`，**无新增依赖、无模型调用**。

1. **四个 provider 统一返回 `Evidence`**：`gnews` / `bing_rss` / `gdelt` / `searxng`。
   每条带 URL、发布时间（统一转 `Asia/Shanghai`）、来源域、provider、时间依据。
2. **Google News 重定向解析**：跟随 `news.google.com/rss/articles/...` 拿到真实出版方 URL；
   解析失败保留原链接并标「正文用 Reader 兜底」，不伪造 URL。
3. **独立域名去重 + 权威排序**：同一域名只算一个证据（AIHOT 规则），官方 / 央媒优先选代表标题。
4. **正文补采**：逐条 `fetch_article_text`，失败走 `r.jina.ai`（无 key 20 RPM）；**不吃 snippet**。
5. **事件成稿**：≥2 个独立域名、至少 2 篇取到正文、时间落在窗口内才成稿；否则只留线索与原因。
6. **两段摘要**：整分句裁剪、净字 ≤100、数字一个不改写；第二段与首段 2-gram Jaccard >0.45 判复读并重选；
   跨源同单位数字不一致且两篇标题锚点交集 ≥2（确属同一事件）时，输出「甲报 X、乙报 Y，并列标注」，
   **不同事件的同单位数字不会被误判成冲突**。
7. **来源列 / 记录类型 / 事件主键**：`A · B · C`（最多 6 个）、`多渠道合并 / 联网检索独立原文 / 社交词条回查成稿`、
   `e#### / s####`；每篇证据的 URL、发布时间、provider、正文取得方式写进 `raw` 逐条留痕。

### 接入改动

| 文件 | 改动 |
|---|---|
| `config.yaml` | 新增 `keyless:` 段；`provider_order` 加 `keyless`；`max_calls` 48→300、`max_body_fetch` 32→300、`body_timeout` 5→9、`jina_max` 4→40、`clues_per_route` 5→12、`social_main.max_lookup` 24→**0（不限）** |
| `news_pipeline/search_api.py` | 新增 `keyless` provider 分支，无 key 时拉回多候选仍走原有打分 / 硬闸 |
| `news_pipeline/pipeline.py` | 新增 **0a-0 主链路**（主题词表 + 社交/热榜词条 → 无 key 成稿），排在 RSS 补录之前；候选池规范化时保护两段摘要不被单段抽取覆盖 |
| `news_pipeline/social_main.py` | `max_lookup=0` 表示逐条回查不限额 |
| `.github/workflows/daily-news.yml` | 采集步 `timeout-minutes: 35`（RSS 实测约 16 分钟 + 无 key 回查 7 分钟 + 正文补采）；新增「无 Key 检索通道实测」step 逐源打印状态；可选透传 `SEARXNG_URL` |
| `scripts/verify_keyless.py` | 21 项可复跑验收（配置 / 解析 / 成稿形态 / 冲突防误判 / provider 接入 / 真实探测） |
| `scripts/full_audit.py` | 把无 key 链路验收列为第 ⑨ 硬项 |

---

## 4. 怎么跑（零 Secret）

```bash
# 本地
python run.py --source live --start "2026-11-18 20:00" --end "2026-11-19 20:00" \
  --interval lo-ro --json-summary --spec-verify

# 只验链路（不联网也能跑，真实探测失败如实记录）
python scripts/verify_keyless.py --json output/keyless_verify.json
python scripts/full_audit.py
```

线上：**双击 `deploy.bat` 粘 Token 推包**，什么都不用配。20:00 的 Actions 自动跑；
日志里会多出「无 Key 检索通道实测」一步，逐个打印 Google News RSS / Bing RSS / GDELT / SearXNG 的状态码与条数。

### 可选：自建 SearXNG（想再快一点才做）

```bash
git clone https://github.com/searxng/searxng-docker.git && cd searxng-docker
# 编辑 searxng/settings.yml，加入：
#   search:
#     formats:
#       - html
#       - json
docker compose up -d
curl -s 'http://127.0.0.1:8080/search?q=医保&format=json' | head
```
把公网地址填到仓库 **Settings → Secrets → `SEARXNG_URL`**（或 `config.yaml` 的 `keyless.searxng_url`）。
不填也完全能跑：其余三个通道已覆盖。公共 SearXNG 实例（`searx.space` 列表）流量大、易被上游限流，
**不建议当生产依赖**。

---

## 5. 实测证据（本轮）

| 项 | 结果 |
|---|---|
| `verify_keyless.py` | **21/21 PASS**（含两段不复读、跨事件冲突防误判、独立源不足不成稿、provider 接入） |
| `full_audit.py` | **21/21 硬项 PASS**（把无 key 链路列为第 ⑨ 项后复跑） |
| 事件成稿样例 | 3 个独立域名 → 来源列 3 个、两段摘要、净字 77、`e0007`、记录类型「多渠道合并」、发布时间落窗口内 |
| 沙盒真实探测 | `bing_rss` **HTTP 200 / 10 条**；`gnews` / `gdelt` **403**（域名级出口拦截，非代码问题） |
| Actions 参照 | 11-21 日志 step 10 里 NewsNow 公共实例 200/30 条、UAPI 200/20 条、60s 两个实例各 200/50 条，说明 runner 的无限制网络确实能启用沙盒放不通的源 |

---

## 6. 诚实边界（不粉饰）

1. **沙盒不能证明 runner 一定可达**：Google News RSS 与 GDELT 在这里是 403；推包后由新增的 CI 实测 step 逐源打印结果，
   可达就自动进主链路，不可达就如实留痕并继续用 Bing RSS + RSS/端点流，**不编数据**。
2. **GDELT 的坑**：≥5 秒/请求；匹配全文不一定匹配标题（会带出无关稿）；同一通稿在多家站点重复出现——
   已用「独立域名去重 + 窗口过滤 + 标题锚点」处理，并用全局锁 + 每批 20 次调用上限防止多线索并发撞限流；
   召回里仍可能有噪声，由既有 F5 / strict 贴近度再筛。
3. **Google News RSS 的坑**：link 是重定向；2024 年编码变更后必须跟随取真实 URL；
   RSS 是 Google 「碰巧提供」的 feed，不是有 SLA 的正式产品，格式可能变。
4. **SearXNG 的坑**：`format=json` 必须在 settings.yml 显式开启；上游引擎会因风控陆续失效，
   脚本按「结果条数下限」兜底而不是假设每次拿满。
5. **无 key 比有 key 慢**：逐条回查 + 正文补采并发跑，采集步曾一度放宽到 35 分钟并触发超时事故；
   现已改为 workflow 15 分钟硬杀 + 脚本 600 秒全局预算（到点短路采集层但必定出表），
   无 key 成稿层自身有 7 分钟总闸；Serper 2500 次免费额度仍能提速，但**配不配都不影响能不能出这种成品**。
6. 社交平台词条仍必须回查到带事实的独立原文才成行；抖音作品 / 小红书笔记 / 公众号历史文章
   仍需授权或分享预览页链接，换网络也变不出来。
7. `quality.warn_only` 仍是 `true`（未拍板项，本轮没动）；本轮没有重跑 live 批次，
   线上 `latest.xlsx` 要等推包后 20:00 那批才会变成新链路产物。

---

## 7. 一句话

**不用加后端、不用换架构、不用 LLM、不用人工、不用 Serper Key。**
要做的只有一件：把这个包推到 `main`，让 20:00 的批处理按「线索 → 多源独立原文 → 事件成稿」重新跑一遍。
Key 是加速器，不是入场券。
