# get-news · 资讯获取 · 配置驱动 + 覆盖式固定站点

> **当前状态（交付前必读）**：项目 `get-news`（仓库 `lucky-wren/get-news`），产品显示名 **资讯获取**（同音别名「咨询获取」），
> 📐 **目标样例形态对齐（2026-11-22）见 [`资讯获取_目标样例形态对齐与事件成稿层.md`](资讯获取_目标样例形态对齐与事件成稿层.md)**：新增事件成稿层把候选池从 URL 级改为事件级；`python scripts/verify_golden.py` 可复现两份五工作表 xlsx 并逐格对比目标契约。
> 单点配置 `config.yaml → product`；每天 **20:00** / 窗口 `(前一日20:00, 当日20:00)` **左开右开**；
> 交付核查以 `output/full_audit.json` + `output/branding_verify.json` 为准，一键部署双击 `deploy.bat`。
> 下方「根因 / 复核 / 诊断」类文档均为**历史批次记录**，其条数、A 档数、门禁数字不代表当前包状态。
> ⏱ **采集步 35 分钟超时事故修复与耗时优化见 [`资讯获取_超时事故修复与耗时优化说明.md`](资讯获取_超时事故修复与耗时优化说明.md)**：前置 6 步串行 49min → 一次并行预探测 150s，采集步 35→15min，脚本自带 600s 全局预算，超时短路采集层但必定出表。

> 🚀 **最省事的上线方式：双击 [`deploy.bat`](deploy.bat)（优先调 [`deploy.py`](deploy.py)，无 Python 时回退纯 ASCII 的 `deploy.ps1`），只粘贴一个 GitHub Token，不用 gh、不用登录。说明见 [`资讯获取_一键部署说明.md`](资讯获取_一键部署说明.md)**。它会自动把 `.github/` 隐藏目录一起推上去并读回远端校验，根治「以为推了其实 workflow 还是旧的」。
>
> 🔥 **「一条 A 都没有 / 质量太差」根因、行业做法对照与质量 v3 修复见 [`资讯获取_质量v3根因与行业做法.md`](资讯获取_质量v3根因与行业做法.md)**；`python scripts/verify_quality_v3.py` 可复跑（含离线回放验证 S+A>0）。
>
> 🔗 **「非独立站点 / DNS 未解析 是不是解决不了」实测纠错与逐条通路见 [`资讯获取_两类渠道可解性纠错与实现.md`](资讯获取_两类渠道可解性纠错与实现.md)**；`python -m news_pipeline.channel_resolve --probe` 可复跑。
>
> 🔍 **「一条 A 都没有 / 很多渠道取不到」逐格根因与修复见 [`资讯获取_A档恒空与渠道口径根因.md`](资讯获取_A档恒空与渠道口径根因.md)**；两路离线回放脚本 `scripts/replay_quality.py` 可直接复算。
>
> 📘 **要部署 / 要改配置，直接看 [`资讯获取_配置部署说明.md`](资讯获取_配置部署说明.md)**；要看「开源仓库方案 + 搜索 API 怎么保证数据质量」，看 [`资讯获取_高质量数据链路说明.md`](资讯获取_高质量数据链路说明.md)；要查「31 类渠道怎么只有这么点数据、卡在哪」，看 [`资讯获取_31类渠道漏斗诊断.md`](资讯获取_31类渠道漏斗诊断.md)；批次质量逐项根因看 [`资讯获取_批次质量根因审计.md`](资讯获取_批次质量根因审计.md)；渠道扩容、低质渠道淘汰与 A 档评级修复看 [`资讯获取_渠道扩容与质量提升方案.md`](资讯获取_渠道扩容与质量提升方案.md)；Actions 报 `TypeError: 'int' object is not subscriptable` 看 [`资讯获取_CI崩溃根因与修复.md`](资讯获取_CI崩溃根因与修复.md)；小红书 / 微信 / 抖音无签名取数看 [`资讯获取_受限平台替代方案.md`](资讯获取_受限平台替代方案.md)；GitHub Actions 批次复核与「重跑零配置」改造看 [`资讯获取_Actions批次复核与修复.md`](资讯获取_Actions批次复核与修复.md)：环境依赖、仓库结构、`config.yaml` 逐段、GitHub Pages 一次性配置、定时与 UTC 换算、Secrets 全表、手动补跑、日志排错、上线验收清单、常见场景命令，全部照着做即可。

> **内容摘要（2026-11-20 补充，形态对齐目标样例）**：净字数 ≤100、**两段**（抽到 ≥2 个整句时按句边界分段，只有一句保持单段）、流畅说清楚、整句收束、数字不改写；纯代码抽取式，覆盖 SKILL §8 的 2–3 段 / 240 字。验收与门禁见 `news_pipeline/normalize.py` / `spec.py` / `quality.py`。
>
> **本版按需求重述①–⑤ + 2026-11-20 口径拍板重新对齐**：每天 **20:00**（Asia/Shanghai）出一份大众向要闻 Excel；窗口 `(前一日 20:00, 当日 20:00)` **左开右开**（用户拍板，页面 / `--interval` 四档仍可切）；主白名单 **31 类 599 渠道**（`data/资讯渠道大全_31类扩展版.md`，v3 全行业分类版仅作扩展参考）；10 个社交平台登记为**主干**（按需求②，v3 文档 §14「默认不入池」不作为判据）；5 类快讯/拼盘源全量拆条逐条回查；≥7 级地震 / 死伤多事故 / 多渠道高覆盖**强制第 1**；破产倒闭主体核验；影响面四维只决定顺序、不动评级数值；评分 `C` 按 **§4.4**、附录按 **01** 用「1–17 + 工程留痕」；**页面运行日志区保留**（默认折叠、展开才读取）；**单个 xlsx**（`output/` 只留本批一份，`docs/downloads/latest.xlsx` 是它的字节副本）。逐条落地与机械验收见 `资讯获取_需求说明书.md`。


一个仓库、一份配置、一个固定页面地址。每天到点由 GitHub Actions 按窗口期抓取资讯、筛选评级、生成五工作表 Excel，**覆盖仓库里同一份 `latest.json` / `latest.xlsx`**，页面地址永不变。

```
config.yaml（单点：几点跑 + 窗口期）
        │
        ▼
.github/workflows/daily-news.yml   cron（UTC 12:00 = 北京 20:00）
        │  python run.py  →  采集 → 清洗/正文补采 → 窗口过滤 → F5噪声 → 准入
        │                   → 聚类 → 议题收敛 → 六维 V/H打分 → 编排 → 12 项质量门禁 → 五工作表
        │  python build_site.py --overwrite
        ▼
docs/data/latest.json ──┐
docs/downloads/latest.xlsx ─┼──► https://<owner>.github.io/<repo>/  （固定地址）
docs/assets/news-data.js ──┘        └─ 窗口设置 / 重跑 / 下载 / 数据表
```

---

## 0. 纯代码、规格仲裁与开源组件（先读这一节）

**这套实现不调用任何大语言模型 / AI 语义服务。** 采集、准入、打分、编排、出表、验收全部由 Python 代码执行；摘要是抽取式（按整句裁剪），不是生成式；大众贴近度词组表、`Final_adj`、编排分 `U`、聚类阈值全部是可逐条复算的规则。

**链路身份**：本仓库是工作流包 `00_工作流总览.md` §2 里的**链路③（仓库侧纯代码采集）**，不是定时任务调度器回执、不是会话内 AI 补跑。因此保留 `latest.json` / 固定页面地址等工程产物，但口径表明确声明身份，不冒充定时任务产物；不写 `status=degraded` 冒充会话内批次。

**仲裁规则**（README 包内）：`01_instruction > 02/03 > 05`，`04` 仅作变更留痕不作运行规格。据此对齐：

- 附录 **1–17 + 候选池边界留痕**，**不编第 18 号附录**（`00` §9）；仓库侧附加留痕改名「工程留痕 A–F」，不占用 instruction 编号。
- `strict / moderate / off` 三档，无自造档；`story_max_keep` 默认 **1**；λ=6；五项质量惩罚 + 三态核实全部由代码实现。
- 交付文件名：**单个 xlsx**。`run.py` 产出 `output/{YYYY_MM_DD}_窗口期.xlsx` 并自动清理往期、覆盖 `docs/downloads/latest.xlsx`（字节一致）；`--deliver` 不再产生第二份副本。改交付名用 `--name`。

**开源组件**（`requirements.txt`，全部本地运行、无云端模型；逐项登记见 Excel 口径表）：

| 环节 | 库 | 说明 |
|---|---|---|
| Feed 解析 | `feedparser` | RSS 0.9x/1.0/2.0 + Atom，替代手写 `ElementTree` |
| 正文抽取 | `trafilatura` + `lxml_html_clean` | 独立原文正文 / 元数据，失败回退 BeautifulSoup |
| 时间归一化 | `dateparser` + `cn2an` | 只补绝对时间；相对时间串按规格不换算 |
| 相似度诊断 | `rapidfuzz` + `jieba` | LCSseq / fuzz / 词级 2-gram；判定式仍自写 |
| 抓取重试 | `httpx` + `tenacity` | 429/503/超时退避，4xx 立即失败 |
| HTML 清洗 | `bleach` / `bs4` / `lxml` | 允许清单与降级 DOM |
| 配置校验 | `pydantic` + `PyYAML` | 档位三档 / λ / story_max_keep 启动校验 |
| 出表 | `openpyxl`（大文件评估 `XlsxWriter`） | 五工作表 / 12 列 / `wrap_text` |

实测版本差异已如实记录：`rapidfuzz 3.14.5` 的 `distance` **不含 Jaccard 模块**，且 `LCSseq` 是「最长公共子序列」而非规格要求的「最长公共子串」，因此 2-gram Jaccard 与 LCS 判据保留自写实现，开源库只做诊断与召回。业务判定不能交给库。

**纯代码验收**：

```bash
python run.py --window "2026-11-18 20:00~2026-11-19 20:00" --interval lo-ro --spec-verify
python scripts/verify_spec.py output/2026_11_19_窗口期.xlsx --json output/spec_verify.json
```

`news_pipeline/spec.py` 按 `01` 第六节 / `03` §5 逐条机械核对（12 列 / 五工作表 / 候选池契约 / `Final_adj` 复算 / 摘要契约 / 前窗打散 / 四项口径硬闸 / 附录编号 / 纯代码声明），不靠人工肉眼判断。

---

## 1. 四个需求，落地在哪

| 需求 | 落地方式 |
|---|---|
| 可设置定时任务（几点跑） | `config.yaml → schedule.run_at`；`python scripts/set_schedule.py 06:30` 一键同步 UTC cron |
| 窗口期（资讯时间） | `config.yaml → window.start_offset_hours / end_offset_hours`，以运行时刻为锚；四种端点开闭；`--anchor` 复现历史批次 |
| 固定页面地址 | 覆盖式写入，只有一份 `latest.json` / `latest.xlsx` / `news-data.js`；`index.html` 从不随批次改 |
| 生成 Excel、可导出下载 | `run.py` 产出五工作表；页面主按钮恒定指向 `./downloads/latest.xlsx` |

本地产物按「年月日 + 窗口期」命名（`output/2026_11_17_窗口期.xlsx`），仓库里覆盖为固定名 `latest.xlsx`，两者内容一致。

---

## 2. 配置（config.yaml）

```yaml
schedule:
  run_at: "20:00"              # 每天北京时间几点跑（01 instruction 参数）
  cron_utc: "0 12 * * *"       # UTC 12:00 = 北京 20:00，由 set_schedule.py 自动同步

window:
  mode: relative
  start_offset_hours: -24      # 左界 = 运行时刻 -24h → (前一日 20:00, 当日 20:00)
  end_offset_hours: 0          # 右界 = 运行时刻
  interval: lo-ro              # 左开右开 (a,b)，用户拍板默认；另有 lo-rc / lc-ro / lc-rc

source:
  mode: live                   # 默认：真实联网多源抓取，无需贴台账 / key / 端点
  target:                      # 离线回归用：内置标准语料重放（--source target）
    seed: data/target_seed.json
    auto_live: true
    estimated_ratio: 0.18
  live:
    rss: true
    apis: true
    html_lists: true
    fetch_article: true        # RSS 只给标题复读时，从独立原文 URL 补正文
    max_articles: 120          # 单次正文补采上限（并发 8）
    probe_timeout: 3.0

rules:
  story_max_keep: 2            # 同一议题保留 2 条，避免「退市潮」刷屏

output:
  overwrite: true
  latest_name: "latest.xlsx"
  keep_history: false

quality:
  min_abstract_ratio: 0.80
  min_group_consistency: 0.98
  min_endpoint_ratio: 0.50
  max_noise_ratio: 0.10
  max_story_dup_ratio: 0.30
  warn_only: true              # 上线建议改 false，让不达标批次直接失败
```

**窗口为什么用相对偏移**：定时任务实际触发有分钟级抖动，写死日期会在跨天、补跑、延迟时错位；以运行时刻为锚，`--window 24` 就是「最近 24 小时的资讯时间」，每天 20:00 触发时恰好是 `(前一日 20:00, 当日 20:00]`。复现历史批次用 `--anchor "2026-11-19 20:00"`，或把 `mode` 切成 `absolute`。

```bash
python scripts/set_schedule.py 20:00 --window 24 --interval lo-ro
python scripts/set_schedule.py --show
python scripts/set_schedule.py --disable       # 暂停定时（保留手动触发）
```

> GitHub Actions 的 `on.schedule.cron` 必须是静态字面量，运行时不能读 YAML 生成；`set_schedule.py` 就是「只改一处、自动同步」的入口。

---

## 3. 页面（极简）

部署后固定地址 `https://<owner>.github.io/<repo>/`，页面只有四块：

1. **窗口期设置**：开始 / 结束 / 端点开闭 + 「重跑并刷新」+「下载 Excel」两个按钮；下面一行是「每天几点跑」与「复制改定时命令」。
2. **本期数据**：一行统计（主表 / 候选池）、一行质量门禁结论、一张朴素表格（序 / 标题 / 来源 / 大类 / 分类 / 等级 / 摘要状态）。
3. **运行日志**（默认折叠，用户拍板保留）：最近 12 次运行状态 / 窗口 / 条数 / 耗时 / 错误摘要，点选看合并全文，可复制、下载 `.log`；重跑成功自动刷新，**失败自动展开并定位错误尾部**。展开才发请求，静态 Pages 下如实提示去 Actions 运行页。
4. **首次设置**（默认折叠）：owner / repo / 分支 / Token，**只有点重跑才需要**；不填也能看数据和下载。
5. 页脚一行说明。

已删除：搜索、筛选、图表、主题切换、归档切换、窗口预览切片、质量卡片网格、分享链接、本地服务地址、多后端徽章。默认零网络探测、零令牌。

「重跑并刷新」的行为：

- 同源有本地服务（`python serve.py`）→ 直接 `POST /api/run`，跑完刷新 `data/latest.json`；
- 静态 Pages + 填了 Token → 触发 `workflow_dispatch`，轮询 run 至完成，自动刷新数据；
- 静态 Pages + 没填 Token → 复制一条 `gh workflow run ...` 命令，粘贴到终端执行；**不会假装跑成功**。

改定时用页面里的「复制改定时命令」，生成 `python scripts/set_schedule.py HH:MM --window N`；静态页面无法直接改仓库 cron，这一点如实说明。

---

## 4. 部署（一次性）

1. 把仓库内容推到 GitHub（默认分支 `main`）。
2. Settings → Pages → Source 选 **GitHub Actions**。
3. Settings → Actions → General → Workflow permissions 选 **Read and write**（覆盖提交需要）。
4. 可选：Secrets 加 `SERPER_API_KEY` / `TAVILY_API_KEY` 用于热榜词条回查。
5. 默认 `source.mode: live` 开箱即真实联网抓取（受限网络下按可达性自适应，逐源申报）；要补热榜/国际源见第 6、11 节。

每次运行（含失败）上传 `news-run-logs-<run_id>` artifact 保留 7 天，含 `ci-run.log`、`ci-summary.txt`、`*.pipeline.log`、`runs.jsonl`。

---

## 5. 流水线、Excel 与日志

`run.py` 阶段：采集与渠道枚举 → 字段规范化（HTML 清洗 / 正文补采 / 时间回填）→ 窗口过滤 → **F5 非新闻过滤** → 准入（大类级 / 贴近度 / 国际三类 / F1–F4）→ 同事件聚类 → 六维 V/H打分 → **议题簇收敛** → 编排 → **12 项质量门禁** → 五工作表出表。

Excel 五工作表：处理后的数据 / 排名最靠前的10条 / 窗口内全部数据 / 口径与稿外信息 / 评级留痕。口径表含附录 1–17、候选池边界留痕、逐条发布时间依据，以及：

- **工程留痕 A**：12 项质量门禁实测值、阈值、方向、口径、动态改进建议；
- **工程留痕 B**：F5 噪声处置留痕、摘要状态逐条留痕、正文补采统计、渠道未取得原因分类。

日志：每次运行一个 `run_id`，`logs/<run_id>.pipeline.log`（阶段耗时、计数、失败分类、异常 traceback）、`logs/runs.jsonl`、`logs/access.log`。

---

## 6. 数据质量：实测诊断 → 本版修复 → 仍需补的端点

### 6.1 实测诊断（历史批次；基于旧 §1–§31 白名单的一份 live 批次，50 条主表 / 66 条候选池）

> 以下 604 / 583 / 21 等数字针对的是**旧版 31 类白名单**，仅作历史诊断留痕；
> 当前主白名单是 31 类 599 渠道扩展版（需求②），v3 全行业分类版另存作扩展参考，见第 12 节。

```
python scripts/audit_xlsx.py /data/inputs/latest.xlsx -o 质量审计报告.md
```

| 指标 | 实测 | 问题 |
|---|---|---|
| 摘要有效率 | **0.0%**（50/50、66/66） | RSS description 是「标题&nbsp; 来源 标题。」模板，`&nbsp;` 未清洗，旧代码把标题复读当摘要 |
| 分类错配 | 10/50 | 「民生服务」大类挂「国际动态 / 灾害与公共卫生」标签；「经济地震」「地震属性」「生活大爆炸」误命中灾害/事故词 |
| 非新闻噪声 | 8/50 | 期刊论文、美剧、导购 FAQ、政务科普混入；「生活大爆炸」被标成重大安全事故 |
| 同一议题占比 | 24%（12 条） | A股低价股退市潮刷屏，议题簇未收敛 |
| 渠道触达 | **7 / 604**，未触达 597 | 白名单里 583 个渠道状态是「站内时间倒排 + RSS（**待配置端点**）」 |
| 记录类型 | 100% RSS 时间倒排 | 只有 Google News RSS 五路聚合能拉，一手源缺失 |

**渠道未取得的根因不是网络失败，是只登记了渠道名、没有可拉取 URL。** Google News RSS 是相关性 top-N 的聚合检索，既不能枚举全量，也不能替代央媒 / 部委 / 官方 API 的一手源。

### 6.2 本版已落地的修复

1. **摘要**：新增 `article.py` — `clean_html` 去 HTML 实体/标签/零宽字符；`is_title_echo` 判「标题复读」；命中一律标 `摘要未取得` 并写工程留痕 B，**绝不复读标题冒充**。`live.fetch_article` 从独立原文 URL 补正文，成功标「正文补采」。修复后 demo 批次摘要有效率 100%。
2. **分类**：标签前缀是上位大类名时强制 `group == 前缀`；「国际动态 / …」必须命中境外地理/国际组织信号；`FALSE_FRIENDS` 否定表拦住经济地震、地震属性、生活大爆炸等伪友好词；国内地震科普回落到「公共安全 / 防灾减灾」。分类一致率从实测 80% 提到 100%。
3. **噪声**：`noise_verdict`（F5）在准入层最前置剔除学术论文 / 影视娱乐 / 导购 FAQ / 政务科普，真实突发词豁免避免误伤，逐条依据进工程留痕 B。
4. **去重**：`TOPIC_THEMES` 通用议题（退市潮 / 寒潮 / CAR-T / 半导体产能等）取代只认三个硬编码事件，`story_max_keep=2` 收敛同议题不同主体。
5. **渠道申报**：失败按「未配置端点 / 超时 / 403 / 404 / 需 key / 无时间字段 / DNS」分类写进口径表与日志，不再是一句笼统「未取得」。
6. **质量门禁**：从 4 项扩到 12 项（先后新增摘要有效率、分类一致率、端点配置率、非新闻噪声率、议题重复率、段落重复率、实时源触达率），全部写进工程留痕 A；`warn_only=false` 可让不达标批次直接失败。同时修了旧版 `reg.summary()` 中文键导致工程留痕 A 显示 0/0 的 bug。

回归验证：上传文件里的 6 类噪声、5 类分类错配、2 类标题复读、3 类议题重复样本，全部被新规则正确识别；3 条真国际突发与 1 条寒潮预警不被误删。

### 6.3 还需要你做的（代码解决不了）

```bash
python scripts/check_endpoints.py             # 实测已登记端点，写 data/channel_health.json
python scripts/check_endpoints.py --offline   # 不联网，只统计配置缺口与 ★ 优先补齐名单
```

- **补端点**：主白名单 **599 渠道**（用户定稿，旧版 604 / 21 / 3.5% 仅作历史留痕）。本版体检器合并 `endpoints_live.yaml` + `channel_reach.json` 的实证端点，当前已配置 127 个、主口径配置率 21.2%。优先给 `channel_health.json` 的 ★ 名单补官方 RSS / 站内时间倒排 / API，再跑脚本验证。脚本只测已有 URL，**不会凭空编造端点**。
- **一手源优先**：RSS / 官方 API / 站点时间流优先于聚合页与热榜；每条必须带 `pubDate`，按资讯发布时间而非抓取时间过滤。
- **热榜只作发现**：词条强制回查到带事实的独立原文，回查不到进附录 15 不成稿。
- **正文与去重**：正文抽取走 trafilatura、相似度诊断走 rapidfuzz / jieba；摘要保持纯代码抽取式（按整句裁剪），不接任何模型语义层，保留「数字不改写 / 整句收束 / 占位句清除」校验器。
- **H 通道**：`scoring.h_channel: heat`（事件级热度 `heat-v1-48h-halflife24h`）。无平台后台播放/阅读/讨论数时，按同一事件下不同独立来源数构造可核对的 `H_c`，只叠加到确有多源覆盖的条目；`grade` 阈值（S≥85 / A≥70）与 `V_MAX` 不变。想回到纯 V 分可切 `auto`。
- **上线门禁**：把 `quality.warn_only` 改成 `false`，并把 `min_*` 阈值逐步上调。

### 6.4 架构层还有更好的方案吗

| 方案 | 优点 | 代价 |
|---|---|---|
| **GitHub Actions + Pages（当前）** | 零运维、免费、固定地址、覆盖式写入、自带日志与 artifact | cron 分钟级抖动、依赖 GitHub |
| 服务器 cron + Nginx | 秒级准点、完全可控 | 要维护机器、证书、部署 |
| Cloudflare Workers Cron + R2 | 边缘准点、免费额度大 | Python 流水线要重写成 JS/WASM，改造量大 |

对「定时 + 窗口 + 固定地址 + Excel」这四个硬需求，当前方案路径最短；换架构只解决准点，**不解决渠道端点与内容质量**。优先投入应放在 6.3 的数据层。

---

## 7. 边界声明

- 默认 `live` 模式为**真实联网抓取**：央视网 JSONP 秒级时间倒排、中新网/人民网 RSS、站内时间倒排页与独立原文正文补采；逐源触达与失败原因见 Excel 附录 21/21b。`--source target` 是内置标准语料重放（非实时），仅作离线回归。
- 质量门禁的 12 项数字来自本批真实单元格，不是估计；`demo` 模式渠道触达率与端点配置率必然偏低，属如实标注。
- GitHub Actions 定时不保证秒级准点；Pages 有 1–2 分钟缓存；`schedule` 只在默认分支生效、长期无提交可能被暂停。
- 日志不记录令牌与凭据；GitHub PAT 仅存页面 `sessionStorage`，且只在点重跑时使用。
- 不构成投资、医疗、法律或出行决策建议。

---

## 8. 常用命令

```bash
# 本地跑一次 + 覆盖站点 + 单文件预览
python run.py --source demo --json-summary
python build_site.py --overwrite --standalone 静态页面预览.html

# 本地服务（页面重跑走它）
python serve.py                    # http://127.0.0.1:8000

# 改定时与窗口
python scripts/set_schedule.py 20:00 --window 24 --interval lo-ro

# 质量审计与端点体检
python scripts/audit_xlsx.py docs/downloads/latest.xlsx -o 质量审计报告.md
python scripts/check_endpoints.py --offline

# 手动补跑任意窗口
gh workflow run daily-news.yml \
  -f window_start="2026-11-16 20:00" -f window_end="2026-11-17 08:00" \
  -f window_interval=lo-ro -f source=live
```

---

## 9. 对齐「目标态」样例（会话内补跑 → 流水线可复现）

用户给过一份高质量 `2026_11_17_窗口期.xlsx`：94 条主表、两段完整摘要、6 种记录类型、多渠道来源、`e1/e2` 事件主键、扩展大类。它的口径表写明**「会话内全渠道补跑，不是调度器执行，产物不来自仓库流水线」**，所以达成这种效果靠的是把「8 大类逐组检索 → 词条回查独立原文 → 正文成稿 → 多渠道合并」工程化进采集层，不是改页面或调分。详见仓库外的 `目标态达成路径.md`。

本轮已对齐的能力：

| 样例特征 | 落地 |
|---|---|
| 两段完整摘要、无标题复读 | `clean_html` + `is_title_echo` + `live.fetch_article` 正文补采 |
| 样例自身 49/94 条段落自我重复 | `_dedupe_paragraphs` + `_dedupe_sentences`（句级先去重，避免对半切又切出重复块），复现批次 0/88 |
| 扩展大类「工厂与制造业」「社会治理」 | `rules.legacy_groups: true` |
| `e1xxx` 独立原文 / `e2xxx` 词条回查成稿 | `cluster_events` 按 `record_type` 分组编号 |
| 多渠道 `渠道 · 渠道` 来源列 | inbox 保留原值；live 下由 `merge_to_main` 聚类 + 附录 12b |
| 6 种记录类型 / 时点推定 | inbox 逐字段导入；推定条目进附录 4b |
| 口径表计数与实际表不符（70/105 vs 94/129） | 流水线统一从真实候选池/主表计数，工程留痕 A/B 全部实测 |
| 可定时 / 可复现 / 固定地址 | 覆盖写入 `docs/downloads/latest.xlsx` |

质量门禁陆续扩到 12 项（含「段落重复率」`max_para_dup_ratio: 0.02` 与「实时源触达率」`min_source_ratio: 0.60`）；`--source inbox` 从成品 xlsx 导入时，`evidence` / `pub` / 逐渠道状态不可测，4 项采集侧指标标「不适用」而非误判不合格。

立即复现（把已有高质量台账变成成品 + 页面）：

```bash
python run.py --source inbox --inbox "<台账.xlsx>" \
  --start "2026-11-16 20:00" --end "2026-11-17 20:00" --interval lo-ro --json-summary
python build_site.py --xlsx output/2026_11_17_窗口期.xlsx \
  --summary output/2026_11_17_窗口期.json --mode inbox --overwrite --standalone 静态页面预览.html
```

继续扩源见第 6.3 节与第 11 节：补端点（主口径 599，当前配置率 21.2%）→ 开 `hotboards` / `search` 并配 key → `warn_only: false`（需你拍板）。

---

## 10. `target`：离线回归模式（默认已改为 live，见第 11 节）

第 9 节的 `inbox` 仍需要你提供一份台账。`target` 模式把「会话内全渠道补跑」的每一步写成代码，用内置标准语料重放形态，用于无网络时验证链路：

```bash
python run.py --source target                    # 按 config 的 24h 相对窗口（离线回归）
python run.py --source target --window "2026-11-16 20:00~2026-11-17 20:00" --interval lo-ro
python build_site.py --xlsx output/2026_11_17_窗口期.xlsx \
  --summary output/2026_11_17_窗口期.json --mode target --overwrite \
  --standalone 静态页面预览.html
```

**新增代码**（详见仓库外 `代码实现说明.md`）：

| 模块 | 作用 |
|---|---|
| `scripts/build_seed.py` | 把已核实台账原子化：129 条 → 418 个单源原子，生成 `data/target_seed.json` |
| `news_pipeline/target_source.py` | 8 大类归组 → 词条发现 → 独立原文回查 → 正文补采 → 多渠道合并 → 6 种记录类型 → 时间重放；`probe_live()` 探测实时端点，可达自动升级 live |
| `collectors.py` / `run.py` / `serve.py` / `build_site.py` | 接入 `target` 分支（可用 `--source target` 调用）；`/api/run` 与页面重跑按钮支持 |
| `exporter.py` | 口径表新增数据源诚实声明 / 实时端点探测 / 升级路径，新增**附录 20**逐环节留痕 |
| `quality.py` / `docs/assets/app.js` | `target` 采集侧 4 项标「不适用」；页面 meta 显示 `[代码化补跑 · 非实时语料]` |

实测（24h 与默认 12h 两批一致）：**128 / 128 / 88** 条，6 种记录类型齐全，`e1 56 / e2 32`，多渠道来源 80/88，两段以上摘要 84/88，段落重复 **0**，质量门禁内容侧达标；`serve.py` 真实 `POST /api/run` 返回 `ok=true` 并覆盖刷新站点。

**诚实边界**：`target` 是**内置标准语料按窗口重放，非实时抓取**——页面、页脚、Excel、质量建议四处标注，不声称当天新闻。日常请使用第 11 节的默认 `live` 模式。

---

## 11. 默认 `live`：真实联网多源抓取（无需贴台账 / 配 key / 补端点）

> **状态更新**：本节数字（10/11 源 · 815 条 · 93 条主表）是早期批次记录。
> 主白名单以 **31 类 599 渠道**为准（用户 2026-11-20 拍板）；v3 全行业分类版只作扩展参考，
> 端点实测与逐渠道启用动作见 **第 12 节** 与 `data/channel_reach.json`。
> 本节保留作链路设计与 `live_cn.py` 结构说明。

重新诊断网络后发现：受限环境不是全网封锁，而是**按域名白名单**。国际源（Google News / USGS / GDACS / ReliefWeb / RSSHub）返回 403，但央视网、中新网、人民网、政府网、百度、GitHub API 均 200 且带真实数据。

```bash
python run.py                                    # 默认 live，按 config 的 24h 相对窗口真实抓取
python run.py --window "2026-11-18 20:00~2026-11-19 20:00" --interval lo-ro
python build_site.py --xlsx output/2026_11_18_窗口期.xlsx \
  --summary output/2026_11_19_窗口期.json --mode live --overwrite \
  --standalone 静态页面预览.html
```

**核心模块 `news_pipeline/live_cn.py`**：11 个国内源（CCTV JSONP 分页 / 中新网 3 路 RSS / 人民网 3 路 RSS / 中新网滚动页 / 央视网首页 / 人民网首页 / 政府网要闻）+ 6 个国际源按可达性自动启用；时间归一化（秒级 `focus_date`、RFC822 `+0800`、分钟级 `dd_time`）；独立原文正文补采（并发 8）；源站残缺实体清洗；分类提示；逐源触达与失败留痕。

**实测**（2026-11-18，`(07:00, 20:00)` 左开右开）：**10/11 源触达、815 条原始新闻、正文补采 105/120（15 篇如实留空）** → 窗口外剔除 564 → 候选池 177 → 主表 **93** → 五工作表 Excel。质量门禁 11 项，内容侧全部达标：实时源触达率 90.9%、发布时间取得率 100%、摘要有效率 100%、分类一致率 100%、段落重复 0%。页面显示 `[实时联网抓取 · 10/11 源 · 815 条]`。

**配套改动**：`rules.py` 曾自造 `balanced` 贴近度档放宽判定（主表 19 → 93），**已按 SKILL §1.5 / §4.1.3 移除**：只保留 strict（默认）/ moderate / off 三档，真实批次的新表述补进 `MASS_TOPIC`，不放宽判定式；`exporter.py` 新增**附录 21 / 21b** 逐源留痕；`quality.py` 新增「实时源触达率」，与「白名单完备度」两项告警分开考核；`serve.py` / 页面重跑按钮直连真实重抓，端到端 `POST /api/run` 返回 `ok=true` 并覆盖刷新站点。

**诚实边界（历史批次）**：人民网 3 路 RSS 停在 2025-06，300 条旧页全部由窗口过滤剔除并留痕；政府网要闻为 JS 动态渲染，静态页如实申报 0 条；国际源组在受限沙盒 403，逐源申报不可达，部署到 GitHub Actions（网络不受限）会自动启用。旧版两项「白名单完备度」告警衡量的是当时 604 个渠道缺端点，**不是本批抓取失败**；当前已按 v3 清单改为盘点项，见第 12 节。不编造任何条目。

**继续扩源**：`python scripts/check_endpoints.py --offline` 出 ★ 优先补端点名单；配 `SERPER_API_KEY` / `TAVILY_API_KEY` 开 `live.hotboards` / `live.search` 词条回查；`quality.warn_only: false` 让内容侧不达标批次在 CI 直接失败。详见仓库外 `真实抓取能力说明.md`。

---

## 12. 按《资讯渠道大全》v3 清单抓取（当前主白名单）

> **重要更正**：此前项目用的是 `data/资讯渠道大全.md` 的 §1–§31 旧版（599 渠道），
> **不是用户提供 v3 全行业分类版**。旧解析器只认 `## 1.` 章节，对 v3 的 `## 一、` 章节
> 会解析出 **0 个渠道**。本轮已修复并把 v3 设为主白名单，旧表保留为
> `data/资讯渠道大全_31类扩展版.md`（仅作扩展参考，不参与门禁分母）。

**v3 清单**：14 个行业大类、逐行解析 **127 个渠道**（`★ 41 / ☆ 49 / ✗ 37`；
文档 §16 自述 113，实际表格行更多，以逐行解析为准）+ 21 个扩充端点登记 = **148 个白名单**，
其中 **★ 必采 46 个**。`channels.py` 现同时兼容 `## 一、` / `### 2.1` / `## 1.` 三种章节，
级别列允许带后缀（`✗ 仅参考热度`）。

```bash
python scripts/discover_endpoints.py              # 固定候选 62 + v3 入口列 101 个域名 × 4 种端点形态
python scripts/discover_endpoints.py --offline    # 不联网，只重算分层
```

每个域名并发试 `首页 / RSS / 新闻列表 / 要闻页 / 政策最新页` 等 8 个候选 URL，
只对「HTTP 200 且解析出 ≥3 条带日期条目」放行。**深度探测结论**：101 个域名里
**79 个 HTTP 403（域名级出口拦截）、8 个 404/412（候选路径不存在）、7 个可打开但四种形态
都抽不到带日期条目、2 个通过**（1 个已由固定候选覆盖，新增 `spp.gov.cn` 最高检 RSS）。

**实测结果**：端点 **55 → 56**、**56/56 源触达**、原始 **5184 条 → 主表 34 条**（固定可复现窗口 `(07:00, 20:00)` 左开右开）；
★ 必采 **14/46** 已覆盖、白名单 **20/148** 可直连、12 项质量指标全达标。

**修掉的丢数据 bug**（这才是「来源太少」的主因）：`enrich_bodies` 原先按采集顺序
`[:200]` 分配正文补采名额，全被 CCTV/中新网前 3 个源占满；新华网、光明网、经济日报、
部委站虽抓到窗口内条目却补不到正文、被字段校验剔除。改为**按源轮询配额**后，
补采覆盖 **30 个源**，主表来源从 **3 个 → 11 个**（候选池 3 → 13 个）。

**分层**（附录 22/22b/22c 逐渠道全量留痕；社交平台热榜另见附录 23）：

| 分层 | 渠道数 | 启用条件 |
|---|---:|---|
| 可直连（端点覆盖 / 平台归并） | 20 | 已启用，含 ★ 必采 14 个 |
| 网络出口拦截 | 82 | 本沙盒域名级 403；GitHub Actions 等 unrestricted 网络自动重测启用 |
| 需账号登录 / 动态渲染 | 16 | 需开放平台授权或已授权聚合 |
| 无公开入口（栏目 / 账号 / 线下） | 14 | 非独立站点，需人工确认官方数据源，不编造 |
| 待补端点（候选路径无可枚举时间条目） | 10 | 需补该站正确 RSS / 滚动页 / 公告列表 URL |
| 需密钥 | 6 | 配 `SERPER_API_KEY` / `TAVILY_API_KEY` |

**指标口径**：「渠道触达率 / 端点配置率」为分层盘点项（`适用=False`，不参与门禁），
本批抓取成败由「实时源触达率」(56/56) 判定。页面质量行显示
`v3清单 ★必采 14/46 已覆盖 · 白名单 20/148 可直连 · 本环境拦截 82（换网络自动启用） · 微博热搜 50 条线索（仅热度参考·不进主表）`。

**100% 全渠道的边界**：v3 的 46 个 ★ 里 18 个被本沙盒域名级拦截、10 个需补正确端点 URL、
5 个需密钥、1 个是平台内栏目——这些不是「没配」，是逐条实测后的可行动结论；
平台内栏目与需登录账号没有公开无鉴权端点，静态抓取永远覆盖不了，逐渠道申报、不编造条目。
详见仓库外 `全渠道抓取方案.md`。

---

## 13. 微博 / 抖音等社交平台热点数据

`config.yaml` 的 `source.live.social_hotboards: true`（默认开）启用
`live_cn.collect_social_hotboards()`，**每次 `python run.py` 自动执行**。

**实测结论**：

| 平台 | 结果 |
|---|---|
| 微博 | `weibo.com/ajax/statuses/hot_band` **HTTP 200**，50 条完整热搜，带秒级 `onboard_time`、热度值、分类、标签、排名、事件线索；`ajax/side/hotSearch` 冗余确认 |
| 抖音 | 10 个官方/历史路径全部 **403**（域名级拦截）；另有 6 个 RSSHub 公共实例、16 个第三方聚合镜像全部 403 / 连接失败 |
| 百度热搜 / 知乎 / B站 / 头条 / 快手 / 小红书 | 403 / 401 |

**按 v3 §14「✗ 仅作热度参考，默认不入池」**：热搜词条不是新闻，`onboard_time` 是上榜时间
而非资讯发布时间。快照**不进 `specs`**，因此不进候选池、不参与评级、不进主表、不计入质量门禁；
只在 Excel 附录 23 / 23a / 23b / 23c 原样留痕，页面质量行显示
「微博热搜 50 条线索（仅热度参考·不进主表）」。独立原始数据见
`data/微博热搜快照_2026-11-18.json`。

要把词条变成要闻：配 `SERPER_API_KEY` / `TAVILY_API_KEY` → 开 `source.live.search: true`
→ `collect_hotboards()` 回查到带事实的独立原文才成稿；回查不到进附录 15 申报，不编造。

**抖音的三条合规启用路径**：① 抖音开放平台申请「热门视频/话题榜单」能力（需应用审核）；
② 授权第三方热榜 API（按次计费）；③ 换 unrestricted 网络（GitHub Actions）后脚本自动重测
——但签名/设备指纹反爬仍在，通常仍需配合 ① 或 ②。搜索引擎缓存页不作为平台热榜数据凑数。

详见仓库外 `社交平台热点数据说明.md`。


---

## 本轮更新（全面核查：2026-11-21）

产品统一叫「**资讯获取**」。本轮不再按单点投诉修补，而是对配置、工作流、29 个流水线模块、
24 个脚本、数据文件、产物与 zip 做独立交叉核查，一次修掉 **20 项**问题，包括：
zip 漏掉 18 个验证证据、白名单分母 599/604 打架、两类渠道解析 `0→0` 假绿、
分类标签前缀不等于上位大类、候选池摘要超长无人查、标题被正文污染、
部署文档把 `h_channel` 写成 `auto`、CI 回写漏 `channel_health.json` 等。
完整逐项证据见包内 `资讯获取_全面核查与修复报告.md`。

**每次交付前的两道闸**（新增，不再靠人工记忆）：

```bash
python scripts/full_audit.py      # 20 硬项 + 全套 verify，任一 FAIL 返回非 0
python scripts/make_package.py    # 固化打包 + 解包自检（含 .github/ 与 output 证据）
```

当前基线：规格验收 **59/59**、规则回归 **89/89**、workflow **46/46**、部署包 **55/55**、
两类解析 **14/14**、质量 v3 **11/11**（离线回放 S1 / A10）、搜索硬闸 **21/21**、
通路 E **12/12**、mock 部署 **19/19**。推包仍只需双击 `deploy.bat` 粘贴 Token。
