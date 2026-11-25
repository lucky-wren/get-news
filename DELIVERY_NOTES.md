# 交付说明 · 面向大众的每日要闻

## 用起来（一键，只需 Token）
解压 → 双击 `deploy.bat` → 粘一次 Token。自动建/切仓库、真 `git push`、
同一 Token 启用 Pages（GitHub Actions 源）、触发一次 workflow，免登录免填仓库名。
之后每天北京 20:00 自动出 Excel 并发页面。页面与 Actions 的 Run workflow
均可填 `--window-start "YYYY-MM-DD HH:MM"` / `--window-end`，只采左开右闭窗口内
带可核对发布时间的数据。`--mode offline` 是 11-21 定稿回放，不覆盖线上地址。
若 `Settings → Pages` 被改回 branch 源，需手动设回 GitHub Actions。

## 本轮（截图 42/43/44/45）
根因：html 用 now±3 天漂移 + trafilatura 异常未降级 → CI 红、端点恒空；
旧链路成品有错配/政务稿/平台 0/非降序；⑳ 写死 True；「声明」误剔英伟达稿；
聚合名回退挂名；截图 45：deploy.keep_docs 保留远端旧 latest.xlsx 防 Pages 404，
零网络自检跑在出表步之前，新回归测试只读「文件在不在」就吃进旧批次必红。
修：当批窗口硬闸、异常降级、排除分组、挂名不回退、⑳ 真断言 + ㉑ 挂名闸、
deploy.run 文件 fd（防孙进程挂起）；成品硬验只在完整包同批产物跑，
出表步之后由 `verify_package.py` 硬验，pytest 不读 checkout 运行时产物。

## 真跑与验证（CI 同款 extract 依赖 / 生产路径，不带 replay）
窗口 11-23 20:00 → 11-24 20:00：21/21 达标，主表 39 / 候选 131 / 口径表 2523；
错配 0、政务行业稿 0、来源挂名 0、9/9 主干平台、Final_adj 严格降序、
展示序=分数序。两套 pytest 全绿、`verify_package.py` 47/47、阶段 A 56 项 0 FAIL、
五处改回旧码反证全红、真实 CLI A/B、部署端到端 12/12。

## 边界（不粉饰）
399 端点本环境策略层 403，端点一个没删、逐类申报缺口，正式 runner IP 可达即采；
本轮只取得 1 个榜单时点，四时点快照与站内时间倒排未取得，31 类「能否按窗口
枚举全量」写否；9 平台由窗口内本批榜单快照真实词条回查，不硬塞平台名。
离线逐格 0 差异验的是定稿语料回放，不等于真采全量。真推你的仓库、官方
ubuntu-latest runner、Windows 真机双击仍需你本地那一步。
