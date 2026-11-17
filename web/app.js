/* ==========================================================
   BiliBox 前端逻辑
   ========================================================== */
(function () {
  'use strict';

  // ---------------- 工具 ----------------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const state = {
    parsed: null,
    selected: new Set(),
    filter: '',
    jobs: [],
    config: {},
    status: {},
    collapsed: new Set(),
    detailOpen: new Set(),
    jobsTimer: null,
    qrTimer: null,
    renderedCount: 0,
    pageSize: 60,
  };

  function api(path, opts) {
    return fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts))
      .then(async (r) => {
        const js = await r.json().catch(() => ({ ok: false, error: '响应异常' }));
        if (!js.ok && js.error) throw new Error(js.error);
        return js;
      });
  }

  function post(path, data) {
    return api(path, { method: 'POST', body: JSON.stringify(data || {}) });
  }

  function toast(msg, type) {
    const wrap = $('#toastWrap');
    const el = document.createElement('div');
    el.className = 'toast' + (type ? ' ' + type : '');
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(() => {
      el.style.transition = 'opacity .25s, transform .25s';
      el.style.opacity = '0';
      el.style.transform = 'translateY(6px)';
      setTimeout(() => el.remove(), 300);
    }, 2600);
  }

  function outputMode() {
    const on = document.querySelector('#outSeg .seg-btn.on');
    return on ? on.dataset.out : 'video';
  }

  function fmtDur(sec) {
    sec = Math.round(sec || 0);
    if (sec < 60) return sec + '秒';
    const m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return s ? `${m}分${s}秒` : `${m}分钟`;
    const h = Math.floor(m / 60);
    return `${h}小时${m % 60}分`;
  }

  function fmtSize(bytes) {
    if (!bytes) return '';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0, n = bytes;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(n >= 100 ? 0 : 1) + ' ' + u[i];
  }

  function fmtSpeed(bps) {
    if (!bps) return '';
    return fmtSize(bps) + '/s';
  }

  function esc(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // ---------------- 环境状态 ----------------
  async function loadStatus() {
    try {
      const st = await api('/api/status');
      state.status = st;
      // 版本号只有 APP_VER 这一个真相源：徽标从接口取，别再往 HTML 里写死
      // （之前就是写死在 index.html 里，改了 APP_VER 徽标还停在 2.0）
      const bv = $('#brandVer');
      if (bv && st.version) bv.textContent = st.version;
      const pill = $('#envPill');
      const ok = st.ffmpeg;
      pill.className = 'status-dot ' + (ok ? 'is-ok' : 'is-warn');
      pill.querySelector('span').textContent = ok ? '就绪' : '缺少 ffmpeg';
      pill.title = ok
        ? `ffmpeg 就绪 · Python ${st.python}`
        : '未检测到 ffmpeg，音视频无法合并，请检查 bin 目录';

      const chip = $('#userChip');
      const u = st.user || {};
      if (u.is_login) {
        chip.classList.add('logged');
        chip.querySelector('span').textContent = u.uname || '已登录';
        chip.title = (u.vip ? '大会员 · ' : '') + '已登录，可下载 1080P 及以上';
      } else {
        chip.classList.remove('logged');
        chip.querySelector('span').textContent = '未登录';
        chip.title = '点击扫码登录，解锁 1080P 及以上画质';
      }
      $('#envBox').textContent =
        `Python      ${st.python}\n` +
        `ffmpeg      ${st.ffmpeg_path || '未找到'}\n` +
        `下载目录    ${st.download_dir}\n` +
        `运行平台    ${st.platform}`;
      return st;
    } catch (e) {
      console.warn(e);
    }
  }

  // ---------------- 解析 ----------------
  async function doParse(url) {
    const input = $('#urlInput');
    url = (url || input.value || '').trim();
    if (!url) { toast('请先粘贴 B站 链接', 'err'); return; }

    const btn = $('#parseBtn');
    btn.disabled = true;
    btn.querySelector('.btn-label').textContent = '解析中';
    btn.querySelector('.spinner').hidden = false;

    try {
      const res = await api('/api/parse', { method: 'POST', body: JSON.stringify({ url }) });
      state.parsed = res;
      // 默认只勾「当前画质下还没下够」的；全都够数才退成全选，方便重下
      const freshIdx = res.items.map((_, i) => i).filter((i) => dlFlags(res.items[i]).state !== 'yes');
      state.selected = new Set(freshIdx.length ? freshIdx : res.items.map((_, i) => i));
      const nOld = res.items.filter((x) => dlFlags(x).state === 'yes').length;
      if (nOld) $('#selNewBtn').hidden = false;
      // 合集较多时只展开第一个，其余折叠成组头
      const gs = [...new Set(res.items.map((it) => it.group || ''))].filter(Boolean);
      state.collapsed = new Set(gs.slice(1));
      $('#filterInput').value = '';
      renderResult();
      $('#resultCard').hidden = false;
      $('#resultCard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      toast(`已解析到 ${res.total} 个条目`);
    } catch (e) {
      toast(e.message || '解析失败', 'err');
    } finally {
      btn.disabled = false;
      btn.querySelector('.btn-label').textContent = '解析';
      btn.querySelector('.spinner').hidden = true;
    }
  }

  const TYPE_LABEL = {
    video: '单视频',
    video_pages: '多分P',
    season: 'UP主合集',
    series: 'UP主系列',
    space: 'UP主投稿',
    favlist: '收藏夹',
    bangumi: '番剧',
    watchlater: '稍后再看',
    history: '观看历史',
  };

  function renderResult() {
    const p = state.parsed;
    if (!p) return;

    const cover = $('#resCover');
    if (p.cover) {
      cover.src = '/api/cover?url=' + encodeURIComponent(p.cover);
      cover.style.visibility = 'visible';
    } else {
      cover.removeAttribute('src');
      cover.style.visibility = 'hidden';
    }

    $('#resType').textContent = TYPE_LABEL[p.type] || '内容';
    $('#resTitle').textContent = p.title || '未命名';

    const owner = $('#resOwner');
    owner.textContent = p.owner || '';
    owner.style.display = p.owner ? '' : 'none';
    $('#resDot').style.display = p.owner ? '' : 'none';

    $('#resCount').textContent = `共 ${p.total} 个${p.type === 'bangumi' ? '话' : '条目'}`;

    const desc = $('#resDesc');
    const cleanDesc = (p.desc || '').replace(/\s+/g, ' ').trim();
    const skipDesc = !cleanDesc || cleanDesc === '-' || cleanDesc.length < 4;
    desc.textContent = skipDesc ? '' : cleanDesc;
    desc.style.display = skipDesc ? 'none' : '';

    // 合集提示
    const tip = $('#seasonTip');
    if (p.season && p.season.count > 1) {
      tip.hidden = false;
      $('#seasonTipText').textContent =
        `此视频属于合集《${p.season.title}》，共 ${p.season.count} 集`;
      $('#seasonTakeBtn').onclick = () => {
        state.parsed = {
          ok: true, platform: 'bilibili', type: 'season',
          title: p.season.title, cover: p.cover, owner: p.owner, desc: '',
          items: p.season.items, total: p.season.items.length,
          url: p.url,
        };
        state.selected = new Set(state.parsed.items.map((_, i) => i));
        state.renderedCount = 0;
        renderResult();
        toast('已切换为整个合集');
      };
    } else {
      tip.hidden = true;
    }

    state.renderedCount = 0;
    $('#itemList').innerHTML = '';
    renderItems(true);
    updateSelInfo();
  }

  function visibleItems() {
    const p = state.parsed;
    if (!p) return [];
    const kw = state.filter.trim().toLowerCase();
    return p.items
      .map((it, i) => ({ it, i }))
      .filter(({ it }) => !kw || (it.title || '').toLowerCase().includes(kw));
  }

  // 「已下载」要跟着当前选的画质走。
  // 720P 下过的条目在 4K 模式下不算已下载，否则用户切到 4K 发现一条都没勾上，
  // 手动勾了又会被后端当成「已存在」秒跳过——就是这次报的那个 bug。
  // 老记录没有 dl_max_h 字段，按「已下载」处理，不打扰已经下过的人。
  function dlFlags(it) {
    if (!it.downloaded) return { state: 'no', text: '' };
    const had = parseInt(it.dl_max_h || 0, 10);
    const want = parseInt($('#optQuality').value, 10) || 1080;
    if (!had || had === want) return { state: 'yes', text: '已下载' };
    // 角标位置窄，只取画质那一截："480P 清晰 · AVC1" → "480P"
    const qt = String(it.dl_qual_text || '').split(/[\s·]/)[0] || (had + 'P');
    return { state: 'stale', text: '已下 ' + qt };
  }

  function makeRow(it, i, grouped, label) {
    const row = document.createElement('li');
    const fl = dlFlags(it);
    row.className = 'item-row' + (state.selected.has(i) ? ' checked' : '') + (grouped ? ' grouped' : '') +
      (fl.state === 'yes' ? ' downloaded' : '');
    row.dataset.idx = i;
    const num = label != null ? label : (it.top_index || it.index || (i + 1));
    row.innerHTML =
      `<input type="checkbox" ${state.selected.has(i) ? 'checked' : ''}>` +
      `<span class="item-idx">${num}</span>` +
      `<span class="item-title" title="${esc(it.title)}">${esc(it.title)}</span>` +
      `<span class="item-badge${fl.state === 'stale' ? ' stale' : ''}" ${fl.state === 'no' ? 'hidden' : ''}>${esc(fl.text || '已下载')}</span>` +
      `<span class="item-dur">${it.duration ? fmtDur(it.duration) : ''}</span>`;
    return row;
  }

  function renderItems(reset) {
    const list = $('#itemList');
    list.innerHTML = '';
    const vis = visibleItems();

    if (!vis.length) {
      list.innerHTML = `<li class="item-row"><span class="item-title" style="color:var(--text-3)">没有匹配的条目</span></li>`;
      return;
    }

    // 按来源分组：同一合集/多分P 折叠成一块，独立视频单列
    const groups = [];
    const map = new Map();
    for (const pair of vis) {
      const g = pair.it.group || '';
      if (!map.has(g)) {
        const obj = { key: g, items: [] };
        map.set(g, obj);
        groups.push(obj);
      }
      map.get(g).items.push(pair);
    }

    const frag = document.createDocumentFragment();
    for (const g of groups) {
      if (!g.key) {
        for (const { it, i } of g.items) frag.appendChild(makeRow(it, i, false));
        continue;
      }
      const selCount = g.items.filter((x) => state.selected.has(x.i)).length;
      const allSel = selCount === g.items.length;
      const collapsed = state.collapsed.has(g.key);
      const head = document.createElement('li');
      head.className = 'group-head';
      head.dataset.group = g.key;
      head.innerHTML =
        `<input type="checkbox" data-gsel="1" ${allSel ? 'checked' : ''}>` +
        `<span class="group-toggle">${collapsed ? '▸' : '▾'}</span>` +
        `<span class="group-name" title="${esc(g.key)}">${esc(g.key)}</span>` +
        `<span class="group-count">${g.items.length} 集${selCount && !allSel ? ' · 已选 ' + selCount : ''}</span>`;
      if (selCount > 0 && !allSel) head.querySelector('input').indeterminate = true;
      frag.appendChild(head);
      if (!collapsed) {
        let n = 0;
        for (const { it, i } of g.items) {
          n += 1;
          frag.appendChild(makeRow(it, i, true, n));
        }
      }
    }
    list.appendChild(frag);
    updateSelInfo();
  }

  function updateSelInfo() {
    const total = state.parsed ? state.parsed.items.length : 0;
    $('#selInfo').textContent = `已选 ${state.selected.size} / ${total}`;
    const all = state.selected.size === total && total > 0;
    $('#checkAll').checked = all;
    $('#checkAll').indeterminate = state.selected.size > 0 && !all;
    $('#startBtn').disabled = state.selected.size === 0;
    updateEstimate();
  }

  function estimateMB() {
    const p = state.parsed;
    if (!p) return 0;
    const q = parseInt($('#optQuality').value, 10);
    // 按实测码率粗估：480P≈0.07MB/s，720P≈0.13，1080P≈0.26
    // 没登录时最高只能拿 720P，预估也按 720P 封顶，不然会高估一大截
    const login = (state.status.user || {}).is_login;
    const cap = login ? q : Math.min(q, 720);
    const rateMap = { 2160: 1.0, 1080: 0.26, 720: 0.13, 480: 0.07, 360: 0.04 };
    const rate = rateMap[cap] || 0.13;
    let dur = 0;
    for (let i = 0; i < p.items.length; i++) {
      if (state.selected.has(i)) dur += (p.items[i].duration || 0);
    }
    return outputMode() === 'audio' ? dur * 0.02 : dur * rate;
  }

  function updateEstimate() {
    const p = state.parsed;
    if (!p) return;
    let dur = 0;
    for (let i = 0; i < p.items.length; i++) {
      if (state.selected.has(i)) dur += (p.items[i].duration || 0);
    }
    const est = estimateMB();
    $('#estText').textContent = state.selected.size
      ? `预计 ${fmtSize(est * 1024 * 1024)} · ${fmtDur(dur)}`
      : '';
  }

  // ---------------- 创建任务 ----------------
  async function startDownload() {
    if (!state.parsed || !state.selected.size) return;
    const p = state.parsed;
    const qh = parseInt($('#optQuality').value, 10);
    const u = (state.status || {}).user || {};
    if (qh > 720 && !u.is_login) {
      const go = confirm('未登录最高只能下 720P（你选的是 ' + qh + 'P）。\n\n点「确定」按 720P 先下，点「取消」去扫码登录。');
      if (!go) { openLogin(); return; }
    }
    const estMB = estimateMB();
    if (estMB > 4000) {
      const okGo = confirm(`这次一共 ${state.selected.size} 个条目，预计约 ${(estMB / 1024).toFixed(1)} GB，确定开始下载吗？`);
      if (!okGo) return;
    }

    const selected = Array.from(state.selected).sort((a, b) => a - b);
    const options = {
      selected,
      max_height: parseInt($('#optQuality').value, 10),
      codec: $('#optCodec').value,
      audio: $('#optAudio').value,
      cover: $('#optCover').checked,
      subtitle: $('#optSub').checked,
      danmaku: $('#optDan').checked,
      audio_only: outputMode() === 'audio',
      sub_dir: p.items.length > 1,
    };

    const btn = $('#startBtn');
    btn.disabled = true;
    try {
      const res = await post('/api/jobs', { parsed: p, options });
      toast(`已加入下载队列（${selected.length} 项）`);
      switchTab('jobs');
      refreshJobs();
      setTimeout(() => {
        const el = $('#jobsPanel');
        if (el) {
          const top = el.getBoundingClientRect().top + window.scrollY - 76;
          window.scrollTo({ top, behavior: 'smooth' });
        }
      }, 260);
    } catch (e) {
      toast(e.message || '创建任务失败', 'err');
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------- 任务列表 ----------------
  const JOB_ICONS = {
    queued: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="8" stroke="currentColor" stroke-width="1.6" stroke-dasharray="3 3"/></svg>',
    running: '<svg viewBox="0 0 24 24" fill="none"><path d="M12 4v4m0 8v4m8-8h-4M8 12H4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="1.4s" repeatCount="indefinite"/></path></svg>',
    done: '<svg viewBox="0 0 24 24" fill="none"><path d="M5 12.5 10 17.5 19 8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    error: '<svg viewBox="0 0 24 24" fill="none"><path d="M12 7.5v5.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="12" cy="16.5" r="1" fill="currentColor"/><circle cx="12" cy="12" r="8" stroke="currentColor" stroke-width="1.6"/></svg>',
    cancelled: '<svg viewBox="0 0 24 24" fill="none"><path d="M8 8l8 8M16 8l-8 8" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  };

  const JOB_TEXT = {
    queued: '排队中', running: '下载中', done: '已完成', error: '有失败', cancelled: '已取消',
    interrupted: '已中断',
  };

  async function refreshJobs() {
    try {
      const res = await api('/api/jobs');
      state.jobs = res.jobs || [];
      renderJobs();
    } catch (e) { /* 静默 */ }
  }

  function renderJobs() {
    const list = $('#jobList');
    const empty = $('#jobsEmpty');
    const jobs = state.jobs;

    if (!jobs.length) {
      empty.hidden = false;
      list.innerHTML = '';
      $('#jobBadge').hidden = true;
      return;
    }
    empty.hidden = true;

    const active = jobs.filter(j => j.status === 'running' || j.status === 'queued').length;
    const badge = $('#jobBadge');
    if (active) { badge.hidden = false; badge.textContent = active; }
    else badge.hidden = true;

    const existing = new Map();
    Array.from(list.children).forEach(el => existing.set(el.dataset.id, el));

    jobs.forEach((job, idx) => {
      let card = existing.get(job.id);
      if (!card) {
        card = document.createElement('div');
        card.className = 'job-card';
        card.dataset.id = job.id;
        list.appendChild(card);
      }
      renderJobCard(card, job);
      if (list.children[idx] !== card) list.insertBefore(card, list.children[idx] || null);
    });
    // 移除已清理的
    const ids = new Set(jobs.map(j => j.id));
    Array.from(list.children).forEach(el => {
      if (!ids.has(el.dataset.id)) el.remove();
    });
  }

  function buildFoot(job, cur, prog) {
    const parts = [];
    if (job.status === 'running') {
      if (cur.stage) parts.push(`<span class="stage">${esc(cur.stage)}</span>`);
      if (prog.speed) parts.push(fmtSpeed(prog.speed));
      if (prog.eta && prog.eta > 0) parts.push(`剩 ${fmtDur(prog.eta)}`);
      if (prog.total > 0) {
        const shown = Math.min(prog.bytes || 0, prog.total);
        parts.push(`${fmtSize(shown)} / ${fmtSize(prog.total)}`);
      } else if (prog.bytes) {
        parts.push(fmtSize(prog.bytes));
      }
    } else if (job.status === 'done') {
      parts.push(`共 ${job.total} 个文件 · ${job.created_text}`);
    } else if (job.status === 'interrupted') {
      parts.push(`<span>${esc(job.error || '上次没有下完，点「继续下载」接着走')}</span>`);
    } else if (job.status === 'error') {
      const err = (job.items || []).find(i => i.status === 'error');
      parts.push(`<span class="err">${esc((err && err.error) || job.error || '下载失败')}</span>`);
    } else {
      parts.push(job.created_text);
    }
    return parts.join(' <span>·</span> ');
  }

  function buildJobActions(card, job) {
    const box = card.querySelector('.job-actions');
    if (!box) return;
    box.innerHTML =
      (job.total > 1 ? `<button class="ghost-btn" data-act="detail">${state.detailOpen.has(job.id) ? '收起' : '明细'}</button>` : '') +
      (job.status === 'running' || job.status === 'queued'
        ? `<button class="ghost-btn danger" data-act="cancel">取消</button>`
        : job.status === 'done'
          ? `<button class="ghost-btn" data-act="open">打开文件夹</button>`
          : job.status === 'interrupted'
            ? `<button class="ghost-btn primary" data-act="retry">继续下载</button><button class="ghost-btn" data-act="open">打开文件夹</button>`
            : `<button class="ghost-btn" data-act="retry">重试</button><button class="ghost-btn" data-act="open">打开文件夹</button>`);
  }

  function renderJobCard(card, job) {
    // DOM 只建一次，之后只改字段：轮询重建会把点击和展开状态一起冲掉
    if (!card._built) {
      card.innerHTML = `
        <div class="job-top">
          <div class="job-state"></div>
          <div class="job-main">
            <div class="job-title"></div>
            <div class="job-sub">
              <span class="j-status"></span>
              <span class="sep">·</span>
              <span class="j-count"></span>
              <span class="sep j-cur-sep" hidden>·</span>
              <span class="j-cur" hidden></span>
            </div>
          </div>
          <div class="job-right"><span class="job-pct"></span></div>
        </div>
        <div class="job-bar"><i></i></div>
        <div class="job-foot">
          <span class="foot-info"></span>
          <div class="job-actions"></div>
        </div>
        <div class="job-detail"></div>`;
      card._built = true;
    }

    const cur = (job.items || []).find(i => i.status === 'running') || {};
    const prog = cur.progress || {};
    const pct = Math.max(0, Math.min(100, Number(job.percent) || 0));

    if (card._status !== job.status) {
      card._status = job.status;
      const st = card.querySelector('.job-state');
      st.className = 'job-state ' + job.status;
      st.innerHTML = JOB_ICONS[job.status] || '';
      buildJobActions(card, job);
    }

    const t = card.querySelector('.job-title');
    if (t.textContent !== job.title) t.textContent = job.title;
    t.title = job.title;

    card.querySelector('.j-status').textContent = JOB_TEXT[job.status] || job.status;
    card.querySelector('.j-count').textContent = `${job.done_count}/${job.total}`;

    const curEl = card.querySelector('.j-cur');
    const curSep = card.querySelector('.j-cur-sep');
    const showCur = job.status === 'running' && !!cur.title;
    curEl.hidden = !showCur;
    curSep.hidden = !showCur;
    if (showCur) {
      if (curEl.textContent !== cur.title) curEl.textContent = cur.title;
      curEl.title = cur.title;
    }

    const pctText = job.status === 'queued' ? '—' : Math.round(pct) + '%';
    const pctEl = card.querySelector('.job-pct');
    if (pctEl.textContent !== pctText) pctEl.textContent = pctText;

    const bar = card.querySelector('.job-bar');
    bar.className = 'job-bar' + (job.status === 'done' ? ' done' : job.status === 'error' ? ' error' : '');
    bar.querySelector('i').style.width = (job.status === 'queued' ? 0 : pct) + '%';

    const foot = card.querySelector('.foot-info');
    const footHtml = buildFoot(job, cur, prog);
    if (foot._h !== footHtml) { foot.innerHTML = footHtml; foot._h = footHtml; }

    const detail = card.querySelector('.job-detail');
    const open = job.total > 1 && state.detailOpen.has(job.id);
    detail.classList.toggle('show', open);
    if (open) {
      const dh = (job.items || []).map(it => {
        const label = { pending: '等待', running: '下载中', done: '完成', error: '失败', cancelled: '取消' }[it.status] || it.status;
        const canStop = it.status === 'pending' || it.status === 'running';
        return `<div class="detail-row ${it.status}"><span class="n">${it.index}</span><span class="t" title="${esc(it.title)}">${esc(it.title)}</span><span class="s">${label}${it.filesize ? ' · ' + fmtSize(it.filesize) : ''}</span>${canStop ? `<button class="mini-btn" data-item="${it.index}">取消</button>` : ''}</div>`;
      }).join('');
      if (detail._h !== dh) { detail.innerHTML = dh; detail._h = dh; }
    }
  }

  // 任务卡内事件（委托）
  $('#jobList').addEventListener('click', async (e) => {
    // 明细里单个文件取消
    const itemBtn = e.target.closest('[data-item]');
    if (itemBtn) {
      const card = itemBtn.closest('.job-card');
      try {
        await post(`/api/jobs/${card.dataset.id}/cancel_item`, { index: parseInt(itemBtn.dataset.item, 10) });
        toast('已取消这个文件');
        await refreshJobs();
      } catch (err) { toast(err.message, 'err'); }
      return;
    }
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const card = btn.closest('.job-card');
    const job = state.jobs.find(j => j.id === card.dataset.id);
    if (!job) return;
    const act = btn.dataset.act;

    if (act === 'detail') {
      if (state.detailOpen.has(job.id)) state.detailOpen.delete(job.id);
      else state.detailOpen.add(job.id);
      buildJobActions(card, job);
      renderJobCard(card, job);
    } else if (act === 'cancel') {
      try { await post(`/api/jobs/${job.id}/cancel`, {}); toast('已取消'); refreshJobs(); }
      catch (err) { toast(err.message, 'err'); }
    } else if (act === 'retry') {
      try { await post(`/api/jobs/${job.id}/retry`, {}); toast('已重新加入队列'); refreshJobs(); }
      catch (err) { toast(err.message, 'err'); }
    } else if (act === 'open') {
      try { await post('/api/open', {}); } catch (err) { toast(err.message, 'err'); }
    }
  });

  // ---------------- 文件列表 ----------------
  async function loadLibrary() {
    try {
      const res = await api('/api/library');
      $('#dirPath').textContent = res.root || '';
      const list = $('#fileList');
      const empty = $('#filesEmpty');
      const entries = res.entries || [];
      if (!entries.length) {
        empty.hidden = false;
        list.innerHTML = '';
        return;
      }
      empty.hidden = true;
      list.innerHTML = entries.map(en => {
        if (en.type === 'dir') {
          return `<li class="file-row dir">
            <svg class="file-ico" viewBox="0 0 24 24" fill="none"><path d="M4 7.5A2.5 2.5 0 0 1 6.5 5h3.2l1.6 2h6.2A2.5 2.5 0 0 1 19 9.5v7A2.5 2.5 0 0 1 16.5 19h-10A2.5 2.5 0 0 1 4 16.5v-9z" stroke="currentColor" stroke-width="1.5"/></svg>
            <span class="file-name">${esc(en.name)}</span>
            <span class="file-size">${en.count} 个文件</span>
          </li>`;
        }
        return `<li class="file-row">
          <svg class="file-ico" viewBox="0 0 24 24" fill="none"><path d="M6 3.5h7l5 5v12a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 20.5v-17z" stroke="currentColor" stroke-width="1.4"/><path d="M13 3.5v5h5" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>
          <span class="file-name">${esc(en.name)}</span>
          <span class="file-size">${fmtSize(en.size)}</span>
        </li>`;
      }).join('');
    } catch (e) { /* 静默 */ }
  }

  // ---------------- 标签页 ----------------
  function switchTab(name) {
    $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    $('#jobsPanel').hidden = name !== 'jobs';
    $('#filesPanel').hidden = name !== 'files';
    if (name === 'files') loadLibrary();
  }

  $$('.tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));

  // ---------------- 设置 ----------------
  async function loadConfig() {
    try {
      const res = await api('/api/config');
      state.config = res.config || {};
      const c = state.config;
      $('#setDir').value = c.download_dir || '';
      document.querySelectorAll('#setLayout button').forEach(
        b => b.classList.toggle('on', b.dataset.layout === (c.output_layout || 'flat')));
      $('#setConc').value = String(c.concurrency || 2);
      $('#setThreads').value = String(c.threads_per_file || 8);
      $('#setProxy').value = c.proxy || '';
      $('#setCookie').value = c.cookie || '';
      $('#optQuality').value = String(c.max_height || 1080);
      $('#optCodec').value = c.codec_pref || 'avc';
      $('#optAudio').value = c.audio_quality || 'best';
      $('#optCover').checked = c.download_cover !== false;
      $('#optSub').checked = !!c.download_subtitle;
      $('#optDan').checked = !!c.download_danmaku;
    } catch (e) { console.warn(e); }
  }

  async function saveSettings() {
    const payload = {
      download_dir: $('#setDir').value.trim(),
      concurrency: parseInt($('#setConc').value, 10),
      threads_per_file: parseInt($('#setThreads').value, 10),
      proxy: $('#setProxy').value.trim(),
      cookie: $('#setCookie').value.trim(),
      output_layout: (document.querySelector('#setLayout button.on') || {}).dataset
                     ? document.querySelector('#setLayout button.on').dataset.layout : 'flat',
    };
    try {
      const res = await post('/api/config', { config: payload });
      state.config = res.config;
      $('#saveTip').hidden = false;
      setTimeout(() => { $('#saveTip').hidden = true; }, 1600);
      loadStatus();
      toast('设置已保存', 'ok');
    } catch (e) {
      toast(e.message || '保存失败', 'err');
    }
  }

  // ---------------- 登录 ----------------
  async function openLogin() {
    $('#loginDialog').showModal();
    $('#loginUser').hidden = true;
    startQr();
  }

  async function startQr() {
    clearInterval(state.qrTimer);
    const target = $('#qrTarget');
    target.innerHTML = '';
    $('#qrMask').hidden = true;
    $('#qrStatus').textContent = '正在生成二维码…';

    try {
      const res = await api('/api/login/qr');
      if (typeof QRCode === 'undefined') {
        $('#qrStatus').textContent = '二维码组件缺失，请检查静态文件';
        return;
      }
      new QRCode(target, {
        text: res.url,
        width: 200,
        height: 200,
        colorDark: '#17181b',
        colorLight: '#ffffff',
        correctLevel: QRCode.CorrectLevel.M,
      });
      $('#qrStatus').textContent = '打开 B站 App 扫一扫';

      const key = res.key;
      state.qrTimer = setInterval(async () => {
        try {
          const r = await api(`/api/login/poll?key=${encodeURIComponent(key)}`);
          if (r.status === 'success') {
            clearInterval(state.qrTimer);
            $('#qrStatus').textContent = '登录成功';
            $('#loginUser').hidden = false;
            $('#loginUserName').textContent = (r.user && r.user.uname) || '已登录';
            toast('登录成功，现在可以下载 1080P 及以上画质了', 'ok');
            loadStatus();
            setTimeout(() => $('#loginDialog').close(), 1200);
          } else if (r.status === 'fail') {
            clearInterval(state.qrTimer);
            $('#qrMask').hidden = false;
            $('#qrMaskText').textContent = '未拿到登录凭证';
            $('#qrStatus').textContent = r.message || '登录失败，请刷新二维码重试';
          } else if (r.status === 86038) {
            // 二维码 3 分钟就过期，过期后自动换一张，用户不用手动点刷新
            clearInterval(state.qrTimer);
            $('#qrStatus').textContent = '二维码已过期，正在自动换一张…';
            setTimeout(() => startQr(), 1200);
          } else if (r.message) {
            $('#qrStatus').textContent = r.message;
          }
        } catch (e) { /* 忽略偶发错误 */ }
      }, 2000);
    } catch (e) {
      $('#qrStatus').textContent = e.message || '生成失败';
    }
  }

  async function doLogout() {
    try {
      await post('/api/login/logout', {});
      toast('已退出登录');
      $('#loginDialog').close();
      loadStatus();
    } catch (e) { toast(e.message, 'err'); }
  }

  // ---------------- 事件绑定 ----------------
  $('#parseBtn').addEventListener('click', () => doParse());
  $('#urlInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') doParse();
  });
  $('#urlInput').addEventListener('input', () => {
    $('#clearBtn').hidden = !$('#urlInput').value;
  });
  $('#clearBtn').addEventListener('click', () => {
    $('#urlInput').value = '';
    $('#clearBtn').hidden = true;
    $('#urlInput').focus();
  });

  // 示例 chip：填入示例链接
  const DEMOS = {
    video: 'https://www.bilibili.com/video/BV1GJ411x7h7',
    pages: '',
    season: '',
    space: '',
    fav: '',
    bangumi: '',
  };
  $$('.chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const demo = DEMOS[chip.dataset.demo];
      if (demo) {
        $('#urlInput').value = demo;
        $('#clearBtn').hidden = false;
        doParse(demo);
      } else {
        toast('粘贴对应页面的链接即可，例如：space.bilibili.com/123456');
      }
    });
  });

  // 剪贴板识别
  let pasteChecked = false;
  $('#urlInput').addEventListener('focus', async () => {
    if (pasteChecked || $('#urlInput').value) return;
    pasteChecked = true;
    try {
      const text = await navigator.clipboard.readText();
      if (text && /(bilibili\.com|b23\.tv|BV[0-9A-Za-z]{10})/.test(text)) {
        $('#urlInput').value = text.trim();
        $('#clearBtn').hidden = false;
        $('#pasteHint').hidden = false;
        setTimeout(() => { $('#pasteHint').hidden = true; }, 3200);
      }
    } catch (e) { /* 无权限，忽略 */ }
  });

  // 条目勾选 / 合集折叠
  $('#itemList').addEventListener('click', (e) => {
    const head = e.target.closest('.group-head');
    if (head) {
      const g = head.dataset.group;
      if (e.target.matches('input[data-gsel]')) {
        // 整组全选 / 取消
        const pairs = state.parsed.items
          .map((it, i) => ({ it, i }))
          .filter(({ it }) => (it.group || '') === g);
        const allSel = pairs.every(({ i }) => state.selected.has(i));
        pairs.forEach(({ i }) => {
          if (allSel) state.selected.delete(i);
          else state.selected.add(i);
        });
      } else {
        if (state.collapsed.has(g)) state.collapsed.delete(g);
        else state.collapsed.add(g);
      }
      renderItems();
      return;
    }
    const row = e.target.closest('.item-row');
    if (!row || !row.dataset.idx) return;
    const i = parseInt(row.dataset.idx, 10);
    const cb = row.querySelector('input[type=checkbox]');
    if (e.target !== cb) {
      cb.checked = !cb.checked;
    }
    if (cb.checked) state.selected.add(i);
    else state.selected.delete(i);
    row.classList.toggle('checked', cb.checked);
    updateSelInfo();
  });

  // 滚动加载
  $('#itemList').addEventListener('scroll', () => {
    const el = $('#itemList');
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 40) {
      const vis = visibleItems();
      if (state.renderedCount < vis.length) {
        const tipRow = el.querySelector('li:last-child');
        if (tipRow && tipRow.textContent.includes('还有')) tipRow.remove();
        renderItems(false);
      }
    }
  });

  $('#selNewBtn').addEventListener('click', () => {
    const items = state.parsed ? state.parsed.items : [];
    state.selected = new Set(items.map((_, i) => i).filter((i) => dlFlags(items[i]).state !== 'yes'));
    renderItems(true);
    toast(`已只勾选 ${state.selected.size} 个当前画质下未下载的条目`);
  });

  $('#checkAll').addEventListener('change', (e) => {
    const total = state.parsed ? state.parsed.items.length : 0;
    state.selected = e.target.checked ? new Set(state.parsed.items.map((_, i) => i)) : new Set();
    renderItems(true);
  });

  $('#invertBtn').addEventListener('click', () => {
    if (!state.parsed) return;
    const total = state.parsed.items.length;
    const ns = new Set();
    for (let i = 0; i < total; i++) if (!state.selected.has(i)) ns.add(i);
    state.selected = ns;
    renderItems(true);
  });

  $('#rangeBtn').addEventListener('click', () => {
    if (!state.parsed) return;
    const total = state.parsed.items.length;
    const raw = prompt('输入要选择的序号范围，例如 1-10,15,20-30', `1-${total}`);
    if (raw == null) return;
    const set = new Set();
    raw.split(/[,，]/).forEach(seg => {
      seg = seg.trim();
      if (!seg) return;
      const m = seg.match(/^(\d+)\s*-\s*(\d+)$/);
      if (m) {
        let a = parseInt(m[1], 10), b = parseInt(m[2], 10);
        if (a > b) [a, b] = [b, a];
        for (let i = a; i <= b; i++) if (i >= 1 && i <= total) set.add(i - 1);
      } else {
        const n = parseInt(seg, 10);
        if (!isNaN(n) && n >= 1 && n <= total) set.add(n - 1);
      }
    });
    if (!set.size) { toast('范围里没有有效序号', 'err'); return; }
    state.selected = set;
    renderItems(true);
    toast(`已选中 ${set.size} 项`);
  });

  $('#filterInput').addEventListener('input', (e) => {
    state.filter = e.target.value;
    renderItems(true);
  });

  ['optQuality'].forEach(id => {
    $('#' + id).addEventListener('change', updateEstimate);
  });

  // 输出形式：视频 / 仅音频（互斥）
  $('#outSeg').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn');
    if (!btn) return;
    document.querySelectorAll('#outSeg .seg-btn').forEach((b) => b.classList.remove('on'));
    btn.classList.add('on');
    updateEstimate();
  });

  $('#startBtn').addEventListener('click', startDownload);
  $('#settingsBtn').addEventListener('click', async () => {
    await loadConfig();
    $('#settingsDialog').showModal();
  });
  $('#saveSettingsBtn').addEventListener('click', saveSettings);
  document.querySelectorAll('#setLayout button').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#setLayout button').forEach(x => x.classList.toggle('on', x === b));
    });
  });

  $('#pickDirBtn').addEventListener('click', async () => {
    try {
      const res = await post('/api/pick_dir', {});
      if (res.path) { $('#setDir').value = res.path; toast('已选择：' + res.path, 'ok'); }
      else if (!res.supported) toast('选择框只在 Windows 版可用，直接填路径也行', 'warn');
    } catch (e) { toast(e.message, 'err'); }
  });

  $('#openSetDirBtn').addEventListener('click', async () => {
    try { await post('/api/open', { path: $('#setDir').value || state.config.download_dir }); }
    catch (e) { toast(e.message, 'err'); }
  });
  $('#userChip').addEventListener('click', openLogin);
  $('#qrRefresh').addEventListener('click', startQr);
  $('#logoutBtn').addEventListener('click', doLogout);
  $('#openDirBtn').addEventListener('click', async () => {
    try { await post('/api/open', {}); } catch (e) { toast(e.message, 'err'); }
  });
  $('#refreshFilesBtn').addEventListener('click', loadLibrary);

  $$('[data-close]').forEach(btn => {
    btn.addEventListener('click', () => {
      const d = document.getElementById(btn.dataset.close);
      if (d) d.close();
    });
  });
  $('#loginDialog').addEventListener('close', () => clearInterval(state.qrTimer));

  // 设置弹窗打开时同步画质等；切画质要重算「已下载」标记
  $('#optQuality').addEventListener('change', () => {
    state.config.max_height = parseInt($('#optQuality').value, 10);
    if (!state.parsed) return;
    const items = state.parsed.items;
    // 换画质等于换一份产物：720P 下过的条目在 4K 下要重新勾上，否则根本勾不了
    state.selected = new Set(items.map((_, i) => i).filter((i) => dlFlags(items[i]).state !== 'yes'));
    renderItems(true);
    const stale = items.filter((x) => dlFlags(x).state === 'stale').length;
    if (stale) toast(`${stale} 条之前下的是低画质，已按当前画质重新勾选`);
  });

  // 键盘快捷键
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'v' && document.activeElement !== $('#urlInput')) {
      $('#urlInput').focus();
    }
  });

  // ---------------- 启动 ----------------
  async function init() {
    await loadStatus();
    await loadConfig();
    await refreshJobs();
    loadLibrary();
    state.jobsTimer = setInterval(() => {
      refreshJobs();
      if (!$('#filesPanel').hidden) loadLibrary();
    }, 1000);
  }

  init();
})();
