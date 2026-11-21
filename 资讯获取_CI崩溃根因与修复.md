# 资讯获取 · Actions「采集 → 评级 → 五工作表 Excel」崩溃根因与修复

> 【批次记录】本文件记录 2026-11-21 之前某一次运行/某一轮修复的实测数字，**不代表当前交付包状态**；当前状态见 `output/full_audit.json`、`output/branding_verify.json` 与 `资讯获取_全面排查与调整报告_v2.md`。

> 依据：2026-11-20 19:08 运行截图（step 6 标红，耗时 **12m 11s**）。
> 结论：**不是网络问题、不是渠道问题、不是缺 Key、不是配置问题**，是上一版 `search_api.py` 一处类型 bug；填任何 Secret 都救不了，必须推修复后的代码包。

---

## 1. 截图堆栈逐行译读

```
run.py:248  in main            → R = run_pipeline(cfg, win, BASE, out)
pipeline.py:346 in run_pipeline → "trace": search_api.trace_rows(),
search_api.py:680 in trace_rows → return [t.as_row() for t in TRACE]
search_api.py:170 in as_row     → self.chosen_url[:100], round(self.chosen_score, 2),
TypeError: 'int' object is not subscriptable
[失败] ... 日志: logs/20261120_190819_97e962.pipeline.log
```

`chosen_url` 本该是字符串 URL，实际是 `int`，对它做 `[:100]` 切片直接抛错。行号与上一版交付包完全一致，根因可 100% 定位。

## 2. 根因：缓存「空结果」时把 `0` 当成了 URL

`search_api.search_best()` 会按 `query|site` 缓存候选，末尾无条件写入：

```python
_CACHE[qkey] = scored          # 所有 provider 返回 0 候选 / 配额用尽时，scored = []
```

同一个词被再次检索时（本批 112 源 + 主题检索 + 热榜词条回查，重复词很常见）命中空缓存：

```python
# 旧代码（bug）
TRACE.append(TraceRow(query, "cache", "成功" if hits else "无结果",
                      len(hits), hits[0].url if hits else 0, ...))
#                                      ^^^^^^^^^^^^^^^ int 0 塞进了 chosen_url
```

于是 TRACE 里多了一条 `chosen_url=0` 的留痕行。采集、准入、评分、聚类、编排全部正常跑完（所以耗时 12 分钟），直到 pipeline 尾部构造 logs、调用 `trace_rows()` 准备写「工程留痕 H · 搜索逐次留痕」时才崩。

**为什么前 12 分钟没有任何告警**：留痕序列化是出表前的最后一步，前面没有任何地方消费 `TRACE`。这是典型的「诊断数据拖垮主链路」。

## 3. 后果（比看起来严重）

- 截图里 `[产物] output/2026_11_20_窗口期.xlsx` 是 `run.py` 预置的产物**路径**打印，**不代表 Excel 已落盘**。异常发生在 `logs` 构造阶段，五工作表还没写。
- step 6 失败 → 后续 `build_site.py` / 归档回写 / `upload-pages-artifact` / `deploy-pages` 全部按 `success()` 跳过。
- 结果：**页面 `https://lucky-wren.github.io/get-news/` 和 `docs/downloads/latest.xlsx` 仍停留在上一批**，12 分钟采集白跑。这也解释了为什么「重跑后数据看着没变」。

## 4. 修复（4 处代码 + 2 组防回退用例）

| 文件 | 改动 |
|---|---|
| `news_pipeline/search_api.py` | ① 空缓存命中 `chosen_url` 写 `""` 而非 `0`；② `TraceRow.as_row()` 全字段类型归一（`_s` / `_num`），数值占位如实转写、不伪装成 URL；③ `trace_rows()` 逐条 try/except，坏留痕降级成一行「留痕序列化失败」，不丢行、不让整批崩；④ `stats()` / `provider_summary()` 用 `getattr` 防御非 TraceRow 对象 |
| `news_pipeline/pipeline.py` | 把 `search_api` 三处调用前置成 `_sa` 并包兜底：留痕异常只记 `runlog.metric(留痕异常=...)`，主链路继续出表。**留痕是诊断信息，不允许成为致命依赖** |
| `news_pipeline/search_api.py · self_test` | 新增「空缓存命中」+「int / None / 非 TraceRow 脏留痕」两组回归，断言 `trace_rows()` 不抛、不丢行 |
| `scripts/verify_search_quality.py` | 新增 3 项显式防回退：`trace_rows 容忍 int chosen_url` / `保留正常独立原文 URL` / `provider_summary 与 stats 不抛` |

## 5. 验证结果

| 检查 | 结果 |
|---|---|
| Python / JS / YAML 编译 | OK |
| 搜索层质量回归 | **21/21**（原 18 项 + 新增 3 项） |
| 规则回归 / 通路 E | 37/37、12/12 |
| 规格验收（五表 12 列 / 契约） | 50/50 |
| 模拟截图崩溃路径：空缓存 → `trace_rows()` | 2 行留痕、`chosen_url` 全为 `str`、cache 行取值为 `""`、**无异常** |
| 完整离线 pipeline 冒烟（demo 窗口） | 7 阶段跑通、五工作表出表成功、无 TypeError |

> 冒烟用的 demo 产物已在打包前恢复为上一版包内原件，`docs/downloads/latest.xlsx` 未被本轮调试数据污染。

## 6. 你要做的（只有一步）

把本轮 zip 推到 `lucky-wren/get-news` 的 `main`，再手动触发一次 `daily-news.yml`（或等 20:00）。**不需要改任何 Secret、配置或窗口**。

重跑后确认三件事：
1. step 6 不再标红，日志出现 `[质量]` 与产物路径；
2. 「工程留痕 H-c · 搜索逐次留痕」里有本次的逐次记录，cache 行的「选中 URL」是空而不是 `0`；
3. 后续 step 7–11（构建静态站 / 回写 / deploy-pages `Reported success!`）全部执行，页面与 `latest.xlsx` 更新。

另外请把上一次质量审计里那 4 条元问题一并核掉：commit 信息应为「覆盖资讯获取数据」、日志里应出现 `discover_endpoints.py` 与 `news_pipeline.reach`、端点快照生成时间应为当天、窗口文案应为 `lo-ro` 左开右开。若这次 19:08 的运行仍是旧 workflow，那两项（端点重测、零配置重跑）依旧没生效。

## 7. 诚实边界

- 这个 bug 是我在做「多 provider + 质量硬闸 + 配额缓存」时引入的，属于我的实现缺陷，与平台网络、渠道可达性、Serper 额度都无关。
- 本轮只改代码与回归，**没有在沙盒重跑 live**（这里对央视 / Sohu / 部委域名仍 403，重跑只会产出与你批次无关的数据并覆盖固定页面）。修复效果以你推包后的 Actions 运行为准。
- 上一轮审计里的 10 项质量问题（摘要有效率 0.26、分类/长度门禁放水、USGS 小震、统计值自相矛盾等）与本 bug 是**两件事**：那个包推上去后，门禁会如实报更多不达标项，不要误认为是这次崩溃引起的回退。
