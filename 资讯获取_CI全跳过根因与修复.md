# 资讯获取 · CI「首步失败 → 9 步 0s 全跳过 → 摘要自己崩」根因与修复

> 【批次记录】本文件记录 2026-11-21 之前某一次运行/某一轮修复的实测数字，**不代表当前交付包状态**；当前状态见 `output/full_audit.json`、`output/branding_verify.json` 与 `资讯获取_全面排查与调整报告_v2.md`。

> 依据：2026-11-20 12:23 那次 Actions 运行截图（用户提供）。
> 结论先给：**不是网络问题、不是渠道问题、不是缺 Key，是 workflow 第一个 step 少了 `mkdir -p logs`。**
> 修复后即使端点重测真的失败，当天 Excel / 固定页面 / 下载也照常产出。

---

## 1. 先确认一件好事：新包确实推上去了

截图里的 step 名已经是新版 workflow：

| 截图里 | 旧版（11-20 10:05 那次） |
|---|---|
| 端点自动发现（按当前网络实测扩源） | 无此步 |
| 全渠道可达性矩阵（599 渠道逐条补齐，换网络自动扩源） | 无此步 |
| 免费源池实测（DailyHotApi 之外的公共实例 / 自建候选） | 无此步 |
| 构建固定站点（覆盖原文档） | 构建静态站（含历史归档与 Excel 下载） |
| 覆盖提交回仓库（页面地址不变） | 归档数据回写仓库（供次日累积） |
| CI 运行摘要（供排查） | 无此步 |

所以上一轮反复说的「推包才生效」这一步**你已经做了**，commit 产品名、17 步序列、端点重测都在仓库里了。这次挂的是新 workflow 自己的一个健壮性缺陷。

---

## 2. 截图逐行解读

```
✗ 端点自动发现（按当前网络实测扩源）                    3m 11s
⊘ 全渠道可达性矩阵 / 免费源池实测 / 采集 / 构建固定站点
  / 渠道端点体检 / 覆盖提交 / 配置 Pages / 上传产物
  / 部署到 GitHub Pages                                    0s   ← 全被跳过
✗ CI 运行摘要（供排查）                                    0s
   line 10: logs/ci-summary.txt: No such file or directory
   Error: Process completed with exit code 1.
✓ 上传运行日志（保留 7 天）                                1s
```

- 9 个空心圆 0s = GitHub 默认的 `success()` 依赖，前一步失败就全跳过；
- `if: always()` 的两个 step 仍执行：摘要崩了、日志上传成功（但 `logs/` 里几乎没文件，`if-no-files-found: warn` 让它不报错）；
- 「供排查」的 step 反而先把排查信息弄丢了，这是最讽刺也最该先修的一处。

---

## 3. 根因链（一条线，四步）

1. **`logs/` 没被 git 跟踪**，checkout 后必然不存在；
2. **第 132 行「端点自动发现」是唯一一个写 `logs/` 却没写 `mkdir -p logs` 的 step**：
   ```bash
   python scripts/discover_endpoints.py 2>&1 | tee logs/discover-endpoints.log
   ```
   其余 4 个写日志的 step（全渠道矩阵 / 免费源池 / 采集 / 端点体检）**都有** `mkdir -p logs`，唯独第一个漏了；
3. Actions 对 `shell: bash` 默认注入 `bash --noprofile --norc -e -o pipefail`。`tee` 打开不存在的路径立刻失败 → `pipefail` 把整条管道判非 0 → **step 标红**；
4. 后面 9 步按 `success()` 全部跳过 → 能创建 `logs/` 的 step 一个都没跑 → `if: always()` 的摘要 step 执行 `> logs/ci-summary.txt` → `No such file or directory` → 它自己也红了。

**为什么恰好 3m11s 才失败**：`discover_endpoints.py` 确实把 319 个域名 × 多路径 × 7s 超时真实跑完了；Python 接管道时 stdout 是块缓冲，脚本 print 不多，全程没 flush，直到进程退出才撞上早已退出的 `tee`，BrokenPipeError + pipefail 判定失败。所以「跑了 3 分钟」和「tee 一开始就写不进」并不矛盾。

**诚实的边界**：截图没有展开第一个 step 的内部日志，因此不能 100% 排除脚本自身也抛了异常（例如读 `channel_coverage.json` 失败）。但下面两类修复对**两种可能都覆盖**：`mkdir -p logs` 解决 tee/pipefail；`continue-on-error` + 降级分支解决脚本自身非 0。不管哪种，都不再让整批陪葬。

---

## 4. 修了什么（5 处）

| # | 位置 | 改动 |
|---|---|---|
| 1 | 新增 step「准备日志目录（排障前置）」，紧跟 checkout | 最前面就 `mkdir -p logs` + 写 `run-meta.txt`，第二层保险 |
| 2 | 「端点自动发现」 | 补 `mkdir -p logs`；`if python … \| tee; then … else … fi` 捕获非 0，打印 `::warning::` 明确「降级复用已有端点，本批仍正常出表」；加 `timeout-minutes: 10`；末尾读取端点清单的 Python 加 `\|\| true` |
| 3 | 全渠道矩阵 / 免费源池 / 端点体检 | 三个增强-诊断 step 全部加 `continue-on-error: true`（前两个另加 `timeout-minutes`）。**任何诊断失败都只告警，不再触发后面 0s 全跳过** |
| 4 | 「CI 运行摘要」 | 最前面无条件 `mkdir -p logs`；写文件、Python heredoc、`cat`、`ls` 全部 `\|\| true`；新增 `job 状态: ${{ job.status }}`、增强项降级说明、已生成排障文件列表 |
| 5 | 主链路（采集 / 建站 / 覆盖提交 / 部署） | **保持硬失败**，不加容错——真出不了 Excel 就该红，不能被掩盖 |

设计原则一句话：**扩源与诊断是增强项，采集与交付是硬承诺**。前者失败降级复用上一版端点，后者失败必须让 job 红。

---

## 5. 防回退：`scripts/verify_workflow.py`

纯离线静态校验，30 项，锁死三类回退：

- 每个写 `logs/` 的 `run:` 块，`mkdir -p logs` 必须出现在**首次写入之前**（按字符位置比）；
- 4 个增强 step 必须 `continue-on-error: true`；4 个主链路 step 必须**不**加；
- CI 摘要必须 `if: always()` + 自建目录 + `|| true` + 记录 `job.status`；运行日志必须 `if: always()` + `if-no-files-found: warn`；
- 旧文案「更新要闻速览 / 供次日累积 / 左开右闭 / → 当日 0800 / 历史归档」零残留；`--spec-verify`、`覆盖资讯获取数据` 仍在。

```bash
python scripts/verify_workflow.py            # 30/30 PASS
```

---

## 6. 验证记录

| 项 | 结果 |
|---|---|
| `verify_workflow.py` | **30/30 PASS** |
| 干净 checkout 模拟（删掉 `logs/`，按 `bash --noprofile --norc -e -o pipefail` 跑首步 + 摘要片段） | `rc=0`，生成 `discover-endpoints.log` + `ci-summary.txt`，**不再报 No such file or directory** |
| 失败降级模拟（让 Python 返回 rc=7，模拟端点重测真挂） | 打印 `::warning::…降级复用…` → `>>> 主链路继续执行` → 摘要仍出，`rc=0` |
| 编译 / 搜索硬闸 / 规则 / 规格 / 通路 E | `compileall` OK · **21/21** · **55/55** · **50/50** · **12/12** |
| zip 解包复验 | 110+ 文件、中文名 UTF-8、日志与缓存 0 入包，全套脚本在解包副本里重跑仍全绿 |

---

## 7. 你要做的只有一步

把本包推到 `main`，手动触发 `daily-news.yml`（或等 20:00）。**不用改任何 Secret、配置、窗口。**

重跑后按这个清单核：

1. 「准备日志目录（排障前置）」绿；
2. 「端点自动发现」绿或黄色警告（**黄也不影响**），日志里能看到实证端点数；
3. **「采集 → 筛选评级 → 五工作表 Excel」不再是 0s**，有真实耗时；
4. 「CI 运行摘要」绿，展开能看到 `job 状态: success`、窗口 `lo-ro`、质量门禁结论；
5. 「部署到 GitHub Pages」绿，`Reported success!`；
6. 固定地址 `https://lucky-wren.github.io/get-news/` 刷新后是**本批**数据，`latest.xlsx` 可下载。

如果端点重测那步仍是红/黄，展开它的日志把内容贴给我——现在它已经不会阻断出表，摘要里也会留下失败原因，定位会快很多。

---

## 8. 与「质量不行」的关系（别混为一谈）

这次事故和上一轮审计的 10 项内容质量 FAIL 是**两件事**：

- 本轮修的是「**跑不起来 / 全跳过**」；
- 上一轮修的是「**跑出来但摘要空、分类错、小震进主表、F5 漏网**」，那些代码改动都在同一个包里，但**必须等这次跑通才会出现在 Excel 里**。

也就是说：先让流水线真正跑完，之前那 55 条规则回归、门禁改严、F5 补词组、H 代理、USGS 4.5 阈值才会体现在数据上。推包重跑后，用 `python scripts/audit_batch.py <新xlsx> --logs <新日志目录>` 看 FAIL 数从 10 降到多少，那才是质量验收。

**未拍板项保持原值**：`quality.warn_only: true`（不达标只告警照常上线）。设 `false` 会让某天直接无产物，风险由你决定，我没擅自改。


---

## 9. 第二轮：截图仍报错，说明「修复根本没进仓库」（2026-11-20 22:xx）

用户又贴了一张几乎一样的截图：首步「端点自动发现」红 2m52s、后面 9 步 0s、
CI 摘要 `line 10: logs/ci-summary.txt: No such file or directory`、`exit code 1`。

**决定性证据**：本地新版 workflow 第 294 行就是 `mkdir -p logs`，第 307 行写
`> logs/ci-summary.txt || true`，而且首步带 `continue-on-error: true`。
若远端真是新版，首步失败会被降级成警告、后面 9 步**不会** 0s 跳过，
摘要更不可能报「No such file or directory」。所以结论只有一个：

> **仓库 `main` 上跑的仍是旧 workflow。上一轮的修复在本地，没进仓库。**

根因是老问题复发：手动复制项目文件时用了 `cp -r src/* dst/` / 资源管理器拖拽，
`.github/` 是点开头的隐藏目录，`*` 不匹配它，于是 `daily-news.yml` 永远是旧的。
日志里那个新 step 名（端点自动发现）说明**某一次**推成功过，但后续覆盖时又漏了，
或推到了非默认分支。

### 三层收口，不再靠「记得复制隐藏目录」

| 层 | 做法 | 解决什么 |
|---|---|---|
| **1 · 部署层（治本）** | `deploy.bat` + `deploy.ps1`：只需一个 PAT，自动 `git clone` → `robocopy /E`（含隐藏目录）→ `git add -A` → push；没装 git 就走 GitHub Contents API 逐文件 PUT。**推完读回远端 `daily-news.yml` 校验**「准备日志目录 / continue-on-error / 覆盖资讯获取数据」四个标记，缺一个就明确报「PAT 没勾 workflow 权限」 | 根治「以为推了其实没推」 |
| **2 · 仓库层（旧 workflow 双保险）** | `logs/.gitkeep` 随仓库提交。checkout 后 `logs/` 天然存在，**旧版 workflow 的 `tee logs/discover-endpoints.log` 也不会因缺目录崩** | 即使部署没更新，当天也不至于 0 产出 |
| **3 · workflow 层（上一轮已修）** | 准备日志目录 step、增强步 `continue-on-error`、摘要自建目录 + 全程 `\|\| true` | 新 workflow 自身的健壮性 |

`deploy.ps1` 里专门写了权限提示：修改 `.github/workflows/` 需要 classic PAT 勾
**`repo` + `workflow`**（fine-grained 需 Workflows 读写）；缺 `workflow` 时
GitHub 会静默拒绝更新 workflow 文件，`git push` 看着成功但远端没变——
这是第二个高频坑，远端校验就是为抓它。推送成功后脚本还会自动打一次
`workflow_dispatch`，不用再去页面点 Run。

### 新增校验

```bash
python scripts/verify_workflow.py   # 32/32：含 logs/.gitkeep 与部署脚本随包
python scripts/verify_deploy.py     # 37/37：CRLF / 括号配平 / 无明文 token / 远端校验标记与 workflow 一致
```

### 用户操作（唯一一步）

双击 `deploy.bat` → 粘贴 Token → 等脚本打印
「✅ 远端 workflow 已更新」+「✅ 已触发」。
若打印「❌ 远端 workflow 仍是旧版」，按提示重生成带 `workflow` 权限的 PAT 再跑一次。

**诚实边界**：沙盒是 Linux，无法真执行 `.bat` / `.ps1`；脚本按 Windows
PowerShell 5.1 语法编写，静态检查覆盖 CRLF、括号配平、关键字、无明文 token、
标记一致性，git / Contents API 两条路径逐行核对过，但**未在 Windows 实跑**。
真部署结果以脚本自己读回远端 workflow 的校验输出为准。


### 9.1 后续：一键部署脚本自己在 Windows 上报语法错（2026-11-20 22:xx）

用户双击 `deploy.bat` 后截图：中文 echo 被 cmd 当命令、PowerShell 5.1 报
`Unexpected token` / `Try 缺 Catch` / `Write-Host` 变 `鏉?枃绁?`。
根因仍是编码：**UTF-8 无 BOM + 中文**，cmd 按 OEM 解析 bat、PowerShell 5.1
按 ANSI(GBK) 读无 BOM 的 ps1，中文乱码破坏引号配对。

修复：bat 改纯 ASCII、主实现换成 `deploy.py`（Python 标准库，可用本地 mock
GitHub API 真实跑通）、ps1 降为回退并改成「UTF-8 BOM + 正文纯 ASCII」，
中文校验标记改用 base64 运行时解码。逐行根因与 19/19 真实 HTTP 自检证据见
`资讯获取_一键部署说明.md` 第 8 节、`output/deploy_selftest.json`。

**这不影响本文件第 9 节的结论**：仓库里跑的仍是旧 workflow 这件事没变，
只是「把修复推上去」的工具本身也得先能正常运行。用修好的 `deploy.bat` 重跑一次即可。
