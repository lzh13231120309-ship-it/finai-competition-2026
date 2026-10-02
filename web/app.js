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
  const t = S.tasks.find(x => x.id === S.currentTask);
  $('#task-meta').textContent = `${cfg.provider || ''} · ${cfg.model || '未选模型'}${t ? ' · ' + (t.message_count || 0) + ' 条消息' : ''}`;
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

  if (run && !run.finished && run.blocks && run.blocks.length) {
    const node = assistantNode(run.blocks, true);
    chat.appendChild(node);
    run.node = node;
    run.container = node.querySelector('.bubble-wrap');
    bindLiveUpdate(run);
  } else if (run && !run.finished) {
    const node = assistantNode([{ type: 'text', text: '', done: false }], true);
    chat.appendChild(node);
    run.node = node;
    run.container = node.querySelector('.bubble-wrap');
    bindLiveUpdate(run);
  }
  scrollBottom();
}

function safeJson(s) {
  try { return JSON.parse(s || '{}'); } catch { return { _raw: s }; }
}

function userNode(m) {
  const div = document.createElement('div');
  div.className = 'msg user';
  const atts = (m.attachments || []).map(a =>
    `<span class="att-chip">${esc(a.name)}</span>`).join('');
  div.innerHTML = `<div class="avatar">我</div>
    <div class="bubble-wrap">
      <div class="role-line">你</div>
      ${atts ? `<div class="att-chips">${atts}</div>` : ''}
      <div class="bubble">${md(m.content || '')}</div>
    </div>`;
  return div;
}

function assistantNode(blocks, streaming) {
  const div = document.createElement('div');
  div.className = 'msg assistant';
  div.innerHTML = `<div class="avatar">AI</div>
    <div class="bubble-wrap">
      <div class="role-line">助手${streaming ? ' · 生成中' : ''}</div>
      <div class="blocks"></div>
    </div>`;
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
    const status = b.done ? (b.ok === false ? '<span class="tool-status err">失败</span>'
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
    S.runs.set(taskId, { blocks: [], finished: false, abort: null, node: null, container: null,
      reader: null, error: null });
  }
  return S.runs.get(taskId);
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
  }
}

async function refreshRunning() {
  try {
    const r = await API.get('/api/tasks');
    const runningIds = new Set(r.running || []);
    Array.from(S.runs.keys()).forEach(id => {
      if (!runningIds.has(id) && !S.runs.get(id).finished) {
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
  if (!text && !S.attachments.length) return;
  if (S.runs.has(S.currentTask) && !S.runs.get(S.currentTask).finished) {
    toast('当前任务正在运行，请先停止或切换任务');
    return;
  }
  if (!S.currentTask) await newTask(text.slice(0, 14) || '新任务');

  const taskId = S.currentTask;
  const atts = S.attachments.map(a => a.name);
  input.value = '';
  autoGrow(input);

  // 本地立即回显
  const localUser = { role: 'user', content: text, attachments: S.attachments.map(a => ({ name: a.name })) };
  S.messages.push(localUser);

  const run = ensureRun(taskId);
  run.blocks = [];
  run.finished = false;
  S.attachments = [];
  renderAttachments();
  renderChat();
  updateRunUI();

  const controller = new AbortController();
  run.abort = controller;

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId, content: text, attachments: atts }),
      signal: controller.signal,
    });
    if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);
    const reader = resp.body.getReader();
    run.reader = reader;
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();
      for (const part of parts) {
        const line = part.split('\n').find(l => l.startsWith('data:'));
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line.slice(5)); } catch { continue; }
        handleEvent(taskId, ev);
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      run.blocks.push({ type: 'text', text: `⚠️ 请求失败：${e.message}`, done: true });
      if (S.currentTask === taskId) renderChat();
    }
  } finally {
    run.finished = true;
    updateRunUI();
    renderTasks();
    const t = S.tasks.find(x => x.id === taskId);
    if (t) { t.message_count = (t.message_count || 0) + 1; }
    if (S.currentTask === taskId) {
      const r = await API.get(`/api/tasks/${taskId}/messages`);
      S.messages = r.messages || [];
      renderChat();
      updateTitleMeta();
    }
    loadTasks();
  }
}

function handleEvent(taskId, ev) {
  const run = ensureRun(taskId);
  const isCurrent = S.currentTask === taskId;
  switch (ev.type) {
    case 'delta': {
      const last = run.blocks[run.blocks.length - 1];
      if (last && last.type === 'text' && !last.done) last.text += ev.text;
      else run.blocks.push({ type: 'text', text: ev.text, done: false });
      if (isCurrent) scheduleLiveRender(run);
      break;
    }
    case 'reasoning': {
      const last = run.blocks[run.blocks.length - 1];
      if (last && last.type === 'reasoning' && !last.done) last.text += ev.text;
      else {
        run.blocks.push({ type: 'reasoning', text: ev.text, done: false, open: true });
        if (isCurrent) renderChat();  // 新思考块出现，整体重绘一次
        break;
      }
      if (isCurrent) scheduleLiveRender(run);
      break;
    }
    case 'tool_start': {
      run.blocks.push({ type: 'tool', name: ev.name, args: ev.args, result: '', done: false, id: ev.id });
      if (isCurrent) renderChat();
      break;
    }
    case 'tool_end': {
      for (let i = run.blocks.length - 1; i >= 0; i--) {
        const b = run.blocks[i];
        if (b.type === 'tool' && b.id === ev.id && !b.done) {
          b.result = ev.preview; b.done = true; b.ok = ev.ok; b.ms = ev.ms; break;
        }
      }
      if (isCurrent) renderChat();
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
    case 'error': {
      run.blocks.push({ type: 'text', text: `⚠️ ${ev.error}`, done: true });
      run.finished = true;
      if (isCurrent) renderChat();
      break;
    }
    case 'cancelled':
      run.blocks.push({ type: 'text', text: '（已停止生成）', done: true });
      run.finished = true;
      if (isCurrent) renderChat();
      break;
    case 'done':
    case 'close':
      run.finished = true;
      updateRunUI();
      break;
  }
}

/* 流式渲染节流：数据包只更新内存，每 ~90ms 才重绘一次「最后一块」。
   旧实现对每个数据包把碎片单独过一遍 Markdown 再塞进 DOM：
   ① 跨包的表格/加粗被拦腰截断，渲染错乱（左窄条、右侧空白）；
   ② 每次插入都触发整泡重排，长文卡顿。 */
const liveTimers = new Map();

function scheduleLiveRender(run) {
  if (liveTimers.has(run)) return;
  liveTimers.set(run, setTimeout(() => {
    liveTimers.delete(run);
    renderLiveTail(run);
  }, 90));
}

function renderLiveTail(run) {
  if (!run.node || run.finished) return;
  const b = run.blocks[run.blocks.length - 1];
  if (!b) return;
  const holder = run.node.querySelector('.blocks');
  if (!holder) return;
  const last = holder.lastElementChild;
  if (b.type === 'text' && last && last.classList.contains('bubble-md')) {
    last.innerHTML = md(b.text || '') + '<span class="cursor"></span>';
    scrollBottom();
    return;
  }
  if (b.type === 'reasoning' && last && last.classList.contains('reasoning')) {
    const body = last.querySelector('.reasoning-body');
    if (body) {
      body.textContent = b.text || '';
      body.scrollTop = body.scrollHeight;  // 思考体自动滚到底
    }
    return;
  }
  renderChat();  // 结构变化（块类型切换），整体重绘
}

async function stopRun() {
  if (!S.currentTask) return;
  await API.post('/api/chat/cancel', { task_id: S.currentTask });
  const run = S.runs.get(S.currentTask);
  if (run && run.abort) run.abort.abort();
  toast('已请求停止');
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
  $('#cfg-provider').value = c.provider || 'deepseek';
  $('#cfg-baseurl').value = (c.base_url_overrides || {})[c.provider] || '';
  $('#cfg-model').value = c.model || '';
  $('#cfg-temp').value = c.temperature ?? 0.3;
  $('#cfg-maxtokens').value = c.max_tokens || 8192;
  $('#cfg-system').value = c.system_prompt || '';
  $('#cfg-rounds').value = c.context_rounds ?? 20;
  $('#cfg-steps').value = c.agent_max_steps ?? 50;
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

  $('#btn-toggle-side').onclick = () => $('#sidebar').classList.toggle('collapsed');
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
    $('#cfg-baseurl').value = (S.config?.base_url_overrides || {})[pid] || '';
    $('#cfg-model').value = (p.models || [])[0] || '';
    updateProviderHint(); updateKeyStatus();
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
    const r = await API.post('/api/config', { patch: {
      provider: pid, model,
      base_url_overrides: Object.assign({}, S.config?.base_url_overrides || {},
        { [pid]: $('#cfg-baseurl').value.trim() }),
      temperature: parseFloat($('#cfg-temp').value) || 0.3,
      max_tokens: parseInt($('#cfg-maxtokens').value) || 8192,
    }});
    S.config = r.config; renderModelLabel();
    const t = await API.post('/api/models', { provider: pid, model });
    el.className = 'hint ' + (t.ok ? 'ok' : 'err');
    if (t.ok) el.textContent = `连接成功，服务端返回 ${t.count} 个模型，当前选择「${model}」可用。`;
    else el.textContent = t.error;
  };
  $('#btn-save-general').onclick = async () => {
    const r = await API.post('/api/config', { patch: {
      system_prompt: $('#cfg-system').value,
      context_rounds: parseInt($('#cfg-rounds').value) || 20,
      agent_max_steps: parseInt($('#cfg-steps').value) || 50,
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
    const cls = st.running ? 'on' : (st.error ? 'err' : 'off');
    const toolCount = (st.tools || []).length;
    return `<div class="list-item">
      <div class="li-head">
        <div class="li-name">${esc(s.config.name)}</div>
        <span class="pill ${cls}">${esc(st.status || '未启动')}</span>
        ${toolCount ? `<span class="pill">${toolCount} 个工具</span>` : ''}
      </div>
      <div class="li-desc">${esc(s.config.command || '')} ${esc((s.config.args || []).join(' '))}
        ${st.error ? '<br><span style="color:var(--red)">' + esc(st.error) + '</span>' : ''}</div>
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

boot();
