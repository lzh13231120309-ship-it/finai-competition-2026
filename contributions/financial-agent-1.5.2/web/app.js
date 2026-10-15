/* 人工智能金融大赛本地 Agent —— 前端逻辑 */

const API = {
  async get(url) { const r = await fetch(url); return r.json(); },
  async post(url, body) {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}) });
    return r.json();
  },
  async patch(url, body) {
    const r = await fetch(url, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}) });
    return r.json();
  },
  async del(url) { const r = await fetch(url, { method: 'DELETE' }); return r.json(); },
};

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const S = {
  tasks: [], currentTask: null, messages: [], config: null, providers: {},
  attachments: [], runs: new Map(), modelsByProvider: {},
  customEditing: null, cpFetched: [],
};

/* =================================================================== 工具 */

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('#toast-wrap').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 2600);
}

function esc(t) {
  return String(t == null ? '' : t).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/* 轻量 Markdown 渲染（离线可用，无外部依赖） */
function md(src) {
  if (!src) return '';
  const codes = [];
  let t = String(src).replace(/```([^\n`]*)\n([\s\S]*?)```/g, (m, lang, body) => {
    codes.push('<pre><code>' + esc(body.replace(/\n$/, '')) + '</code></pre>');
    return '\u0000CODE' + (codes.length - 1) + '\u0000';
  });
  t = esc(t);

  // 表格
  t = t.replace(/(^\|.*\|\s*$\n)(^\|[\s:|-]+\|\s*$\n)((?:^\|.*\|\s*$\n?)*)/gm, (m, head, sep, body) => {
    const cells = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
    const th = cells(head).map(c => '<th>' + c + '</th>').join('');
    const rows = body.trim().split('\n').filter(Boolean)
      .map(r => '<tr>' + cells(r).map(c => '<td>' + c + '</td>').join('') + '</tr>').join('');
    return '<table><thead><tr>' + th + '</tr></thead><tbody>' + rows + '</tbody></table>';
  });

  t = t.replace(/^###\s+(.*)$/gm, '<h3>$1</h3>')
       .replace(/^##\s+(.*)$/gm, '<h2>$1</h2>')
       .replace(/^#\s+(.*)$/gm, '<h1>$1</h1>')
       .replace(/^\s*(?:---|\*\*\*)\s*$/gm, '<hr>')
       .replace(/^&gt;\s?(.*)$/gm, '<blockquote>$1</blockquote>')
       .replace(/^\s*[-*+]\s+(.*)$/gm, '<li>$1</li>')
       .replace(/^\s*\d+\.\s+(.*)$/gm, '<li>$1</li>');
  t = t.replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, '<ul>$1</ul>');

  t = t.replace(/`([^`\n]+)`/g, '<code>$1</code>')
       .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
       .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
       .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  t = t.split(/\n{2,}/).map(p => {
    if (/^\s*<(h[1-3]|ul|ol|pre|table|blockquote|hr|\u0000CODE)/.test(p.trim())) return p;
    return '<p>' + p.replace(/\n/g, '<br>') + '</p>';
  }).join('\n');

  t = t.replace(/\u0000CODE(\d+)\u0000/g, (m, i) => codes[+i]);
  return t;
}

/* =================================================================== 初始化 */

async function boot() {
  applyTheme(localStorage.getItem('theme') || 'light');
  await Promise.all([loadProviders(), loadConfig(), loadTasks(), checkHealth()]);
  bindEvents();
  updateTitleMeta();
  renderModelLabel();
  setInterval(refreshRunning, 4000);
}

async function checkHealth() {
  try {
    const h = await API.get('/api/health');
    $('#health').textContent = h.has_key ? `已就绪 · ${h.model || '未选模型'}` : '未配置 API Key';
  } catch { $('#health').textContent = '服务未连接'; }
}

async function loadProviders() {
  const r = await API.get('/api/providers');
  S.providers = r.providers || {};
  const sel = $('#cfg-provider');
  sel.innerHTML = Object.entries(S.providers)
    .map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join('');
}

async function loadConfig() {
  const r = await API.get('/api/config');
  S.config = r.config;
  fillSettings();
}

function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('theme', t);
}

/* =================================================================== 任务 */

async function loadTasks() {
  const r = await API.get('/api/tasks');
  S.tasks = r.tasks || [];
  (r.running || []).forEach(t => ensureRun(t));
  renderTasks();
  if (!S.currentTask && S.tasks.length) await selectTask(S.tasks[0].id);
  else if (!S.tasks.length) showWelcome(true);
}

function renderTasks() {
  const q = ($('#task-search').value || '').trim().toLowerCase();
  const list = $('#task-list');
  const items = S.tasks.filter(t => !q || (t.title || '').toLowerCase().includes(q));
  if (!items.length) {
    list.innerHTML = `<div class="empty-tip">${S.tasks.length ? '没有匹配的任务' : '还没有任务，点上方「新建任务」开始'}</div>`;
    return;
  }
  list.innerHTML = items.map(t => `
    <div class="task-item ${t.id === S.currentTask ? 'active' : ''}" data-id="${t.id}">
      <div class="t-name">
        ${S.runs.has(t.id) && !S.runs.get(t.id).finished ? '<span class="mini-dot"></span>' : ''}
        <span style="overflow:hidden;text-overflow:ellipsis">${esc(t.title || '新任务')}</span>
      </div>
      <div class="t-sub">${t.message_count || 0} 条 · ${esc(timeAgo(t.updated_at))}</div>
      <button class="t-del" data-del="${t.id}" title="删除任务">✕</button>
    </div>`).join('');

  list.querySelectorAll('.task-item').forEach(el => {
    el.onclick = (e) => {
      if (e.target.dataset.del) return;
      selectTask(el.dataset.id);
    };
  });
  list.querySelectorAll('.t-del').forEach(b => {
    b.onclick = async (e) => {
      e.stopPropagation();
      const id = b.dataset.del;
      if (!confirm('删除该任务及其全部消息？此操作不可撤销。')) return;
      await API.del('/api/tasks/' + id);
      S.runs.delete(id);
      if (S.currentTask === id) { S.currentTask = null; S.messages = []; showWelcome(true); }
      await loadTasks();
      toast('任务已删除', 'ok');
    };
  });
}

function timeAgo(ts) {
  if (!ts) return '';
  const d = Date.now() / 1000 - ts;
  if (d < 60) return '刚刚';
  if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
  if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
  return new Date(ts * 1000).toLocaleDateString('zh-CN');
}

async function newTask(title = '新任务') {
  const r = await API.post('/api/tasks', { title });
  S.tasks.unshift(r.task);
  await selectTask(r.task.id);
  renderTasks();
  $('#input').focus();
  return r.task.id;
}

async function selectTask(id) {
  S.currentTask = id;
  S.attachments = [];
  renderAttachments();
  const r = await API.get(`/api/tasks/${id}/messages`);
  if (S.currentTask !== id) return;
  const run = S.runs.get(id);
  if (run?.finished && (!run.stopped || run.cancelAcknowledged)) run.retain = false;
  S.messages = r.messages || [];
  renderTasks();
  renderChat();
  const t = S.tasks.find(x => x.id === id);
  $('#task-title').textContent = t ? (t.title || '新任务') : '新任务';
  updateTitleMeta();
  updateRunUI();
}

function updateTitleMeta() {
  const cfg = S.config || {};
  const p = S.providers[cfg.provider] || {};
  const providerLabel = p.label || cfg.provider || '';
  const t = S.tasks.find(x => x.id === S.currentTask);
  $('#task-meta').textContent = `${providerLabel} · ${cfg.model || '未选模型'}${t ? ' · ' + (t.message_count || 0) + ' 条消息' : ''}`;
}

/* =================================================================== 消息渲染 */

function showWelcome(v) {
  $('#welcome').classList.toggle('hidden', !v);
  if (v) { $('#task-title').textContent = '新任务'; $('#task-meta').textContent = ''; }
}

function renderChat() {
  const chat = $('#chat');
  const has = S.messages.length || (S.currentTask && S.runs.has(S.currentTask));
  showWelcome(!has);
  chat.innerHTML = '';
  let run = S.currentTask ? S.runs.get(S.currentTask) : null;

  let i = 0;
  while (i < S.messages.length) {
    const m = S.messages[i];
    if (m.role === 'user') { chat.appendChild(userNode(m)); i++; continue; }
    if (m.role === 'assistant') {
      if (m.blocks) { chat.appendChild(assistantNode(m.blocks, false)); i++; continue; }
      const blocks = [];
      if (m.reasoning) blocks.push({ type: 'reasoning', text: m.reasoning, done: true });
      if (m.content) blocks.push({ type: 'text', text: m.content, done: true });
      const calls = m.tool_calls || [];
      let j = i + 1;
      const results = [];
      while (j < S.messages.length && S.messages[j].role === 'tool') { results.push(S.messages[j]); j++; }
      calls.forEach((c, idx) => {
        const res = results[idx];
        blocks.push({
          type: 'tool',
          name: (c.function || {}).name || '',
          args: safeJson((c.function || {}).arguments),
          result: res ? res.content : '(无结果)',
          done: true, ok: res ? !String(res.content).startsWith('错误：') : true, ms: null,
        });
      });
      chat.appendChild(assistantNode(blocks, false));
      i = j;
      continue;
    }
    i++;
  }

  if (run && run.retain !== false && run.blocks && run.blocks.length) {
    const node = assistantNode(run.blocks, !run.finished);
    chat.appendChild(node);
    run.node = node;
    run.renderedBlocks = run.blocks.length;
    run.dirtyBlocks.clear();
    run.container = node.querySelector('.bubble-wrap');
    bindLiveUpdate(run);
  } else if (run && !run.finished) {
    const node = assistantNode([{ type: 'text', text: '', done: false }], true);
    chat.appendChild(node);
    run.node = node;
    run.renderedBlocks = 0;
    run.dirtyBlocks.clear();
    run.container = node.querySelector('.bubble-wrap');
    bindLiveUpdate(run);
  }
  scrollBottom();
}

function safeJson(s) {
  try { return JSON.parse(s || '{}'); } catch { return { _raw: s }; }
}

async function copyText(t) {
  try { await navigator.clipboard.writeText(t); }
  catch (e) {
    const ta = document.createElement('textarea');
    ta.value = t; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); ta.remove();
  }
  toast('已复制到剪贴板', 'ok');
}

function userNode(m) {
  const div = document.createElement('div');
  div.className = 'msg user';
  const atts = (m.attachments || []).map(a =>
    `<span class="att-chip">${esc(a.name)}</span>`).join('');
  div.innerHTML = `<div class="avatar">我</div>
    <div class="bubble-wrap">
      <div class="role-line"><span>你</span><button class="copy-btn" title="一键复制">⧉ 复制</button></div>
      ${atts ? `<div class="att-chips">${atts}</div>` : ''}
      <div class="bubble">${md(m.content || '')}</div>
    </div>`;
  div.querySelector('.copy-btn').onclick = () => copyText(m.content || '');
  return div;
}

function assistantNode(blocks, streaming) {
  const div = document.createElement('div');
  div.className = 'msg assistant';
  div.innerHTML = `<div class="avatar">AI</div>
    <div class="bubble-wrap">
      <div class="role-line"><span>助手${streaming ? ' · 生成中' : ''}</span><button class="copy-btn" title="一键复制">⧉ 复制</button></div>
      <div class="blocks"></div>
    </div>`;
  div.querySelector('.copy-btn').onclick = () => {
    const text = (blocks || []).filter(b => b && b.type === 'text')
      .map(b => b.text || '').join('\n\n');
    copyText(text);
  };
  const holder = div.querySelector('.blocks');
  blocks.forEach(b => holder.appendChild(blockNode(b, streaming)));
  if (streaming && blocks.length && blocks[blocks.length - 1].type === 'text') {
    const last = holder.lastChild.querySelector('.bubble-md');
    if (last) last.insertAdjacentHTML('beforeend', '<span class="cursor"></span>');
  }
  return div;
}

function blockNode(b, streaming) {
  if (b.type === 'reasoning') {
    const d = document.createElement('div');
    d.className = 'reasoning';
    d.innerHTML = `<div class="reasoning-head"><span>${b.open ? '▾' : '▸'}</span>思考过程</div>
      <div class="reasoning-body ${b.open ? '' : 'hidden'}">${esc(b.text || '')}</div>`;
    d.querySelector('.reasoning-head').onclick = () => {
      b.open = !b.open;  // 状态记在块对象上，重渲染后不丢
      const body = d.querySelector('.reasoning-body');
      body.classList.toggle('hidden', !b.open);
      d.querySelector('.reasoning-head span').textContent = b.open ? '▾' : '▸';
    };
    return d;
  }
  if (b.type === 'tool') {
    const d = document.createElement('div');
    d.className = 'tool';
    const status = b.paused ? '<span class="tool-status">已暂停</span>' : b.done ? (b.ok === false ? '<span class="tool-status err">失败</span>'
      : `<span class="tool-status ok">完成${b.ms != null ? ' · ' + b.ms + 'ms' : ''}</span>`)
      : '<span class="tool-status">调用中…</span>';
    d.innerHTML = `<div class="tool-head"><span class="tool-ico">⚙</span>
        <span class="tool-name">${esc(b.name)}</span>${status}<span style="font-size:10px;color:var(--text-3)">▾</span></div>
      <div class="tool-body ${b.done ? 'hidden' : ''}">
        <div class="tool-label">参数</div>
        <pre>${esc(JSON.stringify(b.args || {}, null, 2))}</pre>
        <div class="tool-label">结果</div>
        <pre class="tool-res">${esc(String(b.result == null ? '(等待中…)' : b.result))}</pre>
      </div>`;
    d.querySelector('.tool-head').onclick = () => d.querySelector('.tool-body').classList.toggle('hidden');
    return d;
  }
  const d = document.createElement('div');
  d.className = 'bubble bubble-md';
  d.innerHTML = md(b.text || '');
  return d;
}

function bindLiveUpdate(run) {
  if (!run.node) return;
}

function scrollBottom(force) {
  const el = $('#chat-scroll');
  if (force || el.scrollHeight - el.scrollTop - el.clientHeight < 260) {
    requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
  }
}

/* =================================================================== 流式发送 */

function ensureRun(taskId) {
  if (!S.runs.has(taskId)) {
    // 页面刷新后仅知道服务端任务 id；没有本次生成令牌时兼容按任务停止。
    S.runs.set(taskId, { ...createRun(taskId), id: null });
  }
  return S.runs.get(taskId);
}

function createRun(taskId) {
  return { taskId, id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`,
    blocks: [], finished: false, retain: true, stopped: false, abort: null, node: null,
    reader: null, cancelPromise: null, cancelTarget: null, started: false,
    dirtyBlocks: new Set(), renderedBlocks: 0, cursorIndex: null, idleTimer: null };
}

function updateRunUI() {
  const running = S.currentTask && S.runs.has(S.currentTask) && !S.runs.get(S.currentTask).finished;
  $('#btn-send').classList.toggle('hidden', !!running);
  $('#btn-stop').classList.toggle('hidden', !running);
  $('#running-badge').classList.toggle('hidden', !running);
  const anyRunning = Array.from(S.runs.values()).some(r => !r.finished);
  if (anyRunning) {
    const n = Array.from(S.runs.values()).filter(r => !r.finished).length;
    $('#running-text').textContent = n > 1 ? `${n} 个任务运行中` : '运行中';
    const run = S.runs.get(S.currentTask);
    if (run && !run.finished && run.step) {
      $('#running-text').textContent += ` · 第 ${run.step} 步${run.maxSteps ? ' / ' + run.maxSteps : ''}`;
    }
  }
}

async function refreshRunning() {
  try {
    const r = await API.get('/api/tasks');
    const runningIds = new Set(r.running || []);
    Array.from(S.runs.keys()).forEach(id => {
      if (!runningIds.has(id) && !S.runs.get(id).finished && !S.runs.get(id).abort) {
        const run = S.runs.get(id);
        run.finished = true;
      }
    });
    renderTasks();
    updateRunUI();
  } catch {}
}

async function send() {
  const input = $('#input');
  const text = input.value.trim();
  let taskId = S.currentTask;
  const attachments = S.attachments.map(a => ({ ...a }));
  if (!text && !S.attachments.length) return;
  if (S.runs.has(taskId) && !S.runs.get(taskId).finished) {
    toast('当前任务正在运行，请先停止或切换任务');
    return;
  }
  if (!taskId) taskId = await newTask(text.slice(0, 14) || '新任务');

  const atts = attachments.map(a => a.name);
  input.value = '';
  autoGrow(input);

  // 本地立即回显
  const previous = S.runs.get(taskId);
  if (S.currentTask === taskId && previous?.retain && previous.blocks.length) {
    S.messages.push({ role: 'assistant', blocks: previous.blocks });
  }
  const localUser = { role: 'user', content: text, attachments: attachments.map(a => ({ name: a.name })) };
  if (S.currentTask === taskId) S.messages.push(localUser);

  const run = createRun(taskId);
  run.cancelTarget = previous?.cancelTarget || (previous?.cancelPromise ? previous : null);
  S.runs.set(taskId, run);
  if (S.currentTask === taskId) { S.attachments = []; renderAttachments(); renderChat(); }
  updateRunUI();

  const controller = new AbortController();
  run.abort = controller;

  try {
    // 上次停止仍在落盘时，保留输入响应，但等待服务端确认后再续跑。
    armRunIdleTimeout(run);
    if (run.cancelTarget) {
      await confirmRunCancelled(run.cancelTarget);
      run.cancelTarget = null;
    }
    if (run.stopped || S.runs.get(taskId) !== run) return;
    armRunIdleTimeout(run);
    run.started = true;
    const resp = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId, run_id: run.id, content: text, attachments: atts,
        speed_mode: $('#speed-mode').value || S.config?.speed_mode || 'balanced' }),
      signal: controller.signal,
    });
    if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);
    const reader = resp.body.getReader();
    run.reader = reader;
    if (run.stopped || S.runs.get(taskId) !== run) { reader.cancel().catch(() => {}); return; }
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (run.stopped || S.runs.get(taskId) !== run) break;
      if (done) break;
      // 注释心跳也说明工具仍在运行；模型无内容的超时由服务端判断。
      armRunIdleTimeout(run);
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();
      for (const part of parts) {
        const line = part.split('\n').find(l => l.startsWith('data:'));
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line.slice(5)); } catch { continue; }
        handleEvent(taskId, ev, run);
        if (run.finished) break;
      }
      if (run.finished) { reader.cancel().catch(() => {}); break; }
    }
  } catch (e) {
    if (e.name !== 'AbortError' && !run.stopped && S.runs.get(taskId) === run) {
      run.failed = true;
      let restored = false;
      if (!run.started && S.currentTask === taskId && !input.value.trim()) {
        input.value = text;
        S.attachments = [...attachments, ...S.attachments.filter(a => !attachments.some(old => old.name === a.name))];
        S.messages = S.messages.filter(m => m !== localUser);
        autoGrow(input);
        renderAttachments();
        restored = true;
      }
      run.blocks.push({ type: 'text', text: restored
        ? `⚠️ 停止请求尚未确认：${e.message}。指令和附件已放回输入区，请重试。`
        : `⚠️ 请求失败：${e.message}`, done: true });
      if (S.currentTask === taskId) renderLiveBlocks(run);
    }
  } finally {
    clearTimeout(run.idleTimer);
    cancelLiveRender(run);
    if (S.runs.get(taskId) !== run || run.stopped) return;
    run.finished = true;
    updateRunUI();
    if (S.currentTask === taskId) renderLiveBlocks(run);
    renderTasks();
    const t = S.tasks.find(x => x.id === taskId);
    if (t && run.started) { t.message_count = (t.message_count || 0) + 1; }
    if (S.currentTask === taskId && !run.failed) {
      try {
        const r = await API.get(`/api/tasks/${taskId}/messages`);
        if (S.currentTask === taskId && S.runs.get(taskId) === run) {
          S.messages = r.messages || [];
          run.retain = false;
          renderChat();
          updateTitleMeta();
        }
      } catch { /* 保留已经显示的部分输出，等待下次正常刷新。 */ }
    }
    loadTasks();
  }
}

function handleEvent(taskId, ev, run = S.runs.get(taskId)) {
  if (!run || S.runs.get(taskId) !== run || run.stopped || run.finished) return;
  const isCurrent = S.currentTask === taskId;
  if (ev.type !== 'heartbeat') armRunIdleTimeout(run);
  switch (ev.type) {
    case 'delta': {
      const last = run.blocks[run.blocks.length - 1];
      if (last && last.type === 'text' && !last.done) last.text += ev.text;
      else run.blocks.push({ type: 'text', text: ev.text, done: false });
      run.dirtyBlocks.add(run.blocks.length - 1);
      if (isCurrent) scheduleLiveRender(run);
      break;
    }
    case 'reasoning': {
      const last = run.blocks[run.blocks.length - 1];
      if (last && last.type === 'reasoning' && !last.done) {
        last.text += ev.text;
        run.dirtyBlocks.add(run.blocks.length - 1);
      }
      else {
        run.blocks.push({ type: 'reasoning', text: ev.text, done: false, open: true });
        if (isCurrent) scheduleLiveRender(run, true);
        break;
      }
      if (isCurrent) scheduleLiveRender(run);
      break;
    }
    case 'tool_start': {
      run.blocks.push({ type: 'tool', name: ev.name, args: ev.args, result: '', done: false, id: ev.id });
      if (isCurrent) scheduleLiveRender(run, true);
      break;
    }
    case 'tool_end': {
      for (let i = run.blocks.length - 1; i >= 0; i--) {
        const b = run.blocks[i];
        if (b.type === 'tool' && b.id === ev.id && !b.done) {
          b.result = ev.preview; b.done = true; b.ok = ev.ok; b.ms = ev.ms;
          run.dirtyBlocks.add(i); break;
        }
      }
      if (isCurrent) scheduleLiveRender(run, true);
      break;
    }
    case 'title': {
      const t = S.tasks.find(x => x.id === taskId);
      if (t) t.title = ev.title;
      if (isCurrent) $('#task-title').textContent = ev.title;
      renderTasks();
      break;
    }
    case 'status': if (isCurrent) toast(ev.text); break;
    case 'step':
      run.step = ev.n;
      run.maxSteps = ev.max;
      if (isCurrent) updateRunUI();
      break;
    case 'error': {
      run.blocks.push({ type: 'text', text: `⚠️ ${ev.error}`, done: true });
      run.finished = true;
      break;
    }
    case 'cancelled':
      run.blocks.push({ type: 'text', text: '（已停止生成）', done: true });
      run.finished = true;
      break;
    case 'paused':
      run.blocks.push({ type: 'text', text: ev.text || '任务已暂停，继续输入即可恢复。', done: true });
      run.finished = true;
      break;
    case 'done':
    case 'close':
      run.finished = true;
      updateRunUI();
      break;
  }
  if (run.finished) {
    run.blocks.forEach((b, i) => {
      if (b.type === 'tool' && !b.done) { b.paused = true; run.dirtyBlocks.add(i); }
    });
    clearTimeout(run.idleTimer);
    cancelLiveRender(run);
    if (isCurrent) { updateRunUI(); renderLiveBlocks(run); }
    renderTasks();
  }
}

/* 流式渲染节流：数据包只更新内存，每 ~90ms 才重绘一次「最后一块」。
   旧实现对每个数据包把碎片单独过一遍 Markdown 再塞进 DOM：
   ① 跨包的表格/加粗被拦腰截断，渲染错乱（左窄条、右侧空白）；
   ② 每次插入都触发整泡重排，长文卡顿。 */
const liveTimers = new Map();

function cancelLiveRender(run) {
  clearTimeout(liveTimers.get(run));
  liveTimers.delete(run);
}

function scheduleLiveRender(run) {
  if (liveTimers.has(run)) return;
  liveTimers.set(run, setTimeout(() => {
    liveTimers.delete(run);
    if (S.currentTask !== run.taskId || S.runs.get(run.taskId) !== run || run.finished) return;
    renderLiveBlocks(run);
  }, 90));
}

function renderLiveBlocks(run) {
  if (S.currentTask !== run.taskId || S.runs.get(run.taskId) !== run) return;
  const holder = run.node?.querySelector('.blocks');
  if (!holder) { renderChat(); return; }
  const count = run.renderedBlocks;
  const lastIndex = run.blocks.length - 1;
  const cursorIndex = !run.finished && run.blocks[lastIndex]?.type === 'text' ? lastIndex : null;
  if (run.cursorIndex !== cursorIndex && run.cursorIndex != null) run.dirtyBlocks.add(run.cursorIndex);
  const makeNode = index => {
    const node = blockNode(run.blocks[index], !run.finished);
    if (index === cursorIndex) node.insertAdjacentHTML('beforeend', '<span class="cursor"></span>');
    return node;
  };
  // 已完成的历史和工具节点原样保留，只替换本轮有变化的块。
  for (const index of run.dirtyBlocks) {
    if (index < count && holder.children[index]) {
      holder.replaceChild(makeNode(index), holder.children[index]);
    }
  }
  if (!count && run.blocks.length) holder.replaceChildren();
  for (let index = count; index < run.blocks.length; index++) holder.appendChild(makeNode(index));
  run.renderedBlocks = run.blocks.length;
  run.cursorIndex = cursorIndex;
  run.dirtyBlocks.clear();
  const label = run.node.querySelector('.role-line span');
  if (label) label.textContent = run.finished ? '助手' : '助手 · 生成中';
  scrollBottom();
}

function armRunIdleTimeout(run) {
  clearTimeout(run.idleTimer);
  const seconds = Number(S.config?.agent_idle_timeout ?? 180);
  if (seconds <= 0) return;
  run.idleTimer = setTimeout(() => {
    if (S.runs.get(run.taskId) === run && !run.finished) {
      stopRun(run.taskId, '长时间没有新输出，任务已暂停。继续输入即可恢复。');
    }
  }, seconds * 1000 + 1000);
}

function stopRun(taskId = S.currentTask, message = '（已停止生成，继续输入即可恢复）') {
  // 按钮事件不能作为 taskId 使用。
  if (typeof taskId !== 'string') taskId = S.currentTask;
  const run = S.runs.get(taskId);
  if (!run) return;
  if (run.finished) {
    const target = run.cancelTarget || (run.stopped ? run : null);
    if (!target) return run.cancelPromise;
    const confirmation = confirmRunCancelled(target);
    confirmation.catch(() => {});
    return confirmation;
  }
  run.stopped = true;
  run.finished = true;
  clearTimeout(run.idleTimer);
  cancelLiveRender(run);
  run.abort?.abort();
  if (run.reader) { try { Promise.resolve(run.reader.cancel()).catch(() => {}); } catch {} }
  if (S.currentTask === taskId) { updateRunUI(); $('#input').focus(); }
  run.blocks.push({ type: 'text', text: message, done: true });
  run.blocks.forEach((b, i) => {
    if (b.type === 'tool' && !b.done) { b.paused = true; run.dirtyBlocks.add(i); }
  });
  if (S.currentTask === taskId) renderLiveBlocks(run);
  renderTasks();
  return requestRunCancellation(run);
}

async function confirmRunCancelled(run) {
  if (run.cancelAcknowledged) return;
  if (run.cancelPromise) {
    try { return await run.cancelPromise; } catch { /* 临时连接失败允许重试同一次生成。 */ }
  }
  return requestRunCancellation(run);
}

function requestRunCancellation(run) {
  const taskId = run.taskId;
  run.cancelPromise = (async () => {
    let result;
    do {
      result = await API.post('/api/chat/cancel', { task_id: taskId, run_id: run.id });
      if (result.ok === false || result.error) throw new Error(result.error || '停止未确认');
      if (result.stopping) await new Promise(resolve => setTimeout(resolve, 100));
    } while (result.stopping);
    run.cancelAcknowledged = true;
    return result;
  })();
  run.cancelPromise.catch(() => {
    if (S.currentTask === taskId && S.runs.get(taskId) === run) toast('停止请求未确认，请检查连接后重试。', 'err');
  });
  return run.cancelPromise;
}

/* =================================================================== 附件 */

async function uploadFiles(files) {
  if (!files || !files.length) return;
  if (!S.currentTask) await newTask();
  for (const f of files) {
    const fd = new FormData();
    fd.append('task_id', S.currentTask);
    fd.append('file', f);
    try {
      const r = await fetch('/api/upload', { method: 'POST', body: fd });
      const j = await r.json();
      if (j.ok) {
        S.attachments.push(j.file);
        if (j.file.error) toast(`${j.file.name}：${j.file.error}`, 'err');
      } else toast('上传失败', 'err');
    } catch (e) { toast('上传失败：' + e.message, 'err'); }
  }
  renderAttachments();
}

function renderAttachments() {
  const bar = $('#attach-bar');
  bar.innerHTML = S.attachments.map((a, i) => `
    <div class="att-item ${a.error ? 'bad' : ''}" title="${esc(a.error || a.preview || '')}">
      <span class="att-kind">${esc(kindLabel(a.kind))}</span>
      <span>${esc(a.name)}</span>
      <span style="color:var(--text-3);font-size:11px">${fmtSize(a.size)}</span>
      <button class="att-x" data-i="${i}">✕</button>
    </div>`).join('');
  bar.querySelectorAll('.att-x').forEach(b => {
    b.onclick = () => { S.attachments.splice(+b.dataset.i, 1); renderAttachments(); };
  });
}

function kindLabel(k) {
  return { image: '图片', table: '表格', document: '文档', text: '文本' }[k] || '文件';
}
function fmtSize(n) {
  if (!n) return '';
  if (n < 1024) return n + 'B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + 'KB';
  return (n / 1024 / 1024).toFixed(1) + 'MB';
}

/* =================================================================== 模型选择器 */

function renderModelLabel() {
  const c = S.config || {};
  const p = S.providers[c.provider] || {};
  $('#model-label').textContent = c.model || '选择模型';
  $('#model-btn').title = `${p.label || c.provider} · ${c.model || ''}`;
}

async function openModelMenu() {
  const menu = $('#model-menu');
  const c = S.config || {};
  const hasKeys = c.has_keys || {};
  let html = '';
  Object.entries(S.providers).forEach(([pid, p]) => {
    const extra = (S.modelsByProvider[pid] || []).filter(m => !p.models.includes(m));
    const models = [...p.models, ...extra].slice(0, 30);
    html += `<div class="mm-group">${esc(p.label)}${hasKeys[pid] ? '' : ' · 未配置 Key'}</div>`;
    if (!models.length) html += `<div class="mm-item" data-pid="${pid}" data-model=""><span class="mm-name">（点击填写模型名）</span></div>`;
    models.forEach(m => {
      const active = c.provider === pid && c.model === m;
      html += `<div class="mm-item ${active ? 'active' : ''}" data-pid="${pid}" data-model="${esc(m)}">
        <span class="mm-name">${esc(m)}</span>
        ${active ? '<span class="mm-sub">当前</span>' : ''}
        ${hasKeys[pid] ? '' : '<span class="mm-key">缺 Key</span>'}
      </div>`;
    });
  });
  html += `<div class="mm-group">管理</div>
    <div class="mm-item" id="mm-settings"><span class="mm-name">打开设置页配置更多模型…</span></div>`;
  menu.innerHTML = html;
  menu.classList.remove('hidden');

  menu.querySelectorAll('.mm-item[data-pid]').forEach(el => {
    el.onclick = async () => {
      const pid = el.dataset.pid, model = el.dataset.model;
      if (!model) { openDrawer('model'); menu.classList.add('hidden'); return; }
      await switchModel(pid, model);
      menu.classList.add('hidden');
    };
  });
  const ms = $('#mm-settings');
  if (ms) ms.onclick = () => { openDrawer('model'); menu.classList.add('hidden'); };
}

async function switchModel(provider, model) {
  const c = S.config || {};
  if (!(c.has_keys || {})[provider] && provider !== 'ollama') {
    toast(`${S.providers[provider].label} 还没填 API Key`, 'err');
    const r = await API.post('/api/config', { patch: { provider, model } });
    S.config = r.config; fillSettings(); renderModelLabel(); updateTitleMeta();
    openDrawer('model');
    return;
  }
  const r = await API.post('/api/config', { patch: { provider, model } });
  S.config = r.config;
  fillSettings(); renderModelLabel(); updateTitleMeta();
  toast(`已切换到 ${model}（对话上下文保留）`, 'ok');
}

/* Speed selection is global; every chat request also snapshots its choice. */
function renderSpeedMode() {
  const mode = S.config?.speed_mode || 'balanced';
  for (const id of ['#speed-mode', '#cfg-speed-mode']) {
    const element = $(id);
    if (element) element.value = mode;
  }
}

async function switchSpeedMode(mode) {
  const controls = ['#speed-mode', '#cfg-speed-mode'].map(id => $(id)).filter(Boolean);
  for (const element of controls) { element.value = mode; element.disabled = true; }
  try {
    const result = await API.post('/api/config', {patch:{speed_mode:mode}});
    S.config = result.config;
    toast('速度档位已保存，下条消息生效', 'ok');
  } catch (error) {
    toast('速度档位保存失败：' + error.message, 'err');
  } finally {
    renderSpeedMode();
    for (const element of controls) element.disabled = false;
  }
}

/* =================================================================== 设置 */

function openDrawer(tab) {
  $('#drawer').classList.add('open');
  $('#drawer-mask').classList.remove('hidden');
  if (tab) activateTab(tab);
  refreshPanes();
}
function closeDrawer() {
  $('#drawer').classList.remove('open');
  $('#drawer-mask').classList.add('hidden');
}
function activateTab(name) {
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $$('.tabpane').forEach(p => p.classList.toggle('active', p.dataset.pane === name));
  refreshPanes();
}

function fillSettings() {
  const c = S.config || {};
  renderSpeedMode();
  const curP = S.providers[c.provider] || {};
  $('#cfg-provider').value = c.provider || 'deepseek';
  $('#cfg-baseurl').value = (c.base_url_overrides || {})[c.provider]
    || (curP.custom ? (curP.base_url || '') : '');
  $('#cfg-model').value = c.model || '';
  $('#cfg-temp').value = c.temperature ?? 0.3;
  $('#cfg-maxtokens').value = c.max_tokens || 32768;
  $('#cfg-system').value = c.system_prompt || '';
  $('#cfg-rounds').value = c.context_rounds ?? 20;
  $('#cfg-steps').value = c.agent_max_steps ?? 1000;
  $('#cfg-idle-timeout').value = c.agent_idle_timeout ?? 180;
  $('#cfg-autotitle').checked = !!c.auto_title;
  const te = c.tools_enabled || {};
  $('#cfg-calculator').checked = te.calculator !== false;
  $('#cfg-knowledge').checked = te.knowledge_search !== false;
  $('#cfg-skilltool').checked = te.skill_loader !== false;
  $('#cfg-filereader').checked = te.file_reader !== false;
  $('#cfg-mcptool').checked = te.mcp !== false;
  $('#cfg-sysaccess').checked = !!te.system_access;
  $('#cfg-outdir').value = c.default_output_dir || '~/Desktop/AI产出';
  $('#sysaccess-warn').classList.toggle('hidden', !te.system_access);
  $('#cfg-theme').value = c.theme || 'light';
  updateProviderHint();
  updateKeyStatus();
  renderCustomList();
  updateCpKeyStatus();
}

function updateProviderHint() {
  const pid = $('#cfg-provider').value;
  const p = S.providers[pid] || {};
  $('#provider-hint').textContent = p.hint || '';
  const list = $('#model-options');
  const models = [...(p.models || []), ...(S.modelsByProvider[pid] || [])];
  list.innerHTML = models.map(m => `<option value="${esc(m)}"></option>`).join('');
}

function updateKeyStatus() {
  const pid = $('#cfg-provider').value;
  const has = (S.config?.has_keys || {})[pid];
  const masked = (S.config?.api_keys || {})[pid];
  const el = $('#key-status');
  el.textContent = has ? `已保存：${masked}` : '尚未配置（填写后点「保存」）';
  el.className = 'hint ' + (has ? 'ok' : 'warn');
}

/* =================================================================== 自定义提供者管理 */

function customEntries() {
  return Object.entries(S.providers || {}).filter(([k, v]) => v.custom);
}

function renderCustomList() {
  const box = $('#cp-list');
  if (!box) return;
  const items = customEntries();
  if (!items.length) {
    box.innerHTML = '<div class="hint">还没有自定义提供者。在下方填名称和 Base URL，点「保存提供者」添加；保存后它会出现左上角模型菜单里。</div>';
    return;
  }
  box.innerHTML = items.map(([pid, p]) => `
    <div class="cp-item ${S.customEditing === pid ? 'active' : ''}" data-pid="${esc(pid)}">
      <span class="cp-name">${esc(p.label)}</span>
      <span class="cp-meta">${esc(p.base_url)} · ${(p.models || []).length} 个模型${(S.config?.has_keys || {})[pid] ? ' · Key ✓' : ' · 缺 Key'}</span>
    </div>`).join('');
  box.querySelectorAll('.cp-item').forEach(el => {
    el.onclick = () => fillCustomEditor(el.dataset.pid);
  });
}

function fillCustomEditor(pid) {
  const p = (S.providers || {})[pid];
  if (!p || !p.custom) return;
  S.customEditing = pid;
  S.cpFetched = [];
  renderCpPicker();
  $('#cp-name').value = p.label || '';
  $('#cp-baseurl').value = p.base_url || '';
  $('#cp-models').value = (p.models || []).join('\n');
  $('#cp-apikey').value = '';
  $('#cp-hint').textContent = `正在编辑「${p.label}」，改完点「保存提供者」生效。`;
  updateCpKeyStatus();
  renderCustomList();
}

function updateCpKeyStatus() {
  const el = $('#cp-key-status');
  if (!el) return;
  const pid = S.customEditing;
  if (!pid) { el.textContent = ''; el.className = 'hint'; return; }
  const has = (S.config?.has_keys || {})[pid];
  const masked = (S.config?.api_keys || {})[pid];
  el.textContent = has
    ? `已保存：${masked}（要换 Key 就粘贴新的再点「保存提供者」）`
    : '该提供者还没有 API Key';
  el.className = 'hint ' + (has ? 'ok' : 'warn');
}

function resetCustomEditor() {
  S.customEditing = null;
  S.cpFetched = [];
  $('#cp-name').value = '';
  $('#cp-baseurl').value = '';
  $('#cp-models').value = '';
  $('#cp-apikey').value = '';
  $('#cp-search').value = '';
  $('#cp-hint').textContent = '';
  renderCpPicker();
  updateCpKeyStatus();
  renderCustomList();
}

function cpSelectedSet() {
  return new Set($('#cp-models').value.split('\n').map(s => s.trim()).filter(Boolean));
}

function renderCpPicker() {
  const box = $('#cp-picker');
  if (!box) return;
  if (!S.cpFetched.length) { box.innerHTML = ''; return; }
  const kw = ($('#cp-search').value || '').trim().toLowerCase();
  const selected = cpSelectedSet();
  const hits = S.cpFetched.filter(m => !kw || m.toLowerCase().includes(kw)).slice(0, 200);
  box.innerHTML = hits.map(m => `
    <label class="cp-opt"><input type="checkbox" value="${esc(m)}" ${selected.has(m) ? 'checked' : ''}>
      <span>${esc(m)}</span></label>`).join('') ||
    '<div class="hint">没有匹配的模型</div>';
  box.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.onchange = () => {
      const set = cpSelectedSet();
      if (cb.checked) set.add(cb.value); else set.delete(cb.value);
      $('#cp-models').value = [...set].join('\n');
      renderCustomList();
    };
  });
}

async function saveCustomProvider() {
  const name = $('#cp-name').value.trim();
  const base = $('#cp-baseurl').value.trim();
  if (!name) { toast('请先填写提供者名称', 'err'); return; }
  if (!base) { toast('请先填写 Base URL', 'err'); return; }
  const models = $('#cp-models').value.split('\n').map(s => s.trim()).filter(Boolean);

  const list = customEntries().map(([pid, p]) =>
    ({ id: pid, label: p.label, base_url: p.base_url, models: p.models || [] }));
  const item = { id: S.customEditing || '', label: name, base_url: base, models };
  if (S.customEditing) {
    const idx = list.findIndex(x => x.id === S.customEditing);
    if (idx >= 0) list[idx] = item; else list.push(item);
  } else {
    list.push(item);
  }

  const r = await API.post('/api/custom_providers', { providers: list });
  if (!r.ok) { toast('保存失败，请重试', 'err'); return; }
  S.providers = r.providers || {};

  let pid = S.customEditing || '';
  if (!pid) {
    const saved = r.saved || [];
    const hit = saved.filter(x => x.label === name).pop();
    pid = hit ? hit.id : '';
  }

  const key = $('#cp-apikey').value.trim();
  if (key && pid) await API.post('/api/apikey', { provider: pid, api_key: key });

  S.customEditing = pid || null;
  await loadConfig();
  checkHealth();
  if (pid) {
    $('#cfg-provider').value = pid;
    $('#cfg-provider').onchange();
  }
  toast(`提供者「${name}」已保存（${models.length} 个模型）`, 'ok');
}

async function deleteCustomProvider() {
  if (!S.customEditing) { toast('先在列表里点选一家再删除', 'err'); return; }
  const pid = S.customEditing;
  const p = S.providers[pid];
  if (!confirm(`删除自定义提供者「${p.label}」？（不影响其它提供者）`)) return;
  const list = customEntries().filter(([k]) => k !== pid)
    .map(([k, v]) => ({ id: k, label: v.label, base_url: v.base_url, models: v.models || [] }));
  const r = await API.post('/api/custom_providers', { providers: list });
  if (!r.ok) { toast('删除失败', 'err'); return; }
  await API.post('/api/apikey', { provider: pid, api_key: '' });
  S.providers = r.providers || {};
  if ((S.config?.provider) === pid) {
    const fallback = customEntries()[0] || ['deepseek', S.providers.deepseek];
    await API.post('/api/config', { patch: { provider: fallback[0], model: (fallback[1].models || [])[0] || '' } });
  }
  await loadConfig();
  resetCustomEditor();
  checkHealth();
  toast('已删除', 'ok');
}

async function fetchCustomModels() {
  const base = $('#cp-baseurl').value.trim();
  if (!base) { toast('请先填写 Base URL', 'err'); return; }
  const btn = $('#btn-cp-fetch');
  btn.disabled = true; btn.textContent = '拉取中…';
  try {
    const r = await API.post('/api/models', {
      provider: S.customEditing || 'custom',
      base_url: base,
      api_key: $('#cp-apikey').value.trim(),
    });
    if (r.ok && r.models.length) {
      S.cpFetched = r.models;
      $('#cp-search').value = '';
      renderCpPicker();
      $('#cp-hint').className = 'hint ok';
      $('#cp-hint').textContent = `拉取到 ${r.count} 个模型。在下方搜索并勾选想用的（勾选 ≠ 全要，只有勾的会出现在左上角菜单），然后点「保存提供者」。`;
    } else {
      $('#cp-hint').className = 'hint err';
      $('#cp-hint').textContent = (r.error || '拉取失败') + '（也可以直接手填模型名，每行一个）';
    }
  } finally {
    btn.disabled = false; btn.textContent = '拉取模型';
  }
}

/* 顶部「模型」区：显式保存，不依赖测试连接 */
async function saveModelSettings() {
  const pid = $('#cfg-provider').value;
  const model = $('#cfg-model').value.trim();
  const el = $('#save-model-result');
  const patch = {
    provider: pid, model,
    temperature: parseFloat($('#cfg-temp').value) || 0.3,
    max_tokens: parseInt($('#cfg-maxtokens').value) || 32768,
  };
  if (!(S.providers[pid] || {}).custom) {
    patch.base_url_overrides = Object.assign({}, S.config?.base_url_overrides || {},
      { [pid]: $('#cfg-baseurl').value.trim() });
  }
  const r = await API.post('/api/config', { patch });
  if (!r.ok) { el.className = 'hint err'; el.textContent = '保存失败，请重试'; return; }
  S.config = r.config; renderModelLabel(); updateTitleMeta(); checkHealth();
  el.className = 'hint ok';
  el.textContent = `已保存：${(S.providers[pid] || {}).label || pid} · ${model || '（未填模型名）'}。左上角菜单已切换。`;
  toast('模型设置已保存', 'ok');
}

/* =================================================================== 事件绑定 */

function bindEvents() {
  $('#btn-new-task').onclick = () => newTask();
  $('#btn-settings').onclick = () => openDrawer('model');
  $('#btn-close-drawer').onclick = closeDrawer;
  $('#drawer-mask').onclick = closeDrawer;
  $('#task-search').oninput = renderTasks;
  $('#btn-clear').onclick = async () => {
    if (!S.currentTask) return;
    if (!confirm('清空当前任务的对话记录？（任务本身保留）')) return;
    await API.post(`/api/tasks/${S.currentTask}/clear`);
    S.messages = [];
    renderChat();
    toast('已清空', 'ok');
  };

  $('#model-btn').onclick = (e) => {
    e.stopPropagation();
    const m = $('#model-menu');
    if (m.classList.contains('hidden')) openModelMenu(); else m.classList.add('hidden');
  };
  document.addEventListener('click', (e) => {
    if (!$('#model-picker').contains(e.target)) $('#model-menu').classList.add('hidden');
  });

  const toggleSide = () => {
    const collapsed = $('#sidebar').classList.toggle('collapsed');
    try { localStorage.setItem('side', collapsed ? '0' : '1'); } catch (e) {}
  };
  $('#btn-toggle-side').onclick = toggleSide;
  $('#side-float').onclick = toggleSide;
  $('#side-hide').onclick = toggleSide;
  // 启动时恢复上次收起状态
  try { if (localStorage.getItem('side') === '0') $('#sidebar').classList.add('collapsed'); } catch (e) {}
  const autoCollapse = () => {
    // 窗口过窄，或页面出现横向溢出（WKWebView 缩放不重排的典型症状）时自动收起
    if (window.innerWidth < 900 ||
        document.documentElement.scrollWidth > window.innerWidth + 4) {
      $('#sidebar').classList.add('collapsed');
    }
  };
  window.addEventListener('resize', autoCollapse);
  new ResizeObserver(autoCollapse).observe(document.documentElement);
  setInterval(autoCollapse, 700);
  autoCollapse();
  $('#speed-mode').onchange = e => switchSpeedMode(e.target.value);
  $('#cfg-speed-mode').onchange = e => switchSpeedMode(e.target.value);
  $('#btn-send').onclick = send;
  $('#btn-stop').onclick = stopRun;
  $('#btn-attach').onclick = () => $('#file-input').click();
  $('#file-input').onchange = (e) => { uploadFiles(e.target.files); e.target.value = ''; };

  const input = $('#input');
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey) { e.preventDefault(); send(); }
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(); }
  });
  input.addEventListener('input', () => autoGrow(input));

  // 拖拽上传
  const main = $('#main');
  let dragDepth = 0;
  main.addEventListener('dragenter', (e) => { e.preventDefault(); dragDepth++; $('#drop-hint').classList.remove('hidden'); });
  main.addEventListener('dragover', (e) => e.preventDefault());
  main.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; $('#drop-hint').classList.add('hidden'); } });
  main.addEventListener('drop', (e) => {
    e.preventDefault(); dragDepth = 0; $('#drop-hint').classList.add('hidden');
    if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
  });
  // 粘贴图片
  document.addEventListener('paste', (e) => {
    const items = Array.from(e.clipboardData?.items || []);
    const files = items.filter(i => i.kind === 'file').map(i => i.getAsFile()).filter(Boolean);
    if (files.length) { e.preventDefault(); uploadFiles(files); }
  });

  $$('.tab').forEach(t => t.onclick = () => activateTab(t.dataset.tab));
  $$('.card').forEach(c => c.onclick = () => {
    $('#input').value = c.dataset.prompt;
    autoGrow($('#input'));
    $('#input').focus();
  });

  // 模型设置
  $('#cfg-provider').onchange = () => {
    const pid = $('#cfg-provider').value;
    const p = S.providers[pid] || {};
    $('#cfg-baseurl').value = (S.config?.base_url_overrides || {})[pid]
      || (p.custom ? (p.base_url || '') : '');
    $('#cfg-model').value = (p.models || [])[0] || (S.config?.model || '');
    updateProviderHint(); updateKeyStatus();
    if (p.custom) fillCustomEditor(pid);
  };
  $('#btn-reset-url').onclick = () => { $('#cfg-baseurl').value = ''; toast('已还原为内置地址'); };
  $('#btn-save-key').onclick = async () => {
    const pid = $('#cfg-provider').value;
    const key = $('#cfg-apikey').value.trim();
    if (!key) { toast('请先粘贴 API Key', 'err'); return; }
    await API.post('/api/apikey', { provider: pid, api_key: key });
    $('#cfg-apikey').value = '';
    await loadConfig(); checkHealth();
    toast('API Key 已保存到本机', 'ok');
  };
  $('#btn-fetch-models').onclick = async () => {
    const pid = $('#cfg-provider').value;
    $('#models-hint').textContent = '正在拉取…';
    const r = await API.post('/api/models', {
      provider: pid, base_url: $('#cfg-baseurl').value.trim(), api_key: $('#cfg-apikey').value.trim(),
    });
    if (r.ok) {
      S.modelsByProvider[pid] = r.models;
      updateProviderHint();
      $('#models-hint').className = 'hint ok';
      $('#models-hint').textContent = `拉取到 ${r.count} 个模型，点模型输入框可下拉选择`;
    } else {
      $('#models-hint').className = 'hint err';
      $('#models-hint').textContent = r.error || '拉取失败';
    }
  };
  $('#btn-test-model').onclick = async () => {
    const pid = $('#cfg-provider').value;
    const model = $('#cfg-model').value.trim();
    const el = $('#test-result');
    el.className = 'hint'; el.textContent = '正在测试…';
    const patch = {
      provider: pid, model,
      temperature: parseFloat($('#cfg-temp').value) || 0.3,
      max_tokens: parseInt($('#cfg-maxtokens').value) || 32768,
    };
    if (!(S.providers[pid] || {}).custom) {
      patch.base_url_overrides = Object.assign({}, S.config?.base_url_overrides || {},
        { [pid]: $('#cfg-baseurl').value.trim() });
    }
    const r = await API.post('/api/config', { patch });
    S.config = r.config; renderModelLabel(); updateTitleMeta();
    const t = await API.post('/api/models', { provider: pid, model });
    el.className = 'hint ' + (t.ok ? 'ok' : 'err');
    if (t.ok) el.textContent = `连接成功，服务端返回 ${t.count} 个模型，当前选择「${model}」可用。`;
    else el.textContent = t.error;
  };

  // 自定义提供者管理 + 模型保存按钮
  $('#btn-cp-save').onclick = saveCustomProvider;
  $('#btn-cp-new').onclick = resetCustomEditor;
  $('#btn-cp-delete').onclick = deleteCustomProvider;
  $('#btn-cp-fetch').onclick = fetchCustomModels;
  $('#cp-search').oninput = renderCpPicker;
  $('#btn-save-model').onclick = saveModelSettings;
  $('#btn-save-general').onclick = async () => {
    const r = await API.post('/api/config', { patch: {
      system_prompt: $('#cfg-system').value,
      context_rounds: parseInt($('#cfg-rounds').value) || 20,
      agent_max_steps: Math.min(10000, Math.max(0, Number.parseInt($('#cfg-steps').value) || 0)),
      agent_idle_timeout: Math.min(3600, Math.max(0, Number.parseInt($('#cfg-idle-timeout').value) || 0)),
      auto_title: $('#cfg-autotitle').checked,
      theme: $('#cfg-theme').value,
      tools_enabled: {
        calculator: $('#cfg-calculator').checked,
        knowledge_search: $('#cfg-knowledge').checked,
        skill_loader: $('#cfg-skilltool').checked,
        file_reader: $('#cfg-filereader').checked,
        mcp: $('#cfg-mcptool').checked,
        system_access: $('#cfg-sysaccess').checked,
      },
      default_output_dir: $('#cfg-outdir').value.trim() || '~/Desktop/AI产出',
    }});
    S.config = r.config;
    applyTheme($('#cfg-theme').value);
    toast('设置已保存', 'ok');
  };

  bindMCP();
  bindSkills();
  bindKB();
}

function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 220) + 'px';
}

/* =================================================================== MCP */

let editingMCP = null;

function bindMCP() {
  $('#btn-mcp-add').onclick = () => showMCPEditor(null);
  $('#btn-mcp-cancel').onclick = () => $('#mcp-editor').classList.add('hidden');
  $('#btn-mcp-save').onclick = saveMCP;
  $('#btn-mcp-test').onclick = testMCP;
}

async function refreshPanes() {
  if ($('[data-pane="mcp"]').classList.contains('active')) loadMCP();
  if ($('[data-pane="skills"]').classList.contains('active')) loadSkills();
  if ($('[data-pane="kb"]').classList.contains('active')) loadKB();
  if ($('[data-pane="about"]').classList.contains('active')) $('#about-version').textContent = '1.0.0';
}

async function loadMCP() {
  const r = await API.get('/api/mcp');
  const list = $('#mcp-list');
  const servers = r.servers || [];
  if (!servers.length) {
    list.innerHTML = '<div class="empty-tip">还没有 MCP 服务器。点下方按钮添加。</div>';
    return;
  }
  list.innerHTML = servers.map((s, i) => {
    const st = s.status || {};
    const toolCount = (st.tools || []).length;
    // 健康状态只看「就绪 + 有工具」，不看 st.error —— 服务器会把正常进度日志
    // 写到 stderr，旧版把它当错误显示，导致健康的服务器看起来是坏的。
    const healthy = !!st.running && !!st.ready && toolCount > 0;
    const cls = healthy ? 'on' : (st.running ? 'warn' : (st.error ? 'err' : 'off'));
    return `<div class="list-item">
      <div class="li-head">
        <div class="li-name">${esc(s.config.name)}</div>
        <span class="pill ${cls}">${esc(st.status || '未启动')}</span>
        ${toolCount ? `<span class="pill">${toolCount} 个工具</span>` : ''}
      </div>
      <div class="li-desc">${esc(s.config.command || '')} ${esc((s.config.args || []).join(' '))}
        ${st.error ? '<br><span style="color:var(--red)">错误：' + esc(st.error) + '</span>' : ''}
        ${(!st.error && st.log_tail) ? '<br><span style="color:var(--text-3);font-size:11px">日志：' + esc(st.log_tail) + '</span>' : ''}</div>
      <div class="li-actions">
        <button class="mini-btn" data-act="test" data-i="${i}">测试/重启</button>
        <button class="mini-btn" data-act="stop" data-i="${i}">停止</button>
        <button class="mini-btn" data-act="tools" data-i="${i}">查看工具</button>
        <button class="mini-btn" data-act="edit" data-i="${i}">编辑</button>
        <button class="mini-btn danger" data-act="del" data-i="${i}">删除</button>
      </div>
      <div class="tool-body hidden" data-tools="${i}">
        ${(st.tools || []).map(t => `<div class="tool-label">${esc(t.name)}</div><pre>${esc(t.description)}</pre>
          <button class="mini-btn" data-call="${esc(t.name)}" data-srv="${esc(s.config.name)}">调用测试</button>`).join('') || '<pre>暂无工具信息，请先测试连接</pre>'}
      </div>
    </div>`;
  }).join('');

  list.querySelectorAll('button[data-call]').forEach(b => {
    b.onclick = async (e) => {
      e.stopPropagation();
      const srv = b.dataset.srv, tool = b.dataset.call;
      const raw = prompt(`调用 ${srv}.${tool}\n请输入参数 JSON（无参数直接确定）：`, '{}');
      if (raw === null) return;
      let args = {};
      try { args = JSON.parse(raw || '{}'); }
      catch { toast('JSON 格式有误', 'err'); return; }
      toast('调用中…');
      const r = await API.post('/api/mcp/call', { server: srv, tool, arguments: args });
      alert((r.ok ? '调用成功\n\n' : '调用失败\n\n') + String(r.result).slice(0, 1500));
    };
  });

  list.querySelectorAll('button[data-act]').forEach(b => {
    b.onclick = async () => {
      const i = +b.dataset.i, act = b.dataset.act, s = servers[i];
      const name = s.config.name;
      if (act === 'test') {
        toast('正在测试连接…');
        const t = await API.post('/api/mcp/test', { name, restart: true });
        toast(t.ok ? `${name} 连接成功` : `${name} 失败：${t.error || ''}`, t.ok ? 'ok' : 'err');
        loadMCP();
      } else if (act === 'stop') {
        await API.post('/api/mcp/stop', { name });
        loadMCP();
      } else if (act === 'tools') {
        const box = list.querySelector(`[data-tools="${i}"]`);
        box.classList.toggle('hidden');
      } else if (act === 'edit') {
        showMCPEditor(s.config);
      } else if (act === 'del') {
        if (!confirm(`删除 MCP 服务器「${name}」？`)) return;
        const rest = servers.filter((_, k) => k !== i).map(x => x.config);
        await API.post('/api/mcp/save', { servers: rest });
        toast('已删除', 'ok');
        loadMCP();
      }
    };
  });
}

function showMCPEditor(cfg) {
  editingMCP = cfg ? cfg.name : null;
  $('#mcp-editor').classList.remove('hidden');
  $('#mcp-name').value = cfg?.name || '';
  $('#mcp-name').disabled = !!cfg;
  $('#mcp-command').value = cfg?.command || '';
  $('#mcp-args').value = (cfg?.args || []).join('\n');
  $('#mcp-env').value = Object.entries(cfg?.env || {}).map(([k, v]) => `${k}=${v}`).join('\n');
  $('#mcp-enabled').checked = cfg ? cfg.enabled !== false : true;
  $('#mcp-autostart').checked = !!cfg?.auto_start;
  $('#mcp-result').textContent = '';
}

async function collectMCP() {
  const r = await API.get('/api/mcp');
  const servers = (r.servers || []).map(x => x.config);
  const item = {
    name: $('#mcp-name').value.trim(),
    command: $('#mcp-command').value.trim(),
    args: $('#mcp-args').value.split('\n').map(s => s.trim()).filter(Boolean),
    env: Object.fromEntries($('#mcp-env').value.split('\n').map(l => l.trim()).filter(Boolean)
      .map(l => { const i = l.indexOf('='); return [l.slice(0, i).trim(), l.slice(i + 1).trim()]; })),
    enabled: $('#mcp-enabled').checked,
    auto_start: $('#mcp-autostart').checked,
  };
  const idx = servers.findIndex(s => s.name === item.name);
  if (idx >= 0) servers[idx] = item; else servers.push(item);
  return servers;
}

async function saveMCP() {
  if (!$('#mcp-name').value.trim() || !$('#mcp-command').value.trim()) {
    toast('名称和启动命令必填', 'err'); return;
  }
  const servers = await collectMCP();
  const r = await API.post('/api/mcp/save', { servers });
  toast(r.ok ? 'MCP 配置已保存' : '保存失败', r.ok ? 'ok' : 'err');
  $('#mcp-editor').classList.add('hidden');
  loadMCP();
}

async function testMCP() {
  await saveMCP();
  const name = $('#mcp-name').value.trim();
  $('#mcp-result').className = 'hint';
  $('#mcp-result').textContent = '正在启动并握手…';
  const t = await API.post('/api/mcp/test', { name, restart: true });
  if (t.ok) {
    const n = (t.status.tools || []).length;
    $('#mcp-result').className = 'hint ok';
    $('#mcp-result').textContent = `连接成功，发现 ${n} 个工具：` +
      (t.status.tools || []).map(x => x.name).join('、');
  } else {
    $('#mcp-result').className = 'hint err';
    $('#mcp-result').textContent = t.error || t.status?.error || '连接失败';
  }
  loadMCP();
}

/* =================================================================== 技能 */

function bindSkills() {
  $('#btn-skill-scan').onclick = loadSkills;
  $('#btn-skill-new').onclick = () => {
    $('#skill-editor').classList.remove('hidden');
    $('#skill-name').value = ''; $('#skill-desc').value = ''; $('#skill-content').value = '';
  };
  $('#btn-skill-cancel').onclick = () => $('#skill-editor').classList.add('hidden');
  $('#btn-skill-create').onclick = async () => {
    const name = $('#skill-name').value.trim();
    if (!name) { toast('请填写技能名称', 'err'); return; }
    await API.post('/api/skills/create', {
      name, description: $('#skill-desc').value.trim(), content: $('#skill-content').value,
    });
    toast('技能已创建', 'ok');
    $('#skill-editor').classList.add('hidden');
    loadSkills();
  };
  $('#btn-skill-import').onclick = async () => {
    const path = prompt('输入技能文件夹路径（包含 SKILL.md），或直接输入 SKILL.md 所在目录：');
    if (!path) return;
    const r = await API.post('/api/skills/import', { path });
    toast(r.ok ? '导入成功' : ('导入失败：' + (r.error || '')), r.ok ? 'ok' : 'err');
    loadSkills();
  };
  $('#btn-peek-close').onclick = () => $('#skill-peek').classList.add('hidden');
}

async function loadSkills() {
  const r = await API.get('/api/skills');
  $('#skill-dir').textContent = (r.dirs || [])[0] || '';
  const list = $('#skill-list');
  const skills = r.skills || [];
  if (!skills.length) {
    list.innerHTML = '<div class="empty-tip">还没有技能。可以「新建技能」，或把已有的技能文件夹导入进来。</div>';
    return;
  }
  list.innerHTML = skills.map((s, i) => `
    <div class="list-item">
      <div class="li-head">
        <div class="li-name">${esc(s.name)}</div>
        <span class="pill ${s.enabled ? 'on' : 'off'}">${s.enabled ? '已启用' : '已停用'}</span>
        ${s.agent_created ? '<span class="pill">AI 创建</span>' : ''}
      </div>
      <div class="li-desc">${esc(s.description || '（无描述）')}</div>
      <div class="li-actions">
        <button class="mini-btn" data-act="peek" data-i="${i}">查看全文</button>
        <button class="mini-btn" data-act="toggle" data-i="${i}">${s.enabled ? '停用' : '启用'}</button>
        <button class="mini-btn danger" data-act="del" data-i="${i}">删除</button>
      </div>
    </div>`).join('');

  list.querySelectorAll('button[data-act]').forEach(b => {
    b.onclick = async () => {
      const s = skills[+b.dataset.i];
      if (b.dataset.act === 'peek') {
        const d = await API.get('/api/skills/' + encodeURIComponent(s.name));
        $('#peek-name').textContent = s.name + ' · ' + (s.size / 1024).toFixed(1) + 'KB';
        $('#peek-body').textContent = d.text || '';
        $('#skill-peek').classList.remove('hidden');
      } else if (b.dataset.act === 'toggle') {
        await API.post('/api/skills/toggle', { name: s.name, enabled: !s.enabled });
        loadSkills();
      } else if (b.dataset.act === 'del') {
        if (!confirm(`删除技能「${s.name}」？将从磁盘移除该文件夹。`)) return;
        const r = await API.del('/api/skills/' + encodeURIComponent(s.name));
        toast(r.ok ? '已删除' : ('删除失败：' + (r.error || '')), r.ok ? 'ok' : 'err');
        loadSkills();
      }
    };
  });
}

/* =================================================================== 知识库 */

function bindKB() {
  $('#btn-kb-refresh').onclick = loadKB;
  $('#btn-kb-create').onclick = async () => {
    const name = $('#kb-new-name').value.trim();
    if (!name) { toast('请填写知识库名称', 'err'); return; }
    await API.post('/api/knowledge/create', { name });
    $('#kb-new-name').value = '';
    toast('已创建，请把资料放进对应目录', 'ok');
    loadKB();
  };
  $('#btn-kb-index-all').onclick = async () => {
    toast('正在建立索引…');
    const r = await API.post('/api/knowledge/index_all', {});
    const total = (r.results || []).reduce((a, x) => a + (x.chunks || 0), 0);
    toast(`索引完成，共 ${total} 个分片`, 'ok');
    loadKB();
  };
  $('#btn-kb-search').onclick = async () => {
    const q = $('#kb-query').value.trim();
    if (!q) return;
    const r = await API.post('/api/knowledge/search', { query: q, top_k: 5 });
    const box = $('#kb-search-result');
    if (r.note) { box.innerHTML = `<div class="hint warn">${esc(r.note)}</div>`; return; }
    if (!r.hits.length) { box.innerHTML = '<div class="hint">没有检索到相关内容</div>'; return; }
    box.innerHTML = r.hits.map(h => `<div class="sr-item">
        <div class="sr-head">${esc(h.kb)} / ${esc(h.file)} · 第 ${h.chunk_no + 1} 段 · 得分 ${h.score}</div>
        <div class="sr-text">${esc(h.text.slice(0, 300))}</div></div>`).join('');
  };
}

async function loadKB() {
  const r = await API.get('/api/knowledge');
  $('#kb-dir').textContent = r.dir || '';
  const bases = r.bases || [];
  const list = $('#kb-list');
  if (!bases.length) {
    list.innerHTML = '<div class="empty-tip">还没有知识库。可以新建一个，然后把 PDF/Word/Excel/txt 放进去。</div>';
    return;
  }
  list.innerHTML = bases.map((k, i) => `
    <div class="list-item">
      <div class="li-head">
        <div class="li-name">${esc(k.name)}</div>
        <span class="pill ${k.indexed ? 'on' : 'off'}">${k.indexed ? k.chunks + ' 分片' : '未建索引'}</span>
        <span class="pill ${k.enabled ? 'on' : 'off'}">${k.enabled ? '启用' : '停用'}</span>
      </div>
      <div class="li-desc">${k.files} 个文件${k.file_list?.length ? '：' + esc(k.file_list.slice(0, 6).join('、')) + (k.files > 6 ? ' …' : '') : '（目录为空）'}</div>
      <div class="li-actions">
        <button class="mini-btn" data-act="index" data-i="${i}">重建索引</button>
        <button class="mini-btn" data-act="upload" data-i="${i}">上传文件</button>
        <button class="mini-btn" data-act="toggle" data-i="${i}">${k.enabled ? '停用' : '启用'}</button>
      </div>
    </div>`).join('');

  list.querySelectorAll('button[data-act]').forEach(b => {
    b.onclick = async () => {
      const k = bases[+b.dataset.i];
      if (b.dataset.act === 'index') {
        toast('正在建立索引…');
        const res = await API.post('/api/knowledge/index', { name: k.name });
        toast(`完成：${res.files || 0} 个文件 / ${res.chunks || 0} 个分片`, 'ok');
        loadKB();
      } else if (b.dataset.act === 'toggle') {
        await API.post('/api/knowledge/toggle', { name: k.name, enabled: !k.enabled });
        loadKB();
      } else if (b.dataset.act === 'upload') {
        const inp = document.createElement('input');
        inp.type = 'file'; inp.multiple = true;
        inp.onchange = async () => {
          for (const f of inp.files) {
            const fd = new FormData();
            fd.append('kb', k.name); fd.append('file', f);
            await fetch('/api/knowledge/upload', { method: 'POST', body: fd });
          }
          toast('上传并索引完成', 'ok');
          loadKB();
        };
        inp.click();
      }
    };
  });
}

/* Dedicated local financial workflow, usable without a model account. */
const FINANCIAL_LABELS = {
  revenue:'营业收入', net_profit:'净利润', parent_net_profit:'归母净利润', nonrecurring_adj_parent_profit:'扣非归母净利润',
  operating_cash_flow:'经营现金流', assets:'总资产', receivables:'应收账款', inventory:'存货',
  gross_margin:'毛利率', net_margin:'净利率', operating_margin:'营业利润率', operating_cash_margin:'经营现金流收入比',
  cash_conversion:'经营现金流净利润比', debt_ratio:'资产负债率', current_ratio:'流动比率', quick_ratio:'速动比率',
  cash_ratio:'现金比率', free_cash_flow:'经营现金流减资本开支', roe:'归母净资产收益率', roe_total:'总权益收益率',
  roa:'总资产收益率', inventory_turnover:'存货周转次数', receivables_turnover:'应收周转次数', asset_turnover:'总资产周转次数',
  inventory_days:'存货周转天数', receivables_days:'应收周转天数', nonrecurring_profit_share:'扣非前后归母利润差额占比',
  consolidated:'合并', parent:'母公司', unknown:'待确认', verified:'已核验', needs_review:'待复核',
  calculated:'已计算', unavailable:'不可计算', ratio:'比例小数', days:'天', annual:'年度', ytd:'年初累计', quarter:'单季', instant:'时点'
  , CNY:'人民币元', USD:'美元', pass:'通过', fail:'需核对', attention:'需关注',
  balance_sheet_equation:'资产负债权益平衡', asset_subtotals:'资产小计', liability_subtotals:'负债小计', equity_subtotals:'权益归属',
  net_profit_equation:'税前税后利润', profit_attribution:'利润归属', pretax_profit_equation:'利润总额',
  cash_equivalents_rollforward:'现金余额变动', cash_flow_components:'现金流构成',
  weak_profit_cash_conversion:'利润现金转化偏弱', nonrecurring_profit_concentration:'扣非利润差异较大',
  receivables_growth_ahead_of_revenue:'应收增长快于收入', inventory_growth_ahead_of_revenue:'存货增长快于收入'
};
function financialLabel(value) {
  if (FINANCIAL_LABELS[value]) return FINANCIAL_LABELS[value];
  if (typeof value === 'string' && value.endsWith('_yoy')) return financialLabel(value.slice(0,-4)) + '同比';
  if (typeof value === 'string' && value.endsWith('_quarter')) return financialLabel(value.slice(0,-8)) + '单季';
  return value;
}
function financialTable(rows, columns) {
  if (!rows.length) return '<p class="dim">暂无可用数据。缺少的科目或口径会在复核清单中说明。</p>';
  return '<div class="finance-table"><table><thead><tr>' + columns.map(c => '<th>' + esc(c[1]) + '</th>').join('') +
    '</tr></thead><tbody>' + rows.map(r => '<tr>' + columns.map(c => {
      let value = r[c[0]];
      if (['code','scope','status','unit','period_type'].includes(c[0])) value = financialLabel(value);
      if (c[0] === 'evidence_url') return '<td>' + (value ? '<a href="' + esc(value) + '" target="_blank" rel="noopener">查看原文</a>' : '—') + '</td>';
      if (c[0] === 'confirm') return '<td>' + (r.can_confirm ? '<button class="mini-btn finance-confirm" data-fact="' + esc(r.fact_id) + '">确认取数</button>' : '—') + '</td>';
      if (value && typeof value === 'object') value = JSON.stringify(value);
      return '<td>' + esc(value == null ? '—' : value) + '</td>';
    }).join('') + '</tr>').join('') + '</tbody></table></div>';
}

function financialMetricRows(metrics) {
  const multiples = new Set(['current_ratio','quick_ratio','cash_ratio','cash_conversion','inventory_turnover','receivables_turnover','asset_turnover']);
  return metrics.map(metric => {
    const row = {...metric}, value = Number(metric.value);
    if (metric.value != null && Number.isFinite(value)) {
      if (metric.unit === 'ratio') {
        const multiple = multiples.has(metric.code);
        row.display_value = (multiple ? value : value*100).toFixed(2);
        row.display_unit = multiple ? '倍' : '%';
      } else {
        row.display_value = value.toLocaleString('zh-CN', {maximumFractionDigits:2});
        row.display_unit = financialLabel(metric.unit);
      }
    } else { row.display_value = '—'; row.display_unit = financialLabel(metric.unit); }
    return row;
  }).sort((a,b) => String(b.period).localeCompare(String(a.period)));
}

function showFinancialRun(run) {
  if (run.kind === 'finance_workflow') return showFinanceWorkflow(run);
  if (run.kind === 'audit' && run.claim_review) return showReportReview(run);
  if (run.kind === 'valuation_model' || run.engine_version || run.complete_result?.engine_version) return showValuationModel(run);
  let dialog = $('#finance-dialog');
  if (!dialog) {
    dialog = document.createElement('dialog');
    dialog.id = 'finance-dialog';
    document.body.appendChild(dialog);
  }
  const base = '/api/financial/artifacts/' + encodeURIComponent(S.currentTask) + '/' + encodeURIComponent(run.run_id) + '/';
  const facts = run.facts || [], metrics = run.metrics || [], reviews = run.review_queue || [];
  dialog.innerHTML = '<div class="finance-head"><div><h2>财报与数据分析结果</h2><p>完整数字、计算依据和复核项目保存在导出文件中。</p></div><button class="mini-btn" id="finance-close">关闭</button></div>' +
    '<div class="finance-stats"><span><b>' + facts.length + '</b> 条财务事实</span><span><b>' + metrics.filter(m => m.status === 'calculated').length +
    '</b> 项可计算指标</span><span><b>' + reviews.length + '</b> 项待复核</span></div>' +
    '<div class="finance-downloads">' + ['xlsx', 'docx', 'json', 'md'].map(f => '<a class="mini-btn" href="' + base + encodeURIComponent('报告.' + f) + '" target="_blank">下载 ' + ({xlsx:'Excel',docx:'Word',json:'数据',md:'报告文本'}[f]) + '</a>').join(' ') + '</div>' +
    '<p class="dim">标准金额已换算为币种基础单位；原始金额与单位保留在导出表中。ratio 为比例小数，例如 0.2 表示 20%。异常信号是核查线索。</p>' +
    '<h3>财务指标</h3>' + financialTable(financialMetricRows(metrics), [['code','指标'],['company','公司'],['period','期间'],['scope','口径'],['display_value','计算值'],['display_unit','单位'],['status','状态'],['formula','计算依据']]) +
    '<h3>异常信号</h3>' + financialTable(run.signals || [], [['code','信号'],['period','期间'],['message','说明'],['rule','触发规则'],['threshold','参考阈值']]) +
    '<h3>勾稽检查</h3>' + financialTable(run.checks || [], [['code','检查项'],['period','期间'],['status','结果'],['difference','差额'],['tolerance','舍入容差']]) +
    '<h3>财务事实与原文</h3>' + financialTable(facts.slice(0, 100), [['raw_label','科目'],['company','公司'],['period','期间'],['value','标准金额'],['currency','币种'],['scope','口径'],['status','状态'],['evidence_url','出处'],['confirm','人工复核']]) +
    (facts.length > 100 ? '<p class="dim">此处展示前 100 条，导出文件包含全部事实。</p>' : '') +
    '<h3>待复核项目</h3>' + financialTable(reviews, [['code','类型'],['message','说明'],['reason','原因'],['suggestion','建议']]) +
    '<h3>数据与适用范围说明</h3><ul>' + (run.warnings || []).map(w => '<li>' + esc(typeof w === 'object' ? JSON.stringify(w) : w) + '</li>').join('') + '</ul>';
  $('#finance-close').onclick = () => dialog.close();
  dialog.querySelectorAll('.finance-confirm').forEach(button => {
    button.onclick = async () => {
      if (!window.confirm('请先点击「查看原文」，核对数值、科目和列位置。确认取数正确后，将保留原始运行并生成新的核验版本。是否确认？')) return;
      button.disabled = true;
      try {
        const result = await API.post('/api/financial/confirm', {task_id:S.currentTask, run_id:run.run_id, fact_ids:[button.dataset.fact]});
        if (!result.ok) throw new Error(result.detail || '确认失败');
        const saved = await API.get('/api/financial/runs/' + S.currentTask + '/' + result.run_id);
        showFinancialRun(saved.run);
      } catch (error) { toast(error.message, 'err'); button.disabled = false; }
    };
  });
  if (!dialog.open) dialog.showModal();
}

function valuationNumber(value, suffix = '元') {
  if (value == null || value === '' || !Number.isFinite(Number(value))) return '—';
  return Number(value).toLocaleString('zh-CN', {maximumFractionDigits: 2}) + (suffix ? ' ' + suffix : '');
}

function showValuationModel(run) {
  const model = run.complete_result && typeof run.complete_result === 'object' ? run.complete_result : run;
  let dialog = $('#finance-dialog');
  if (!dialog) { dialog = document.createElement('dialog'); dialog.id = 'finance-dialog'; document.body.appendChild(dialog); }
  const plan = model.model_plan || {}, rate = model.discount_rate || {};
  const method = ({multi_stage_fcff:'分阶段经营现金流估值',residual_income:'金融企业剩余收益估值'}[plan.method] || '自动估值模型');
  const amountUnit = plan.currency ? financialLabel(plan.currency) : '输入财报币种';
  const scenarioRows = ['base','optimistic','stress'].map(key => {
    const scenario = (model.scenarios || {})[key] || {}, value = scenario.valuation || {};
    return {label:scenario.label || ({base:'基准',optimistic:'乐观',stress:'压力'}[key]),
      equity:valuationNumber(value.equity_value,amountUnit), per_share:valuationNumber(value.value_per_share,amountUnit + ' / 股'),
      status:value.ok ? '可计算' : '待补充数据', reason:value.reason || ''};
  });
  const base = '/api/financial/artifacts/' + encodeURIComponent(S.currentTask) + '/' + encodeURIComponent(run.run_id || model.run_id) + '/';
  const exports = Array.isArray(run.exports) && run.exports.length ? run.exports : ['xlsx','docx','json'].map(format => ({format}));
  const downloads = exports.filter(item => ['xlsx','docx','json','md'].includes(item.format)).map(item => {
    const url = typeof item.url === 'string' && item.url.startsWith('/api/financial/artifacts/') ? item.url : base + encodeURIComponent('报告.' + item.format);
    const label = ({xlsx:'可编辑 Excel 模型',docx:'Word 估值说明',json:'完整数据 JSON',md:'报告文本'}[item.format]);
    return '<a class="mini-btn" href="' + esc(url) + '" target="_blank" rel="noopener">下载 ' + label + '</a>';
  }).join(' ');
  const review = model.valuation_review || {};
  const warnings = [...new Set([...(model.warnings || []), ...(review.reasons || [])]), ...(model.data_needs || [])];
  const checkRows = Array.isArray(model.checks) ? model.checks.map(item => typeof item === 'string' ? {message:item} : {...item,status:item.status || (item.ok === true ? '通过' : item.ok === false ? '需补充或调整' : '—'),message:item.message || item.detail || item.reason || item.note || item.code}) : Object.entries(model.checks || {}).map(([code,result]) => ({code,message: typeof result === 'object' ? JSON.stringify(result) : String(result)}));
  const assumptionNames = {revenue_base:'基期营业收入',revenue_growth:'收入增长率',gross_margin:'毛利率',opex_ratio:'经营费用率',tax_rate:'所得税率',da_to_revenue:'折旧摊销比例',capex_to_revenue:'资本开支比例',receivable_days:'应收账款周转天数',inventory_days:'存货周转天数',payable_days:'应付账款周转天数',wacc:'折现率',terminal_growth:'永续增长率',terminal_roic:'稳态投入资本回报率',shares:'总股本',net_debt:'净债务',minority_interest:'少数股东权益',non_operating_assets:'非经营性资产',impairment_to_revenue:'首年减值比例',near_term_fcff_haircut:'近期现金流折减',sustainable_profit_discount:'持续盈利折减',book_equity:'期初账面权益',net_income:'首年净利润',cost_of_equity:'权益资本成本',roe:'初始净资产收益率',payout_ratio:'分红比例',terminal_roe:'稳态净资产收益率',years:'预测年数'};
  const sourceNames = {default_assumption:'默认建模假设',user_override:'用户指定假设',user_forecast:'用户指定预测',user_forecast_path:'用户指定逐年路径',fact:'财报取数',metric:'财报计算',derived:'财报推导',user_equity_model:'用户指定金融企业假设'};
  const assumptionRows = Object.entries(model.assumption_sources || {}).map(([key,item]) => ({label:assumptionNames[key] || financialLabel(key),source:sourceNames[item?.source] || item?.source || '详见输入证据',note:item?.note || item?.method || ''}));
  const sourceLabel = ({CAPM:'有来源的市场参数推导',explicit_override:'用户指定折现率',user_override:'用户指定折现率',user_equity_model:'用户指定权益资本成本',default_assumption:'默认建模假设'}[rate.method] || rate.method || '详见模型证据');
  const discount = rate.wacc ?? rate.cost_of_equity ?? model.scenarios?.base?.parameters?.wacc;
  const range = model.range || {};
  dialog.innerHTML = '<div class="finance-head"><div><h2>自动估值建模结果</h2><p>' + esc(method) + ' · ' + esc(plan.years || '—') + ' 年预测 · 币种：' + esc(amountUnit) + '</p></div><button class="mini-btn" id="finance-close">关闭</button></div>' +
    '<p>' + (model.ok ? '模型已完成。可以下载 Excel 调整假设和逐年预测。' : '模型尚需补充数据或调整假设。诊断和已获得的证据仍可下载。') + '</p>' +
    '<div class="finance-downloads">' + downloads + '</div>' +
    '<h3>三情景估值</h3>' + financialTable(scenarioRows,[['label','情景'],['equity','权益价值'],['per_share','每股价值'],['status','计算状态'],['reason','说明']]) +
    '<p class="dim">估值区间：' + esc(valuationNumber(range.equity_value_low,amountUnit)) + ' — ' + esc(valuationNumber(range.equity_value_high,amountUnit)) + '。股本缺失时不计算每股价值。</p>' +
    '<h3>估值结论校验</h3><p>' + esc(review.summary || '历史运行没有结论校验，请结合输入和来源复核。') + '</p>' + financialTable(review.method_comparisons || [],[['method','同行方法'],['sample_count','有效样本'],['equity_value_median','同行权益价值中位'],['relative_gap','相对差异（小数）']]) + '<p class="dim">账面净资产不是价值底线；不同方法不自动平均，缺少数据不会被当作零值事实。</p>' +
    '<h3>折现率与假设来源</h3><p>折现率：' + esc(discount == null || discount === '' ? '—' : valuationNumber(Number(discount)*100,'%')) + '；依据：' + esc(sourceLabel) + '。</p>' +
    '<p class="dim">默认市场参数和预测假设需要结合公司情况核验。三表配平状态与融资假设见完整模型报告，未通过勾稽检查时不能声称三表预测通过。风险、可比公司、反向估值和蒙特卡洛结果为本次运行快照；调整 Excel 后需重新运行以更新这些诊断。</p>' +
    financialTable(assumptionRows,[['label','假设'],['source','来源类别'],['note','说明']]) +
    '<h3>待补充数据与风险说明</h3>' + (warnings.length ? '<ul>' + warnings.map(item => '<li>' + esc(typeof item === 'object' ? item.message || item.reason || JSON.stringify(item) : item) + '</li>').join('') + '</ul>' : '<p class="dim">本次运行没有新增数据缺口提示；仍请核对输入证据和默认假设。</p>') +
    '<h3>模型检查</h3>' + financialTable(checkRows,[['status','状态'],['message','说明']]) +
    '<p class="dim">可比公司、反向估值、概率情景及输入证据保存在完整报告和数据文件中。</p>';
  $('#finance-close').onclick = () => dialog.close();
  if (!dialog.open) dialog.showModal();
}

async function valuationModel() {
  if (!S.currentTask) { toast('请先选择任务并上传财报'); return; }
  const button = $('#btn-valuation-model'), taskId = S.currentTask;
  button.disabled = true; button.textContent = '估值建模中…';
  try {
    const result = await API.post('/api/financial/valuation-model', {task_id:taskId,filenames:S.attachments.map(item => item.name)});
    if (!result.run_id) throw new Error(result.detail || result.error || '估值未完成');
    if (S.currentTask !== taskId) { toast('原任务的估值模型已生成，可到该任务查看分析结果'); return; }
    if (result.complete_result || result.scenarios) showValuationModel(result);
    else { const saved = await API.get('/api/financial/runs/' + taskId + '/' + result.run_id); showFinancialRun(saved.run); }
    toast(result.ok ? '估值模型已生成' : '估值诊断已生成，请查看数据缺口');
  } catch (error) { toast(error.message,'err'); }
  finally { button.disabled = false; button.textContent = '自动估值建模'; }
}

function showFinanceWorkflow(run) {
  let dialog=$('#finance-dialog');
  if(!dialog){dialog=document.createElement('dialog');dialog.id='finance-dialog';document.body.appendChild(dialog);}
  const statuses={completed:'已完成',needs_input:'待补充材料',needs_review:'待复核',error:'运行未完成',cancelled:'已停止'};
  const safeLink=(url,label)=> typeof url==='string' && url.startsWith('/api/financial/artifacts/') ? '<a class="mini-btn" target="_blank" rel="noopener" href="'+esc(url)+'">'+esc(label)+'</a>' : '';
  dialog.innerHTML='<div class="finance-head"><h2>五项联动分析结果</h2><button id="finance-close" class="mini-btn">关闭</button></div>'+
    '<p>'+ (run.completed ? '所选步骤已完成。' : '已保留完成的步骤；请按清单补充材料或复核。')+'</p>'+
    '<p class="dim">各步骤使用明确选择的文件与运行结果。质量的可观察项评分不能代替事实核验；需要人工评审的部分不生成完整总分。</p>'+
    (run.stages || []).map(stage=>'<section><h3>'+esc(stage.name)+' · '+esc(statuses[stage.status] || stage.status)+'</h3><p>'+esc(stage.summary)+'</p>'+safeLink(stage.complete_result,'完整结果')+(stage.exports || []).map(e=>safeLink(e.url,'下载 '+e.format)).join(' ')+
      (stage.workbook_check ? '<p>Excel独立复算：'+esc(({matched:'与模型引擎一致',mismatched:'与模型引擎不一致',needs_review:'待复核',unavailable:'未执行'}[stage.workbook_check.status] || stage.workbook_check.status))+'</p>'+safeLink(stage.workbook_check.complete_result,'复算依据') : '')+
      (stage.refusals?.length ? '<p>待补充：'+esc(stage.refusals.join('；'))+'</p>' : '')+
      (stage.warnings?.length ? '<p>'+esc(stage.warnings.join('；'))+'</p>' : '')+
      (stage.observable_score!=null ? '<p>可观察项评分：'+esc(stage.observable_score)+'；覆盖率：'+esc(stage.observable_coverage)+'。完整人工质量总分：待评。</p>':'')+'</section>').join('')+
    '<div class="finance-downloads">'+(run.exports || [{format:'json',url:'/api/financial/artifacts/'+encodeURIComponent(S.currentTask)+'/'+encodeURIComponent(run.run_id)+'/报告.json'},{format:'md',url:'/api/financial/artifacts/'+encodeURIComponent(S.currentTask)+'/'+encodeURIComponent(run.run_id)+'/报告.md'}]).map(e=>safeLink(e.url,'下载联动清单 '+e.format)).join(' ')+'</div>';
  $('#finance-close').onclick=()=>dialog.close();if(!dialog.open)dialog.showModal();
}

async function financeWorkflow() {
  if(!S.currentTask){toast('请先选择任务并上传材料');return;}
  const taskId=S.currentTask;
  try {
    const sources=await API.get('/api/financial/workflow-inputs/'+encodeURIComponent(taskId));if(S.currentTask!==taskId)return;
    let dialog=$('#finance-dialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='finance-dialog';document.body.appendChild(dialog);}
    const names=sources.files || [], labels={extraction:'结构化提取',financial_analysis:'财报分析',valuation:'估值建模',report_check:'研报纠错',quality:'质量评估'};
    const checklist=(prefix)=>names.map((name,i)=>'<label style="display:block"><input type="checkbox" id="'+prefix+i+'" data-file-index="'+i+'"> '+esc(name)+'</label>').join('');
    dialog.innerHTML='<div class="finance-head"><h2>五项联动分析</h2><button id="finance-close" class="mini-btn">关闭</button></div><p>请选择文件的真实用途。待核查研报不能作为它自己的原始财报依据；缺少参数的估值会返回补充清单。</p>'+
      '<h3>本次要完成的能力</h3>'+Object.entries(labels).map(([key,label])=>'<label><input type="checkbox" id="wf-goal-'+key+'" checked> '+esc(label)+' </label>').join('')+
      '<h3>原始财报</h3>'+checklist('wf-fin-')+
      '<p>或复用已有分析：<select id="wf-analysis"><option value="">不复用</option>'+(sources.runs || []).filter(r=>r.kind==='analysis').map(r=>'<option value="'+esc(r.run_id)+'">'+esc((r.filenames || []).join('、') || r.run_id.slice(0,8))+'</option>').join('')+'</select></p>'+
      '<details><summary>公告材料（可选）</summary>'+checklist('wf-ann-')+'</details>'+
      '<h3>待核查或评估的研报</h3><select id="wf-report"><option value="">没有研报 / 粘贴正文</option>'+names.filter(n=>/\.(pdf|docx|txt|md)$/i.test(n)).map(n=>'<option value="'+esc(n)+'">'+esc(n)+'</option>').join('')+'</select><p><textarea id="wf-text" rows="5" style="width:100%;box-sizing:border-box" placeholder="也可粘贴报告正文，附件与正文选一种"></textarea></p>'+
      '<button id="wf-start" class="mini-btn">开始所选分析</button> <button id="wf-stop" class="mini-btn" disabled>停止后续步骤</button><p id="wf-status" role="status"></p>';
    $('#finance-close').onclick=()=>dialog.close();
    $('#wf-start').onclick=async()=>{
      const button=$('#wf-start'), stop=$('#wf-stop'), label=$('#wf-status');
      const runId=crypto.randomUUID(),payload={task_id:taskId,run_id:runId,goals:Object.keys(labels).filter(key=>$('#wf-goal-'+key).checked),financial_filenames:names.filter((n,i)=>$('#wf-fin-'+i).checked),announcement_filenames:names.filter((n,i)=>$('#wf-ann-'+i).checked),report_filename:$('#wf-report').value,report_text:$('#wf-text').value};
      const analysis=$('#wf-analysis').value;if(analysis)payload.analysis_run_id=analysis;
      button.disabled=true;stop.disabled=false;label.textContent='正在分析，已完成步骤会保留。';
      stop.onclick=async()=>{stop.disabled=true;try{await API.post('/api/financial/workflow/cancel',{task_id:taskId,run_id:runId});label.textContent='停止请求已收到，当前步骤结束后停止后续步骤。';}catch(error){stop.disabled=false;label.textContent=error.message;}};
      try{const result=await API.post('/api/financial/workflow',payload);if(S.currentTask===taskId)showFinanceWorkflow(result);else toast('原任务的联动结果已保存');}
      catch(error){button.disabled=false;stop.disabled=true;label.textContent=error.message;}
    };
    if(!dialog.open)dialog.showModal();
  }catch(error){toast(error.message,'err');}
}

function showReportReview(run) {
  let dialog = $('#finance-dialog');
  if (!dialog) { dialog = document.createElement('dialog'); dialog.id = 'finance-dialog'; document.body.appendChild(dialog); }
  const q = run.quality_review || {}, cr = run.claim_review || {}, stats = cr.stats || {};
  const base = '/api/financial/artifacts/' + encodeURIComponent(S.currentTask) + '/' + encodeURIComponent(run.run_id) + '/';
  const statusNames = {verified:'已验证',contradicted:'与证据矛盾',unverifiable:'待核验'};
  const claims = (cr.claims || []).map(c => ({...c,status:statusNames[c.status] || c.status,evidence_ids:(c.evidence_ids || []).join('，')}));
  dialog.innerHTML = '<div class="finance-head"><div><h2>研报核查与质量评估</h2><p>' + esc(run.summary || '') + '</p></div><button class="mini-btn" id="finance-close">关闭</button></div>' +
    '<div class="finance-stats"><span>已验证 <b>' + esc(stats.verified || 0) + '</b></span><span>矛盾 <b>' + esc(stats.contradicted || 0) + '</b></span><span>待核验 <b>' + esc(stats.unverifiable || 0) + '</b></span></div>' +
    '<p><b>' + (q.delivery_blocked ? '存在需要修订或核验的问题' : '机器核查已完成，仍需人工审阅') + '</b></p>' +
    '<p>整体质量总分：' + esc(q.overall_total == null ? '待人工评审' : q.overall_total) + '；已评权重：' + esc(q.assessed_weight ?? '—') + '/100；可评部分折算分：' + esc(q.provisional_score ?? '未评分') + '。折算分只代表已评范围。</p>' +
    '<p class="dim">使用项目内部量表。数字一致仅表示与指定运行相符；未识别的叙述、行业判断和因果解释仍需人工审阅。</p>' +
    '<div class="finance-downloads">' + ['xlsx','docx','json','md'].map(f => '<a class="mini-btn" href="' + base + encodeURIComponent('报告.' + f) + '" target="_blank">下载 ' + ({xlsx:'核查 Excel',docx:'审阅 Word',json:'结构化数据',md:'审阅文本'}[f]) + '</a>').join(' ') + '</div>' +
    '<h3>逐条主张与修订建议</h3>' + financialTable(claims.slice(0,100),[['line','行'],['raw','报告原文'],['status','结果'],['code','科目'],['period','期间'],['scope','口径'],['expected_value','依据值'],['reason','核查依据'],['advice','修订建议'],['evidence_ids','证据']]) +
    (claims.length > 100 ? '<p>页面展示前100条；完整清单请下载审阅文件。</p>' : '') +
    '<h3>质量评估依据</h3>' + financialTable(q.dimensions || [],[['name','维度'],['assessed_score','实际得分'],['assessed_weight','已评权重'],['max_score','全部权重'],['coverage','覆盖比例'],['status','评估状态'],['basis','依据']]) +
    '<h3>其他核查问题</h3>' + financialTable(run.issues || [],[['severity','程度'],['type','问题'],['line','行'],['detail','内容'],['advice','建议']]) +
    '<h3>适用边界</h3><ul>' + (q.limitations || []).map(x=>'<li>'+esc(x)+'</li>').join('') + '</ul>';
  $('#finance-close').onclick = () => dialog.close();
  if (!dialog.open) dialog.showModal();
}

async function reportReview() {
  if (!S.currentTask) { toast('请先选择任务'); return; }
  const taskId = S.currentTask;
  try {
    const sources = await API.get('/api/financial/report-sources/' + encodeURIComponent(taskId));
    if (S.currentTask !== taskId) return;
    let dialog = $('#finance-dialog');
    if (!dialog) { dialog = document.createElement('dialog'); dialog.id = 'finance-dialog'; document.body.appendChild(dialog); }
    const options = kind => (sources.runs || []).filter(r=>kind.includes(r.kind)).map(r=>'<option value="'+esc(r.run_id)+'">'+esc((r.company || r.kind)+' · '+r.run_id.slice(0,12))+'</option>').join('');
    dialog.innerHTML = '<div class="finance-head"><h2>选择研报与核查依据</h2><button id="finance-close" class="mini-btn">关闭</button></div>' +
      '<p>报告与原始财报应属于同一公司、期间和版本。没有依据时仍可查看待核验清单，但不能确认数字正确。</p>' +
      '<label>报告附件 <select id="review-file"><option value="">粘贴正文</option>'+ (sources.files || []).map(f=>'<option value="'+esc(f)+'">'+esc(f)+'</option>').join('')+'</select></label>' +
      '<p><textarea id="review-text" rows="9" style="width:100%;box-sizing:border-box" placeholder="或粘贴需要核查的研报正文；仅选择一份研报，不把年报当研报。"></textarea></p>' +
      '<p><label>财报分析依据 <select id="review-analysis"><option value="">无依据 / 仅使用正文明确引用的运行</option>'+options(['analysis'])+'</select></label></p>' +
      '<p><label>估值依据 <select id="review-valuation"><option value="">不使用</option>'+options(['valuation','valuation_model'])+'</select></label></p>' +
      '<p><label>法证依据 <select id="review-forensic"><option value="">不使用</option>'+options(['forensic'])+'</select></label></p>' +
      '<button class="mini-btn" id="review-start">开始本地核查与评估</button><p id="review-error" role="status"></p>';
    $('#finance-close').onclick=()=>dialog.close();
    $('#review-start').onclick = async () => {
      const button=$('#review-start'); button.disabled=true; button.textContent='本地核查中…';
      const payload={task_id:taskId,text:$('#review-text').value,filename:$('#review-file').value};
      for (const [selector,key] of [['#review-analysis','run_id'],['#review-valuation','valuation_run_id'],['#review-forensic','forensic_run_id']]) { const value=$(selector).value; if(value) payload[key]=value; }
      try { const result=await API.post('/api/financial/report-review',payload); if(S.currentTask===taskId) showReportReview(result); else toast('原任务的研报审阅已保存'); }
      catch(error) { const label=$('#review-error'); if(label) label.textContent=error.message; button.disabled=false; button.textContent='开始本地核查与评估'; }
    };
    if(!dialog.open) dialog.showModal();
  } catch(error) { toast(error.message,'err'); }
}

async function financialCheck() {
  if (!S.currentTask) { toast('请先新建任务并上传财报'); return; }
  const button = $('#btn-financial-check');
  button.disabled = true;
  button.textContent = '取数与核验中…';
  try {
    const taskId = S.currentTask;
    const result = await API.post('/api/financial/analyze', {task_id: taskId, filenames: S.attachments.map(a => a.name)});
    if (!result.ok) throw new Error(result.detail || result.error || '分析未完成');
    if (S.currentTask !== taskId) { toast('原任务的财报分析已完成，可到该任务查看分析结果'); return; }
    const saved = await API.get('/api/financial/runs/' + taskId + '/' + result.run_id);
    showFinancialRun(saved.run);
    toast('本地财报核验完成');
  } catch (error) { toast(error.message, 'err'); }
  finally { button.disabled = false; button.textContent = '本地财报核验'; }
}

async function latestFinancialResult() {
  if (!S.currentTask) { toast('请先选择任务'); return; }
  try {
    const result = await API.get('/api/financial/runs/' + S.currentTask);
    if (!result.runs || !result.runs.length) { toast('当前任务尚无分析结果，请上传财报后点击「本地财报核验」'); return; }
    const saved = await API.get('/api/financial/runs/' + S.currentTask + '/' + result.runs[0].run_id);
    showFinancialRun(saved.run);
  } catch (error) { toast(error.message, 'err'); }
}

$('#btn-financial-check').onclick = financialCheck;
$('#btn-valuation-model').onclick = valuationModel;
$('#btn-report-review').onclick = reportReview;
$('#btn-finance-workflow').onclick = financeWorkflow;
$('#btn-financial-results').onclick = latestFinancialResult;
boot();
