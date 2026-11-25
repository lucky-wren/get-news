"""页面层：与 xlsx 同一批 Batch 的两个视图，绝不读旧 js / 不写死时间。

用户明确要求「时间窗口设置」：页面上必须能填起止年月日时、应用后只显示窗口内
数据，并能按该窗口重跑；不是只在口径表里写一句窗口文字。每条数据带可核对的
发布时间与时间依据，前端过滤因此有真实凭据，不是摆设。

规格验收 hard 全过后才由 __main__ 调用；页面与 xlsx 必须同批，见 verify_package
的「页面与成品同批」校验。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import clean

TZ = timezone(timedelta(hours=8))

HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>get-news · 资讯获取</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#eef4f4;color:#1d2b2b;
 font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1500px;margin:0 auto;padding:24px 26px 48px}
h1{font-size:26px;margin:0 0 4px}h1 small{font-size:13px;font-weight:400;color:#5b6e6e;margin-left:10px}
.meta{color:#5b6e6e;font-size:13px;margin-bottom:14px}
.winbox{background:#fff;border:1px solid #c9dede;border-radius:8px;padding:14px 18px;margin-bottom:16px}
.wt{font-weight:700;font-size:15px;margin-bottom:4px}
.wt span{font-weight:400;font-size:12.5px;color:#5b6e6e;margin-left:10px}
.wf{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:10px}
.wf label{font-size:13px;color:#395;display:flex;gap:6px;align-items:center}
.wf input{padding:7px 9px;border:1px solid #b9d2d2;border-radius:6px;font-size:14px;
 font-family:inherit;background:#fbfefe;color:#1d2b2b}
.btn{display:inline-block;padding:8px 16px;border-radius:7px;text-decoration:none;
 font-size:14px;border:1px solid #b9d2d2;background:#fff;color:#175b8e;cursor:pointer}
.btn.pri{background:#1668d6;border-color:#1668d6;color:#fff}
.btn:hover{filter:brightness(.97)}
.wg{font-size:13px;margin-top:10px;color:#1d2b2b}
.wg b{color:#1668d6}
.cmd{margin-top:8px;font-size:12.5px;color:#5b6e6e;display:none}
.cmd code{display:block;margin-top:5px;padding:9px 11px;background:#f0f6f6;border-radius:5px;
 white-space:pre-wrap;word-break:break-all;color:#1d2b2b}
.fmt{margin-top:7px;font-size:12.5px;color:#7b8b8b}
.fmt code{display:inline;margin:0;padding:1px 5px;white-space:normal}
.btns{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.q{border-left:5px solid #d9a300;background:#fff9e5;padding:14px 18px;border-radius:6px;margin-bottom:18px}
.q b{font-size:16px}.q.ok{border-color:#2fa36b;background:#e9f8ef}
.q .sub{color:#5b6e6e;font-size:13px;margin-top:5px}
nav{display:flex;gap:20px;border-bottom:2px solid #d6e2e2;margin-bottom:12px}
nav a{padding:8px 2px;cursor:pointer;color:#5b6e6e;font-weight:600;border-bottom:2px solid transparent;margin-bottom:-2px}
nav a.on{color:#1668d6;border-bottom-color:#1668d6}
.tbl{background:#fff;border-radius:7px;overflow:auto;box-shadow:0 1px 3px #0001}
table{border-collapse:collapse;width:100%;min-width:1420px;font-size:13px}
th,td{padding:9px 10px;border-bottom:1px solid #e4eeee;text-align:left;vertical-align:top}
th{background:#DDEBF7;font-weight:700;white-space:nowrap;position:sticky;top:0}
td.abs{max-width:380px;white-space:pre-wrap;word-break:break-word}
td.src{min-width:190px;white-space:nowrap;word-break:keep-all}
td.rec{white-space:nowrap}
tr:hover td{background:#f6fbfb}
tr.out td{background:#f4f6f6;color:#98a6a6;text-decoration:line-through}
footer{color:#617373;font-size:12.5px;margin-top:18px;line-height:1.8}
code{background:#dfeaea;padding:1px 5px;border-radius:3px}
</style></head><body><div class="wrap">
<h1>get-news · 资讯获取 <small>每天 20:00 (Asia/Shanghai)</small></h1>
<div class="meta">本批窗口 <b id="win"></b> · 本批生成于 <b id="gen"></b> · <span id="cnt"></span></div>

<div class="winbox">
 <div class="wt">时间窗口设置 <span>Asia/Shanghai · 左开右闭 · 只采集窗口内数据 · 精确到年月日时（分钟）</span></div>
 <div class="wf">
  <label title="格式 YYYY-MM-DD HH:MM（Asia/Shanghai）">起 <input type="datetime-local" id="ws" step="60"></label>
  <label title="格式 YYYY-MM-DD HH:MM（Asia/Shanghai）">止 <input type="datetime-local" id="we" step="60"></label>
  <button class="btn pri" id="apply">应用窗口（只显示窗口内）</button>
  <button class="btn" id="reset">恢复本批窗口</button>
  <button class="btn" id="rerun">按此窗口重跑</button>
 </div>
 <div class="wg" id="wg"></div>
 <div class="fmt">填写格式 <code>YYYY-MM-DD HH:MM</code>（如 <code>2026-11-24 08:00</code>）；
 浏览器可能按本地语言显示，但控件值与重跑命令始终是该格式。</div>
 <div class="cmd" id="cmd"></div>
</div>

<div class="btns">
<a class="btn pri" id="dl" href="downloads/latest.xlsx">下载本期 Excel</a>
<a class="btn" id="run" href="#">重跑并刷新</a>
<a class="btn" href="news-data.js">刷新页面</a>
<a class="btn" id="repo" href="#">仓库</a></div>
<div class="q" id="q"></div>
<nav id="nav"></nav>
<div class="tbl"><table><thead id="th"></thead><tbody id="tb"></tbody></table></div>
<footer>数据来自同批 <code>docs/downloads/latest.xlsx</code>；页面与表格是同一批对象的两个视图，
生成时刻与采集窗口均由本批现算，不沿用历史构建戳。「应用窗口」按每条可核对的发布时间过滤，
超窗与无时间依据的条目逐条统计、不混入窗口内数据；「按此窗口重跑」给出带
<code>--window-start</code> / <code>--window-end</code> 的命令，在 Actions 的 Run workflow 里
填入同样的起止值即可真正按该窗口采集。规格验收不达标的批次会被撤回，页面保留上一批。</footer>
</div>
<script src="news-data.js?v=__STAMP__"></script>
<script>
const D=window.NEWS_DATA||{};
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const WS=document.getElementById('ws'),WE=document.getElementById('we');
const BATCH_S=D.window_start||'',BATCH_E=D.window_end||'';
WS.value=BATCH_S; WE.value=BATCH_E;
document.getElementById('win').textContent=D.window||'—';
document.getElementById('gen').textContent=D.generated_at||'—';
document.getElementById('cnt').textContent='候选池 '+(D.counts?.candidate??0)+' / 主表 '+(D.counts?.main??0)+' / 31 类逐类申报 '+(D.counts?.reports??0);
document.getElementById('dl').href='downloads/latest.xlsx?v='+encodeURIComponent(D.stamp||'');
document.getElementById('run').href=D.actions_url||'#';
document.getElementById('repo').href=D.repo_url||'#';
const q=document.getElementById('q'),Q=D.quality||{};
q.className='q'+(Q.level==='达标'?' ok':'');
q.innerHTML='<b>质量结论：'+esc(Q.text||'—')+'</b>'+
 '<div class="sub">'+(Q.unmet&&Q.unmet.length?'未达标项：'+Q.unmet.map(esc).join('、'):'全部内容硬指标达标')+'</div>';

const views=[['主表','main'],['前 10','top'],['候选池','candidate'],['31 类逐类申报','reports']];
let cur=0, applied=null;   // null = 本批窗口（全量）；否则 = {s,e} 用户窗口
function colsOf(k){return k==='reports'?['类目','覆盖渠道','本轮命中','成稿条数','能否按窗口枚举全量','缺口']
 :(D.cols||[]);}
function metaOf(k,i){return (D.time_meta&&D.time_meta[k]&&D.time_meta[k][i])||{};}
function inWin(k,i){
 if(!applied) return true;
 const m=metaOf(k,i);
 if(!m.pub) return false;                       // 无可核对发布时间 = 不进窗口内
 if(m.date_only){                              // 仅日期：当日全天与 (s,e] 求交集，不推测时刻
  const d=m.day;
  return (d+'T23:59')>applied.s && (d+'T00:00')<=applied.e;
 }
 return m.pub>applied.s && m.pub<=applied.e;   // 左开右闭
}
function stat(){
 function cnt(k){let a=0,b=0,c=0;(D[k]||[]).forEach((r,i)=>{const m=metaOf(k,i);
  if(!m.pub) c++; else if(inWin(k,i)) a++; else b++;});return {a,b,c};}
 const M=cnt('main'),C=cnt('candidate');
 const g=D.time_gate||{};
 const batch=('本批时间窗口硬闸：窗口内 <b>'+(g['窗口内']??'—')+'</b> 条 / 超窗口 <b>'+
   (g['超窗口']??'—')+'</b> 条 / 时间依据缺失 <b>'+(g['时间依据缺失']??'—')+
   '</b> 条（逐条留痕见 Excel 附录 18）');
 if(!applied){
  document.getElementById('wg').innerHTML=batch+
   '<br>当前显示<strong>本批窗口</strong>内的全部数据；填起止后点「应用窗口」即按新窗口过滤。';
 }else{
  document.getElementById('wg').innerHTML=batch+
   '<br>已应用自定义窗口 <b>'+esc(applied.s.replace('T',' '))+' → '+esc(applied.e.replace('T',' '))+
   '</b>：主表 窗口内 <b>'+M.a+'</b> / 超窗剔除 <b>'+M.b+'</b> / 无时间依据 <b>'+M.c+
   '</b>；候选池 窗口内 <b>'+C.a+'</b> / 超窗剔除 <b>'+C.b+'</b> / 无时间依据 <b>'+C.c+
   '</b>。超窗条目不显示。仅给出日期、未给时刻的条目按当日全天求交集，不推测具体时刻。';
 }
}
function rowsOf(k){
 const r=D[k]||[];
 if(k==='reports') return r.map(o=>colsOf(k).map(c=>o[c]??''));
 const out=[];
 r.forEach((row,i)=>{ if(inWin(k,i)) out.push({row,meta:metaOf(k,i)}); });
 return out;
}
function render(){
 document.getElementById('nav').innerHTML=views.map((v,i)=>'<a class="'+(i===cur?'on':'')+'" data-i="'+i+'">'+v[0]+'</a>').join('');
 const k=views[cur][1],cols=colsOf(k),rs=rowsOf(k);
 document.getElementById('th').innerHTML='<tr>'+cols.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr>';
 document.getElementById('tb').innerHTML=rs.map(o=>{
  const r=k==='reports'?o:o.row;
  const m=k==='reports'?{}:o.meta;
  const tip=m.pub?('发布时间 '+esc(m.date_only?(m.day+'（仅日期）'):m.pub.replace('T',' '))+
    ' · '+esc(m.basis||'时间依据见附录 18')):'';
  return '<tr'+(tip?' title="'+tip+'"':'')+'>'+cols.map((c,j)=>{
   const cls=(c==='内容摘要'?'abs':c==='来源'||c==='缺口'?'src':c==='记录类型'?'rec':'');
   return '<td class="'+cls+'">'+esc(r[j])+'</td>';}).join('')+'</tr>';
 }).join('');
 stat();
 document.querySelectorAll('#nav a').forEach(a=>a.onclick=()=>{cur=+a.dataset.i;render();});
}
function cmdText(s,e){
 const d=(e||BATCH_E).slice(0,10);
 return 'python -m newsbrief --mode live --date '+d+
   ' --window-start "'+s.replace('T',' ')+'" --window-end "'+e.replace('T',' ')+'"';
}
document.getElementById('apply').onclick=()=>{
 const s=WS.value,e=WE.value;
 if(!s||!e){alert('请填完整的起止年月日时');return;}
 if(s>=e){alert('窗口左端必须早于右端');return;}
 applied={s,e};
 const c=document.getElementById('cmd');
 c.style.display='block';
 c.innerHTML='按此窗口重跑的命令（在 Actions 的 Run workflow 里填同样的起止值）：<code>'+
   esc(cmdText(s,e))+'</code>';
 render();
};
document.getElementById('reset').onclick=()=>{
 applied=null; WS.value=BATCH_S; WE.value=BATCH_E;
 document.getElementById('cmd').style.display='none'; render();
};
document.getElementById('rerun').onclick=()=>{
 const s=WS.value||BATCH_S,e=WE.value||BATCH_E;
 const c=document.getElementById('cmd');
 c.style.display='block';
 c.innerHTML='按此窗口重跑的命令（可复制；Actions 的 Run workflow 里填同样的起止值）：<code>'+
   esc(cmdText(s,e))+'</code>';
 try{navigator.clipboard.writeText(cmdText(s,e));}catch(_){}
 window.open(D.actions_url||'#','_blank');
};
render();
</script></body></html>
"""


def _iso(dt: datetime | None) -> str:
    """datetime-local 用的 `YYYY-MM-DDTHH:MM`（Asia/Shanghai）。"""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).strftime("%Y-%m-%dT%H:%M")


def _batch_bounds(batch) -> tuple[str, str]:
    win = getattr(batch, "win", ()) or ()
    if len(win) == 2:
        return _iso(win[0]), _iso(win[1])
    # 兼容：从窗口串里取两个 `YYYY-MM-DD HH:MM`
    m = re.findall(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", batch.window or "")
    if len(m) >= 2:
        return m[0].replace(" ", "T"), m[1].replace(" ", "T")
    return "", ""


def _time_meta(items: list) -> list[dict]:
    """逐条时间依据：把 published 规整成可比较的 `YYYY-MM-DDTHH:MM`。

    前端只做定长字符串比较（等价于时间序），不依赖浏览器本地时区，也不用
    Date 对象，避免 UTC/本地时区把窗口判错。
    """
    out = []
    for it in items:
        src = (it.published or "").strip()
        pub = clean.parse_pub(src)
        date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", src))
        basis = it.time_basis or "采集/原文页面发布时间；逐条留痕见 Excel 附录 18"
        if date_only:
            basis = ("原文仅给出日期、未给出时刻；按当日全天与窗口求交集，"
                     "不推测具体时刻。") + basis
        out.append({
            "pub": _iso(pub),
            "date_only": date_only,
            "day": src if date_only else "",
            "basis": basis,
        })
    return out


def build_site(batch, checks: list[dict], docs_dir: Path) -> None:
    """只在规格验收 hard 全过后调用；页面时间戳与窗口一律本批现算。"""
    now = datetime.now(TZ)
    stamp = now.strftime("%Y%m%d%H%M%S")
    repo = os.environ.get("GITHUB_REPOSITORY", "lucky-wren/get-news")
    hard = [c for c in checks if c.get("结果") == "FAIL" and c.get("gate", True)]
    soft = [c for c in checks if c.get("结果") == "FAIL" and not c.get("gate", True)]
    n_failed = len(getattr(batch, "failed", {}) or {})
    gap = (f"本批 {n_failed} 个端点不可达，主表为窗口内可见子集、非全渠道成果；"
           "正式环境端点可达时条数由当批真实采集量决定。"
           if n_failed >= 20 else "")
    empty_main = not getattr(batch, "main", None)
    # 正常链路里 0 条已被 verify ⑱ 拦下、根本不会走到这里；这是第二层防御，
    # 防止任何绕过验收的调用把「主表 0 条」渲染成绿色「达标」。
    if empty_main:
        level = "不达标"
        text = "本批主表 0 条，未达交付要求；页面与固定下载地址保留上一批。"
    elif soft or n_failed >= 20:
        level = "产能不足"
        text = gap + ("产能不足（内容形态达标，多渠道覆盖与本批条数未达期望）"
                      if soft else "内容形态与编排通过")
    else:
        level = "达标"
        text = "达标（内容形态与编排全部通过）"
    quality = {
        "level": level, "text": text,
        "unmet": ([str(c["项"]) for c in soft]
                  + (["主表 0 条：窗口内未产出可交付要闻"] if empty_main else [])),
        "hard_failed": len(hard),
    }
    ws, we = _batch_bounds(batch)
    gate = {}
    if getattr(batch, "rule_trace", None) is not None:
        gate = dict(getattr(batch.rule_trace, "counters", {}).get("时间窗口", {}))
    data = {
        "product": "get-news · 资讯获取",
        "window": batch.window,
        "window_start": ws,          # 页面时间窗口设置控件的默认值
        "window_end": we,
        "time_gate": gate,           # 本批时间硬闸统计（窗口内/超窗口/时间依据缺失）
        "date": batch.date,
        "mode": batch.mode,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "stamp": stamp,
        "counts": {"candidate": len(batch.candidate), "main": len(batch.main),
                   "reports": len(batch.reports), "koushi": len(batch.koushi)},
        "quality": quality,
        "cols": ["展示序", "Final_adj", "等级", "原始 Final", "分数序", "上位大类",
                 "分类标签", "标题", "内容摘要", "来源", "事件主键", "记录类型"],
        "main": [it.row(main=True) for it in batch.main],
        "top": [it.row(main=True) for it in batch.main[:10]],
        "candidate": [it.row(main=False) for it in batch.candidate],
        "reports": batch.reports,
        "time_meta": {
            "main": _time_meta(batch.main),
            "top": _time_meta(batch.main[:10]),
            "candidate": _time_meta(batch.candidate),
        },
        "koushi_rows": len(batch.koushi),
        "actions_url": f"https://github.com/{repo}/actions/workflows/daily.yml",
        "repo_url": f"https://github.com/{repo}",
    }
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "news-data.js").write_text(
        "// build " + stamp + " · 由 newsbrief.site 从本批 Batch 现算\n"
        + "window.NEWS_DATA=" + json.dumps(data, ensure_ascii=False) + ";\n",
        encoding="utf-8")
    (docs_dir / "index.html").write_text(
        HTML.replace("__STAMP__", stamp), encoding="utf-8")
