# 魔搭 Notebook（免费 CPU 实例）· 通路 A 操作手册

> 【批次记录】本文件记录 2026-11-21 之前某一次运行/某一轮修复的实测数字，**不代表当前交付包状态**；当前状态见 `output/full_audit.json`、`output/branding_verify.json` 与 `资讯获取_全面排查与调整报告_v2.md`。

> 目标：用你图里那台 **CPU 环境 / 8核32GB / ubuntu22.04-py312**（长期有效）跑「开源聚合自建」，
> 让抖音等受限平台的词条能进「资讯获取」流水线。
> 配套脚本：`scripts/modelscope_notebook.py`（**纯 Python 标准库，零依赖**）。

---

## 先说一个会省你两小时的事实

魔搭 Notebook 是**会停止的实例**（图里两台都是「已停止」，CPU 虽标「长期有效」也要手动启动），
而且它的 `127.0.0.1` 只有 Notebook 自己能访问，**GitHub Actions 在另一台机器上，连不到它**。

所以正确的分工是：

| 魔搭 Notebook 用来做什么 | 每天 20:00 自动跑靠什么 |
|---|---|
| ① 先验证「阿里云这张网到底放不放通抖音/小红书」<br>② 手动跑一次、抓一份快照看真实数据<br>③ 本地验证 `DAILYHOT_BASE_URL` 契约能联通 | **不用魔搭常驻**：`daily-news.yml` 已改成在 Actions runner 内起同一个脚本（runner 网络 unrestricted），每天 20:00 自动生效。你只要把仓库推上去，什么都不用配 |

换句话说：**Notebook 是你的验证机，不是生产数据源**。下面步骤照做，5 分钟能出结论。

---

## Step 0 · 启动实例

在「我的 Notebook」列表找到 **CPU 环境**（8核 32GB，长期有效那台），点右侧 **启动**。
启动后进入 JupyterLab / Notebook 界面，开一个 **Terminal**（顶部 `File → New → Terminal`，
或者直接新建一个 `.ipynb` cell 里用 `!` 前缀跑命令也行）。

工作目录放持久化盘，别放临时目录：

```bash
mkdir -p /mnt/workspace/news && cd /mnt/workspace/news
python3 --version     # 3.10+ 即可，脚本只用标准库
```

---

## Step 1 · 把脚本弄进去（三选一）

项目推到 GitHub 后最省事：

```bash
# 方式 A：只拉这一个脚本（推荐，不用装 git 仓库全量）
wget -O modelscope_notebook.py \
  https://raw.githubusercontent.com/<你的owner>/<你的repo>/main/scripts/modelscope_notebook.py

# 方式 B：整仓拉下来（后面要跑 run.py 做本地验证就用这个）
git clone https://github.com/<你的owner>/<你的repo>.git && cd <repo>

# 方式 C：JupyterLab 左侧文件树 → 上传 → 把本地 scripts/modelscope_notebook.py 传上去
```

**不用装 docker、不用 npm、不用 pip install**——DSW 容器里通常没有 docker daemon，Node 也不一定带，
所以脚本直接实现了一个 DailyHotApi 兼容端点（`/douyin`、`/weibo`、`/health` 返回统一 JSON）。

---

## Step 2 · 必做第一步：先测网络，别先装东西

```bash
python3 modelscope_notebook.py --probe --timeout 5
```

约 10–30 秒，输出长这样：

```
平台          结果     词条    说明
--------------------------------------------------------------
douyin       可取     50      已解析出带标题词条
    └ 200    18234B  https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/
weibo        可取     51      已解析出带标题词条
...
xiaohongshu  不可取    0      本网络不可达（403 / DNS / 超时）
wechat       不可取    0      无免鉴权上游路由（小红书 / 微信搜一搜需走通路 C 或 D）
==============================================================
结论：魔搭出口可用，通路 A 成立 → 直接 --serve 起服务
完整报告 → hot_probe.json
```

判读：

- **抖音 / 微博 / 知乎 / 头条 有一个「可取」** → 通路 A 成立，继续 Step 3；
- **全部 403 / 超时** → 魔搭这张网和当前沙盒一样被拦，**别再折腾自建实例**，
  按 `资讯获取_受限平台取数方案.md` 走**通路 C**（注册一个带微信搜一搜 + 小红书端点的聚合 Key，
  填仓库 Secrets `HOT_API_PROVIDER / HOT_API_KEY / HOT_API_BASE_URL`）；
- **小红书 / 微信搜一搜恒为「不可取」**：前者要 `x-s/x-t` 签名，后者官方根本没有查询 API，
  DailyHotApi 也没有它们的上游路由。A 通路天生只覆盖 8–10 个平台，这两个必须靠 C 或 D。

把 `hot_probe.json` 留着——它就是「我确实测过、不是猜的」的证据。

---

## Step 3 · 起服务（DailyHotApi 兼容端点）

```bash
python3 modelscope_notebook.py --serve --port 7860
```

输出：
```
DailyHotApi 兼容端点已启动（demo=False）：
  本机：      http://127.0.0.1:7860/douyin
  健康检查：  http://127.0.0.1:7860/health
  项目侧配置：export DAILYHOT_BASE_URL=http://127.0.0.1:7860
```

**这个 Terminal 别关**（关了服务就没了）。另开一个 Terminal 验证：

```bash
curl -s http://127.0.0.1:7860/health | python3 -m json.tool
curl -s http://127.0.0.1:7860/douyin | python3 -c \
  'import sys,json; d=json.load(sys.stdin); print(d["code"], len(d["data"]), d["data"][0]["title"] if d["data"] else d["reason"])'
```

返回 `200 50 某某热点` 就成了；返回 `502 / 本网络不可达` 说明上游没通，回去看 Step 2 的结论。
只验契约、不想真联网时用 `--demo`（内置样本数据）：

```bash
python3 modelscope_notebook.py --serve --port 7860 --demo
```

---

## Step 4 · 本地验证接进项目（可选但建议做一次）

在同一个 Notebook 里、项目根目录下：

```bash
export DAILYHOT_BASE_URL=http://127.0.0.1:7860
python -m news_pipeline.platform_access          # 应看到「抖音·热点 命中=A 线索=N」
python run.py --start "2026-11-18 20:00" --end "2026-11-19 20:00" --interval lo-ro --spec-verify
```

打开产出的 xlsx → 「口径与稿外信息」→ **工程留痕 G-a**，应该看到抖音那行 `命中通路=A`、
逐通路结论里写着 `DailyHotApi /douyin → N 条`，而不是「未配置」。

**关键提醒**：拿到的是**词条线索**（话题名 + 热度 + 上榜时间），不是新闻。
流水线 0a 段会逐条走 `lookup_article` 回查独立原文，配了 `SERPER_API_KEY` / `TAVILY_API_KEY`
才会成稿；没配就走 Bing 站内回查，回查不到的进附录 15「线索待原文」，**不会拿热度值冒充摘要**。
所以想要主表真的多出抖音来源，请把搜索 key 也配上。

---

## Step 5 · 抓一份快照带回项目（可选）

```bash
python3 modelscope_notebook.py --once
# → hot_snapshot_YYYYmmdd_HHMM.json，JupyterLab 里右键下载
```

回灌方式（把外部抓到的线索注入统一契约）：

```python
import json
from news_pipeline.platform_access import inject_clues
d = json.load(open("hot_snapshot_20261119_2000.json", encoding="utf-8"))
for route, rows in d["平台"].items():
    clues = inject_clues(route, rows, tier="A")
    print(route, len(clues), "条，仍需逐条回查独立原文")
```

---

## Step 6 · 接进「每天 20:00」——其实你什么都不用做

`daily-news.yml` 的采集步骤已经改成：

```yaml
DAILYHOT_BASE_URL: ${{ secrets.DAILYHOT_BASE_URL || 'http://127.0.0.1:7860' }}
# 若 URL 是 127.0.0.1 → 在同一 runner 内后台起 modelscope_notebook.py --serve
# → 等 /health 就绪 → 留一份 logs/hot_probe.json → 再跑 run.py → trap 自动停服务
```

Actions runner 的网络是 unrestricted 的，抖音公共热榜端点**不需要 `a_bogus` 签名**，
所以多数情况下推上去就能取到。每次运行还会把逐平台实测写进 `logs/hot_probe.json` 和
Excel 工程留痕 G，取不到就逐条写原因，不会静默、不会编数据。

只有当你**确实想用魔搭当外部源**时才需要多两步（不推荐当唯一数据源）：

1. 把 Notebook 里的服务暴露成公网 HTTPS（用平台的服务部署能力，或 `cloudflared tunnel` / ngrok）；
2. 把公网地址填到仓库 **Settings → Secrets → `DAILYHOT_BASE_URL`**。

代价要说清：Notebook 一停止服务就没了，免费 CPU 实例没有 7×24 SLA，
到 20:00 没启动那一天就会降级成「逐通路不可达」。要稳定常驻，更合适的是把 DailyHotApi
部署到 Vercel / Cloudflare（免费常驻），再填同一个 Secret。

---

## 排错速查

| 现象 | 原因 / 处理 |
|---|---|
| `--probe` 全 403 | 出口域名级拦截，换 UA / 路径 / http 都救不回（已实测 7 指纹 × 10 路径 0 救回）→ 走通路 C |
| 上游 200 但 0 词条 | 平台改了返回结构，把样本贴进 `parse_upstream()` 加一个分支即可 |
| `/douyin` 返回 502 | 服务活着但上游没通，看返回里的 `tried` 逐条状态码 |
| 项目里通路 A 显示「未配置」 | 忘了 `export DAILYHOT_BASE_URL`，或导出到了另一个 Terminal |
| 有词条但主表没多出来源 | 没配搜索 key，词条回查不到独立原文，全进附录 15（这是规格要求，不是 bug） |
| Notebook 重启后文件没了 | 放 `/mnt/workspace` 持久化盘；临时目录会丢 |
| 小红书 / 微信搜一搜始终 0 条 | 正常：A 通路无上游路由，必须 C（授权聚合）或 D（自签名） |

---

## 边界（不粉饰）

- 脚本**不内置任何签名逆向**，不碰 `a_bogus` / `x-s` / 设备指纹；那属于通路 D，要独立部署签名服务。
- 只取公开可见的榜单词条，遵守平台条款与 robots；涉及用户个人信息 / 行为数据的处理受《个人信息保护法》约束。
- 词条必须回查带事实的独立原文才成行，标题里的月日只是日期线索，不反推发布时间。
- 本沙盒实测 12 个平台全 403（含抖音 6 端点、小红书、知乎 401），所以这份手册里的「可取」结论
  **要由你在魔搭上跑 `--probe` 才能确认**，我不替你下结论。
