(function () {
  'use strict';

  var DATA = window.NEWS_DATA || { site: {}, editions: [] };
  var CONFIG = window.NEWS_CONFIG || {};
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };
  var state = { busy: false, edition: DATA.editions[0] || null, localApi: false, runUrl: '' };

  function norm(s) { return String(s || '').replace(/\s+/g, ''); }
  function normTime(s) { return String(s || '').replace('T', ' ').slice(0, 16); }

  function abstractState(it) {
    if (it.abstract_status) return it.abstract_status;
    var raw = (it.paragraphs || []).join('');
    if (!raw || /&nbsp;|&#160;|<[a-z]/i.test(raw)) return '复读 / 未取得';
    var clean = norm(raw), t = norm(it.title);
    var core = clean.split(t).join('');
    (it.sources || []).forEach(function (s) { core = core.split(norm(s)).join(''); });
    core = core.replace(/[。！？；，、,.!?;:：·|]/g, '');
    return core.length < 12 ? '复读 / 未取得' : '有效';
  }

  function clientQuality(items) {
    var n = items.length || 1, bad = 0, mism = 0, dup = 0;
    items.forEach(function (it) {
      if (abstractState(it).indexOf('有效') < 0) bad++;
      var pre = String(it.tag || '').split('/')[0].trim();
      if (pre && pre !== it.group && /国际动态|民生服务|财经市场|科技与产业|重大突发与司法|社会治理与养老|文娱与热榜|重要赛事/.test(pre)) mism++;
      if (/退市|跌破1元|跌破面值|低于1元|低价股/.test(it.title || '')) dup++;
    });
    return {
      '摘要有效率': Math.round((n - bad) / n * 1000) / 10,
      '分类一致率': Math.round((n - mism) / n * 1000) / 10,
      '退市议题占比': Math.round(dup / n * 1000) / 10,
      '条目数': items.length
    };
  }

  function setStatus(text, kind) {
    var el = $('status');
    el.className = 'status' + (kind ? ' ' + kind : '');
    el.innerHTML = text;
  }

  function render() {
    var e = state.edition;
    if (!e) { $('empty').hidden = false; return; }
    var w = e.window || {};
    var MODE_TAG = { target: '代码化补跑 · 非实时语料', demo: '离线演示 · 非实时',
                     inbox: '台账导入 · 非实时', live: '实时联网抓取',
                     hybrid: '混合 · 含非实时' };
    var tag = MODE_TAG[e.mode] || String(e.mode || '—');
    var ls = e.live_sources || {};
    if (e.mode === 'live' && ls['启用源数']) {
      var hit = (ls['逐源触达'] || []).filter(function (r) { return r['status'] === '已触达'; }).length;
      tag += ' · ' + hit + '/' + ls['启用源数'] + ' 源 · ' + (ls['原始条目数'] || 0) + ' 条';
    }
    $('meta').textContent = '[' + tag + '] 窗口 ' + (w.label || '—') + ' · 每天 ' +
      (CONFIG.run_at || '20:00') + ' ' + (CONFIG.timezone || 'Asia/Shanghai') +
      ' · 更新 ' + (CONFIG.updated_at || e.generated_at || '—');

    if (!$('start').value) {
      $('start').value = normTime(w.start || '').replace(' ', 'T');
      $('end').value = normTime(w.end || '').replace(' ', 'T');
      $('interval').value = w.interval || CONFIG.interval || 'lo-ro';
    }
    $('runat').value = CONFIG.run_at || '20:00';

    var items = e.items || [], pool = e.candidate || [];
    var q = e.quality || {};
    var cq = clientQuality(items);
    $('counts').textContent = '主表 ' + items.length + ' 条 / 候选池 ' + pool.length + ' 条';
    var qm = q['指标'] || {};
    var pct = function (name, fallback) {
      return (qm[name] ? Math.round(qm[name]['实测'] * 1000) / 10 : Number(fallback)).toFixed(1);
    };
    var srcCount = function (arr) {
      var m = {};
      (arr || []).forEach(function (it) {
        (it.sources || (it.source ? [it.source] : [])).forEach(function (s) {
          if (s) m[String(s)] = 1;
        });
      });
      return Object.keys(m).length;
    };
    var parts = [];
    parts.push('来源 ' + srcCount(items) + ' 个（候选池 ' + srcCount(pool) + ' 个）');
    parts.push('摘要有效 ' + pct('摘要有效率', cq['摘要有效率']) + '%');
    parts.push('分类一致 ' + pct('分类一致率', cq['分类一致率']) + '%');
    parts.push('重复议题 ' + pct('议题重复率', cq['退市议题占比']) + '%');
    if (qm['实时源触达率']) parts.push('实时源触达 ' + pct('实时源触达率', 0) + '%');
    var WHITE = ['渠道触达率', '端点配置率'];
    var failed = q['未达标项'] || [];
    var contentFail = failed.filter(function (k) { return WHITE.indexOf(k) < 0; });
    var whiteFail = failed.filter(function (k) { return WHITE.indexOf(k) >= 0; });
    if (contentFail.length) {
      parts.push('⚠ 本批未达标 ' + contentFail.length + ' 项：' + contentFail.map(function (k) {
        return k + ' ' + pct(k, 0) + '%';
      }).join(' / '));
    } else parts.push('内容质量达标');
    var layers = q['白名单分层'] || {};
    var covN = q['白名单覆盖渠道'] || 0;
    var total = q['白名单渠道总数'] || q['渠道白名单'] || 0;
    var blocked = 0;
    Object.keys(layers).forEach(function (k) {
      if (k.indexOf('网络出口拦截') >= 0) blocked += layers[k];
    });
    if (total) {
      parts.push('v3清单 ★必采 ' + (q['星必采已覆盖'] || 0) + '/' +
                 (q['星必采总数'] || 0) + ' 已覆盖 · 白名单 ' + covN + '/' + total +
                 ' 可直连 · 本环境拦截 ' + blocked + '（换网络自动启用）');
    } else if (whiteFail.length) {
      parts.push('白名单完备度待补（' + whiteFail.join('、') + '）');
    }
    var soc = ((e.live_sources || {})['社交热榜']) || {};
    if (soc['启用']) {
      parts.push('微博热搜 ' + (soc['快照条数'] || 0) + ' 条线索（仅热度参考·不进主表）');
    }
    if (q['贴近度档位']) parts.push('贴近度 ' + q['贴近度档位'] + ' 档（SKILL §4.1.3）');
    var qbox = $('quality');
    qbox.textContent = '数据质量：' + parts.join(' · ');
    qbox.className = 'quality' + (contentFail.length ? '' : ' ok');

    var html = items.map(function (it, i) {
      var a = abstractState(it), ok = a.indexOf('有效') === 0;
      var abs = (it.paragraphs || []).join(' ').trim();
      return '<tr><td>' + (it.order || i + 1) + '</td>' +
        '<td>' + esc(it.title) + '</td>' +
        '<td>' + esc((it.sources || []).join(' · ')) + '</td>' +
        '<td>' + esc(it.group) + '</td>' +
        '<td>' + esc(it.tag) + '</td>' +
        '<td>' + esc(it.grade) + '</td>' +
        '<td class="abs' + (ok ? '' : ' bad') + '">' + esc(abs || a) + '</td></tr>';
    }).join('');
    $('table').querySelector('tbody').innerHTML = html;
    $('empty').hidden = items.length > 0;
  }

  function params() {
    var s = normTime($('start').value), e2 = normTime($('end').value);
    if (!/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(s) || !/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(e2)) {
      throw new Error('请填写完整的窗口起止时间');
    }
    if (s >= e2) throw new Error('结束必须晚于开始');
    return { start: s, end: e2, interval: $('interval').value };
  }

  function refreshData() {
    var u = './data/' + (CONFIG.latest_json || 'latest.json') + '?t=' + Date.now();
    return fetch(u, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (j) {
      state.edition = j;
      DATA.editions[0] = j;
      render();
      var a = $('download');
      a.href = './downloads/' + (CONFIG.latest_xlsx || 'latest.xlsx') + '?t=' + Date.now();
      return j;
    });
  }

  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    var ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (err) { /* noop */ }
    document.body.removeChild(ta);
    return Promise.resolve();
  }

  // ---- 运行日志（默认折叠，展开才请求；静态 Pages 下如实提示） ----
  var logState = { rows: [], current: '', text: '', loaded: false };

  function logFallback(msg) {
    $('logList').innerHTML = '<div class="log-item"><span class="tag bad">静态页面</span> ' +
      esc(msg || '未检测到本地日志服务（/api/logs）。静态 Pages 下日志在 GitHub Actions 运行页与 ' +
        'news-run-logs-&lt;run_id&gt; artifact，页面不落盘日志。') + '</div>';
    $('logText').hidden = true;
  }

  function renderLogList(rows) {
    if (!rows || !rows.length) {
      $('logList').textContent = '暂无运行记录。点「重跑并刷新」后会出现。';
      return;
    }
    $('logList').innerHTML = rows.map(function (r) {
      var good = r.status === 'ok';
      var win = r.window || '';
      var iv = r.interval_name || r.interval || '';
      var cnt = (r.candidate == null ? '—' : r.candidate) + ' → ' + (r.main == null ? '—' : r.main);
      var t = String(r.started || '').replace('T', ' ').slice(0, 19);
      var el = r.elapsed_seconds == null ? '' : (r.elapsed_seconds + 's');
      return '<div class="log-item' + (r.run_id === logState.current ? ' active' : '') +
        '" data-rid="' + esc(r.run_id || '') + '">' +
        '<span class="tag ' + (good ? 'ok' : 'bad') + '">' + (good ? '成功' : '失败') + '</span> ' +
        '<code>' + esc(r.run_id || '') + '</code> · ' + esc(t) + ' · ' + esc(win) +
        ' · ' + esc(iv) + ' · ' + esc(cnt) + ' 条 · ' + esc(el) +
        (r.error ? ' · <span class="log-err">' + esc(String(r.error).slice(0, 120)) + '</span>' : '') +
        '</div>';
    }).join('');
    [].forEach.call($('logList').querySelectorAll('.log-item[data-rid]'), function (el) {
      el.addEventListener('click', function () { openLog(el.getAttribute('data-rid')); });
    });
  }

  function openLog(rid) {
    if (!rid) return;
    fetch('/api/logs/' + encodeURIComponent(rid), { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (j) {
        logState.current = rid;
        logState.text = j.text || JSON.stringify(j, null, 2);
        $('logText').hidden = false;
        $('logText').textContent = logState.text;
        $('logDownload').href = '/api/logs/' + encodeURIComponent(rid) + '/download';
        $('logText').scrollTop = $('logText').scrollHeight;
        renderLogList(logState.rows);
      })
      .catch(function (e) {
        $('logText').hidden = false;
        $('logText').textContent = '日志读取失败：' + e.message;
      });
  }

  function loadLogs(autoRid) {
    if (location.protocol === 'file:') { logFallback('本地 file:// 预览不提供日志接口。'); return; }
    fetch('/api/logs?limit=12', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (j) {
        logState.rows = j.logs || [];
        logState.loaded = true;
        renderLogList(logState.rows);
        var rid = autoRid || (logState.rows[0] && logState.rows[0].run_id);
        if (rid) openLog(rid);
      })
      .catch(function () { logFallback(); });
  }

  function showLogs(autoRid, forceOpen) {
    var panel = $('logPanel');
    if (forceOpen) panel.open = true;
    if (panel.open) loadLogs(autoRid);
  }

  function ghCommand(p) {
    return 'gh workflow run daily-news.yml -f window_start="' + p.start +
      '" -f window_end="' + p.end + '" -f window_interval=' + p.interval;
  }

  function runLocal(p) {
    return fetch('/api/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start: p.start, end: p.end, interval: p.interval,
        source: CONFIG.source_mode || 'target', rebuild_site: true
      })
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.j.ok) throw new Error(res.j.error || '运行失败');
        state.lastRunId = res.j.run_id || '';
        return res.j;
      });
  }

  function ghConfig() {
    var v = function (id) { var e = $(id); return e && e.value ? e.value.trim() : ''; };
    var g = { owner: v('owner'), repo: v('repo'), branch: v('branch') || 'main',
              token: v('token') };
    var r = repoInfo();
    if ((!g.owner || !g.repo) && r.slug && r.slug.indexOf('/') > 0) {
      g.owner = r.slug.split('/')[0]; g.repo = r.slug.split('/').slice(1).join('/');
      g.branch = g.branch || r.branch;
    }
    return g;
  }

  function ghFetch(url, token, opt) {
    var o = opt || {};
    o.headers = Object.assign({
      'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
      'Authorization': 'Bearer ' + token
    }, o.headers || {});
    return fetch(url, o);
  }

  function runActions(p, g) {
    var api = 'https://api.github.com/repos/' + encodeURIComponent(g.owner) + '/' + encodeURIComponent(g.repo);
    var t0 = new Date().toISOString();
    return ghFetch(api + '/actions/workflows/daily-news.yml/dispatches', g.token, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref: g.branch, inputs: {
        window_start: p.start, window_end: p.end, window_interval: p.interval
      } })
    }).then(function (r) {
      if (r.status !== 204 && !r.ok) {
        return r.text().then(function (t) { throw new Error('触发失败 HTTP ' + r.status + '：' + t.slice(0, 200)); });
      }
      setStatus('已触发 GitHub Actions，等待运行完成…', 'run');
      var tries = 0;
      function findRun() {
        return ghFetch(api + '/actions/workflows/daily-news.yml/runs?event=workflow_dispatch&per_page=5', g.token)
          .then(function (r) { return r.json(); }).then(function (j) {
            var runs = (j.workflow_runs || []).filter(function (x) { return x.created_at >= t0; });
            if (runs.length) return runs[0];
            if (++tries > 24) throw new Error('未找到刚触发的运行，请到 Actions 页确认');
            return new Promise(function (res) { setTimeout(res, 5000); }).then(findRun);
          });
      }
      return findRun().then(function (run) {
        state.runUrl = run.html_url;
        var n = 0;
        function check() {
          return ghFetch(api + '/actions/runs/' + run.id, g.token)
            .then(function (r) { return r.json(); }).then(function (j) {
              setStatus('运行中：' + (j.status || 'queued') + '（' + (++n) + '）…', 'run');
              if (j.status === 'completed') {
                if (j.conclusion !== 'success') throw new Error('工作流结论为 ' + j.conclusion);
                return { ok: true };
              }
              if (n > 90) throw new Error('等待超过 7 分钟，请到 Actions 页查看');
              return new Promise(function (res) { setTimeout(res, 5000); }).then(check);
            });
        }
        return check();
      });
    });
  }

  // 仓库坐标来自 build_site.py 写入的 latest.json / NEWS_CONFIG，用户不用填任何东西
  function repoInfo() {
    var r = (state.edition && state.edition.repo) || {};
    var slug = r.github_repo || CONFIG.github_repo || '';
    var wf = r.workflow_url || CONFIG.workflow_url ||
      (slug ? 'https://github.com/' + slug + '/actions/workflows/daily-news.yml' : '');
    return { slug: slug, workflow_url: wf,
             actions_url: r.actions_url || CONFIG.actions_url || '',
             branch: r.branch || CONFIG.branch || 'main' };
  }

  // 静态 Pages 无需 Token：打开 Actions 运行页后轮询固定 latest.json，
  // 一旦生成时间变化就自动刷新表格与下载链接（GitHub 也无需在页面里放任何密钥）。
  function waitForNewBatch(before) {
    var tries = 0;
    function poll() {
      return fetch('./data/' + (CONFIG.latest_json || 'latest.json') + '?t=' + Date.now(),
                   { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .catch(function () { return null; })
        .then(function (j) {
          if (j && (j.generated_at || '') !== before) return j;
          setStatus('等待新批次…（' + (++tries) + '）在刚打开的 Actions 页点「Run workflow」，' +
            '约 3–6 分钟后本页自动刷新；期间可关闭此页。', 'run');
          if (tries > 100) throw new Error('等待超过 10 分钟，请到 Actions 页确认运行状态');
          return new Promise(function (res) { setTimeout(res, 6000); }).then(poll);
        });
    }
    return poll();
  }

  function rerun() {
    if (state.busy) return;
    var p;
    try { p = params(); } catch (e) { setStatus('⚠ ' + e.message, 'err'); return; }
    state.busy = true; $('rerun').disabled = true;

    // 只有本地 serve.py 才有 /api/status；Pages 上直接跳过，不发探测请求
    var local = (location.protocol.indexOf('http') === 0 && location.hostname !== 'github.io')
      ? fetch('/api/status', { cache: 'no-store' }).then(function (r) {
          return r.ok ? r.json() : null;
        }).then(function (j) { return !!(j && j.ok); }).catch(function () { return false; })
      : Promise.resolve(false);

    local.then(function (hasLocal) {
      if (hasLocal) {
        setStatus('本地服务运行中，正在执行流水线…', 'run');
        return runLocal(p).then(function () { return 'local'; });
      }
      var g = ghConfig();
      // 可选：用户自愿填了 Token → 页面内直接触发并等待完成（不是必需）
      if (g.token && g.owner && g.repo) {
        return runActions(p, g).then(function () { return 'actions'; });
      }
      // 主路径：零配置，不需要 owner / repo / Token
      var r = repoInfo();
      var before = state.edition ? (state.edition.generated_at || '') : '';
      if (!r.workflow_url) {
        var cmd = ghCommand(p);
        copy(cmd);
        setStatus('未识别仓库坐标：已复制补跑命令 <code>' + esc(cmd) +
          '</code>，粘贴到终端执行即可；或在 config.yaml 填 site.github_repo。', 'err');
        return 'copied';
      }
      try { window.open(r.workflow_url, '_blank', 'noopener'); } catch (e) { /* 弹窗被拦则给链接 */ }
      setStatus('已打开 <a href="' + esc(r.workflow_url) + '" target="_blank" rel="noopener">GitHub Actions 运行页</a>：' +
        '点「Run workflow」→ 分支 <code>' + esc(r.branch) + '</code> → 运行。<br>' +
        '<b>无需任何配置 / Token</b>；本页会自动检测新批次并刷新数据与下载。', 'run');
      return waitForNewBatch(before).then(function (j) {
        state.edition = j;
        DATA.editions[0] = j;
        render();
        $('download').href = './downloads/' + (CONFIG.latest_xlsx || 'latest.xlsx') + '?t=' + Date.now();
        return 'actions-poll';
      });
    }).then(function (how) {
      if (how === 'copied') return;
      if (how === 'actions-poll') {
        setStatus('✅ 新批次已上线，窗口 ' + esc(p.start) + ' → ' + esc(p.end) +
          '，表格与「下载 Excel」已自动指向本期文件。', 'ok');
        return;
      }
      return refreshData().then(function () {
        setStatus('✅ 已完成：窗口 ' + esc(p.start) + ' → ' + esc(p.end) +
          '，数据已刷新，点「下载 Excel」取本期文件。' +
          (state.lastRunId ? ' 日志已展开（' + esc(state.lastRunId) + '）。' : '') +
          (state.runUrl ? ' <a href="' + esc(state.runUrl) + '" target="_blank" rel="noopener">查看 Actions 运行页</a>' : ''), 'ok');
        showLogs(state.lastRunId, true);
      });
    }).catch(function (e) {
      setStatus('❌ ' + esc(e.message) +
        (state.lastRunId ? ' · run_id ' + esc(state.lastRunId) + '，日志已自动展开到错误尾部' : '') +
        (state.runUrl ? ' · <a href="' + esc(state.runUrl) + '" target="_blank" rel="noopener">查看运行日志</a>' : ''), 'err');
      showLogs(state.lastRunId, true);
    }).then(function () {
      state.busy = false; $('rerun').disabled = false;
    });
  }

  function bind() {
    $('rerun').addEventListener('click', rerun);
    $('logPanel').addEventListener('toggle', function () {
      if ($('logPanel').open && !logState.loaded) loadLogs();
    });
    $('logRefresh').addEventListener('click', function () { loadLogs(logState.current); });
    $('logCopy').addEventListener('click', function () {
      if (!logState.text) { setStatus('日志正文为空，先展开并点选一条运行。', 'err'); return; }
      copy(logState.text).then(function () { setStatus('已复制本次运行日志全文。', 'ok'); });
    });
    $('copySchedule').addEventListener('click', function () {
      var t = $('runat').value || '20:00';
      var span = Math.round((new Date('1970-01-01T' + $('end').value) -
        new Date('1970-01-01T' + $('start').value)) / 3600000) || 12;
      var cmd = 'python scripts/set_schedule.py ' + t + ' --window ' + span +
        ' --interval ' + $('interval').value;
      copy(cmd).then(function () {
        setStatus('已复制改定时命令：<code>' + esc(cmd) + '</code>（在仓库根目录执行后提交推送）', 'ok');
      });
    });
    try {
      var saved = JSON.parse(sessionStorage.getItem('news-gh') || '{}');
      ['owner', 'repo', 'branch', 'token'].forEach(function (k) {
        if (saved[k] && $(k)) $(k).value = saved[k];
      });
    } catch (e) { /* noop */ }
    ['owner', 'repo', 'branch', 'token'].forEach(function (k) {
      $(k).addEventListener('change', function () {
        try {
          sessionStorage.setItem('news-gh', JSON.stringify({
            owner: $('owner').value, repo: $('repo').value,
            branch: $('branch').value, token: $('token').value
          }));
        } catch (e) { /* noop */ }
      });
    });
    if (DATA.site && DATA.site.fixed && location.protocol !== 'file:') {
      $('download').href = './downloads/' + (CONFIG.latest_xlsx || 'latest.xlsx');
    } else if (location.protocol === 'file:') {
      $('download').href = 'docs/downloads/' + (CONFIG.latest_xlsx || 'latest.xlsx');
    }
    render();
  }

  bind();
})();
