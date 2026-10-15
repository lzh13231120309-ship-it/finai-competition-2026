const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const flow = source.slice(source.indexOf('function ensureRun('), source.indexOf('/* =================================================================== 附件 */'));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  const elements = new Map();
  const state = { currentTask: 'a', tasks: [{ id: 'a' }, { id: 'b' }], messages: [], attachments: [], runs: new Map(), config: {} };
  const h = { state, posts: [], requests: [], renders: [], notices: [], elements, timers: [] };
  const context = vm.createContext({
    S: state, AbortController, TextDecoder, Date, Math, crypto: require('node:crypto').webcrypto,
    setTimeout: (callback, delay) => {
      if (!h.fakeTimers) return setTimeout(callback, delay);
      const timer = { callback, delay, cancelled: false };
      h.timers.push(timer);
      return timer;
    },
    clearTimeout: timer => { if (timer?.callback) timer.cancelled = true; else clearTimeout(timer); },
    $: selector => {
      if (!elements.has(selector)) elements.set(selector, { value: '', focus() {}, classList: { toggle() {} } });
      return elements.get(selector);
    },
    API: {
      post: (url, body) => { h.posts.push({ url, body }); return h.cancel.promise; },
      get: async () => ({ messages: [], tasks: state.tasks }),
    },
    fetch: async (url, options) => {
      const read = deferred();
      const reader = { read: () => read.promise, cancel: () => { h.cancelledReader = true; return Promise.resolve(); } };
      h.requests.push({ url, options, read, reader });
      return { ok: true, body: { getReader: () => reader } };
    },
    autoGrow() {}, renderAttachments() {}, updateTitleMeta() {}, renderTasks() {}, loadTasks: async () => {},
    renderChat: () => h.renders.push(state.runs.get(state.currentTask)?.blocks.map(b => b.text).join('')),
    toast: message => h.notices.push(message), md: text => text, scrollBottom() {},
  });
  h.cancel = deferred();
  vm.runInContext(flow, context);
  h.context = context;
  h.send = text => { context.$('#input').value = text; return context.send(); };
  return h;
}

test('stop immediately aborts and releases input while cancel response is pending', async () => {
  const h = harness();
  const completion = h.send('first');
  await tick();
  const run = h.state.runs.get('a');
  h.context.handleEvent('a', { type: 'delta', text: 'partial' }, run);
  const stopped = h.context.stopRun();
  assert.equal(run.abort.signal.aborted, true);
  assert.equal(run.finished, true);
  assert.equal(h.cancelledReader, true);
  assert.ok(run.blocks.some(b => b.text === 'partial'));
  assert.equal(h.posts[0].body.run_id, JSON.parse(h.requests[0].options.body).run_id);
  h.cancel.resolve({ ok: true });
  h.requests[0].read.resolve({ done: true });
  await Promise.all([completion, stopped]);
});

test('a delayed stop acknowledgement never aborts a newly selected task', async () => {
  const h = harness();
  const first = h.context.ensureRun('a');
  first.abort = new AbortController();
  const second = h.context.ensureRun('b');
  second.abort = new AbortController();
  const stopped = h.context.stopRun();
  h.state.currentTask = 'b';
  h.cancel.resolve({ ok: true });
  await stopped;
  assert.equal(first.abort.signal.aborted, true);
  assert.equal(second.abort.signal.aborted, false);
});

test('resume waits for cancel acknowledgement, and stale stream cannot finish the next run', async () => {
  const h = harness();
  const oldCompletion = h.send('first');
  await tick();
  const oldRun = h.state.runs.get('a');
  const stopped = h.context.stopRun();
  const newCompletion = h.send('second');
  await tick();
  assert.equal(h.requests.length, 1, 'must not race the backend cancellation');
  const newRun = h.state.runs.get('a');
  assert.notEqual(newRun, oldRun);
  h.cancel.resolve({ ok: true });
  await stopped;
  await tick();
  assert.equal(h.requests.length, 2);
  h.context.handleEvent('a', { type: 'delta', text: 'stale' }, oldRun);
  h.requests[0].read.resolve({ done: true });
  await oldCompletion;
  assert.equal(newRun.finished, false);
  assert.equal(newRun.blocks.some(b => b.text === 'stale'), false);
  assert.notEqual(JSON.parse(h.requests[0].options.body).run_id, JSON.parse(h.requests[1].options.body).run_id);
  h.requests[1].read.resolve({ done: true });
  await newCompletion;
});

test('paused events retain partial content and immediately release the composer', () => {
  const h = harness();
  const run = h.context.ensureRun('a');
  h.context.handleEvent('a', { type: 'delta', text: 'partial' }, run);
  h.context.handleEvent('a', { type: 'paused', text: '继续输入即可恢复', reason: 'idle_timeout' }, run);
  assert.equal(run.finished, true);
  assert.ok(run.blocks.some(b => b.text === 'partial'));
  assert.ok(run.blocks.some(b => b.text === '继续输入即可恢复'));
});

test('stalled output automatically pauses while a server read remains pending', async () => {
  const h = harness();
  h.fakeTimers = true;
  const completion = h.send('first');
  await tick();
  const run = h.state.runs.get('a');
  assert.equal(run.idleTimer.delay, 181000);
  run.idleTimer.callback();
  assert.equal(run.finished, true);
  assert.equal(run.abort.signal.aborted, true);
  assert.ok(run.blocks.some(b => b.text.includes('长时间没有新输出')));
  h.cancel.resolve({ ok: true });
  h.requests[0].read.resolve({ done: true });
  await completion;
});

test('an old history refresh cannot replace messages from a resumed run', async () => {
  const h = harness();
  const history = deferred();
  h.context.API.get = () => history.promise;
  const oldCompletion = h.send('first');
  await tick();
  h.requests[0].read.resolve({ done: true });
  await tick();
  const newCompletion = h.send('second');
  await tick();
  history.resolve({ messages: [{ role: 'assistant', content: 'old history' }] });
  await oldCompletion;
  assert.ok(h.state.messages.some(m => m.role === 'user' && m.content === 'second'));
  assert.equal(h.state.runs.get('a').finished, false);
  h.requests[1].read.resolve({ done: true });
  await newCompletion;
});

test('finished local blocks stay visible until authoritative history replaces them', () => {
  const h = harness();
  const rendered = [];
  const chat = { innerHTML: '', appendChild: node => rendered.push(node) };
  const originalQuery = h.context.$;
  h.context.$ = selector => selector === '#chat' ? chat : originalQuery(selector);
  h.context.showWelcome = () => {};
  h.context.bindLiveUpdate = () => {};
  h.context.assistantNode = (blocks, streaming) => ({ blocks, streaming, querySelector() {} });
  const renderSource = source.slice(source.indexOf('function renderChat('), source.indexOf('function safeJson('));
  vm.runInContext(renderSource, h.context);
  const run = h.context.ensureRun('a');
  run.blocks = [{ type: 'text', text: 'partial', done: false }];
  run.finished = true;
  h.context.renderChat();
  assert.equal(rendered.length, 1);
  assert.equal(rendered[0].blocks[0].text, 'partial');
  assert.equal(rendered[0].streaming, false);
  run.retain = false;
  rendered.length = 0;
  h.context.renderChat();
  assert.equal(rendered.length, 0);
});

test('resume waits until server cleanup finishes when cancel first reports stopping', async () => {
  const h = harness();
  h.fakeTimers = true;
  let cancelled = 0;
  h.context.API.post = async (url, body) => {
    h.posts.push({ url, body });
    return { ok: true, stopping: ++cancelled === 1 };
  };
  const oldCompletion = h.send('first');
  await tick();
  const stopped = h.context.stopRun();
  const newCompletion = h.send('second');
  await tick();
  assert.equal(h.requests.length, 1);
  const retry = h.timers.find(t => t.delay === 100 && !t.cancelled);
  assert.ok(retry);
  retry.callback();
  await stopped;
  await tick();
  assert.equal(h.posts.length, 2);
  assert.equal(h.requests.length, 2);
  h.requests[0].read.resolve({ done: true });
  h.requests[1].read.resolve({ done: true });
  await Promise.all([oldCompletion, newCompletion]);
});

test('SSE comment heartbeats keep a long tool alive without assistant text', async () => {
  const h = harness();
  h.fakeTimers = true;
  const completion = h.send('first');
  await tick();
  const run = h.state.runs.get('a');
  const previousTimer = run.idleTimer;
  const nextRead = deferred();
  h.requests[0].reader.read = () => nextRead.promise;
  h.requests[0].read.resolve({ done: false, value: new TextEncoder().encode(': heartbeat\n\n') });
  await tick();
  assert.equal(previousTimer.cancelled, true);
  assert.notEqual(run.idleTimer, previousTimer);
  assert.equal(run.finished, false);
  nextRead.resolve({ done: true });
  await completion;
});

test('transport failure preserves partial output and the local user message', async () => {
  const h = harness();
  const completion = h.send('first');
  await tick();
  const run = h.state.runs.get('a');
  h.context.handleEvent('a', { type: 'delta', text: 'partial' }, run);
  h.requests[0].read.reject(new Error('connection lost'));
  await completion;
  assert.equal(run.retain, true);
  assert.ok(run.blocks.some(b => b.text === 'partial'));
  assert.ok(h.state.messages.some(m => m.role === 'user' && m.content === 'first'));
});

function liveDom(h, run, count = 0) {
  const holder = {
    children: Array.from({ length: count }, (_, i) => ({ saved: i })),
    appendChild(node) { this.children.push(node); },
    replaceChild(node, old) { this.children[this.children.indexOf(old)] = node; },
    replaceChildren() { this.children = []; },
    querySelectorAll() { return []; },
  };
  const label = { textContent: '助手 · 生成中' };
  run.node = { querySelector: selector => selector === '.blocks' ? holder : label };
  run.renderedBlocks = count;
  h.context.blockNode = (block, streaming) => ({ text: block.text, result: block.result, done: block.done, streaming,
    insertAdjacentHTML() {} });
  return { holder, label };
}

test('tool updates reuse live nodes without rebuilding completed history', () => {
  const h = harness();
  h.fakeTimers = true;
  const run = h.context.ensureRun('a');
  run.blocks = Array.from({ length: 1000 }, (_, i) => ({ type: 'tool', id: String(i), done: true }));
  const { holder } = liveDom(h, run, 1000);
  const first = holder.children[0];
  h.context.handleEvent('a', { type: 'tool_start', name: 'calculator', id: 'next' }, run);
  h.timers.find(t => t.delay === 90 && !t.cancelled).callback();
  assert.equal(h.renders.length, 0);
  assert.equal(holder.children.length, 1001);
  h.context.handleEvent('a', { type: 'tool_end', id: 'next', preview: '42', ok: true }, run);
  h.timers.filter(t => t.delay === 90 && !t.cancelled).at(-1).callback();
  assert.equal(h.renders.length, 0);
  assert.equal(holder.children[0], first);
  assert.equal(holder.children[1000].result, '42');
  h.context.handleEvent('a', { type: 'done' }, run);
});

test('stop releases the composer before updating a long live conversation', async () => {
  const h = harness();
  const run = h.context.ensureRun('a');
  run.abort = new AbortController();
  run.blocks = [{ type: 'text', text: 'partial', done: false }];
  const { holder } = liveDom(h, run, 1);
  let sendHidden = true;
  h.context.$('#btn-send').classList.toggle = (name, hidden) => { sendHidden = hidden; };
  const makeBlock = h.context.blockNode;
  h.context.blockNode = (...args) => {
    assert.equal(run.abort.signal.aborted, true);
    assert.equal(sendHidden, false, 'release input before rendering output');
    return makeBlock(...args);
  };
  h.context.handleEvent('a', { type: 'delta', text: '-new' }, run);
  h.context.renderChat = () => {
    assert.fail('stop should update the existing live node instead of rebuilding history');
  };
  const stopped = h.context.stopRun();
  assert.equal(run.abort.signal.aborted, true);
  assert.equal(run.finished, true);
  assert.equal(holder.children[0].text, 'partial-new');
  h.cancel.resolve({ ok: true });
  await stopped;
});

test('a new prompt retries a failed cancellation with the original run id', async () => {
  const h = harness();
  const oldCompletion = h.send('first');
  await tick();
  const original = h.state.runs.get('a');
  const stopped = h.context.stopRun();
  h.cancel.reject(new Error('offline'));
  await assert.rejects(stopped, /offline/);
  h.cancel = deferred();
  const newCompletion = h.send('second');
  await tick();
  assert.equal(h.posts.length, 2);
  assert.equal(h.posts[1].body.run_id, original.id);
  h.cancel.resolve({ ok: true });
  await tick();
  assert.equal(h.requests.length, 2);
  assert.equal(JSON.parse(h.requests[1].options.body).content, 'second');
  h.requests[0].read.resolve({ done: true });
  h.requests[1].read.resolve({ done: true });
  await Promise.all([oldCompletion, newCompletion]);
});

test('repeated cancellation errors restore the next prompt and attachments for retry', async () => {
  const h = harness();
  const oldCompletion = h.send('first');
  await tick();
  const original = h.state.runs.get('a');
  h.context.handleEvent('a', { type: 'delta', text: 'partial' }, original);
  const stopped = h.context.stopRun();
  h.cancel.reject(new Error('offline'));
  await assert.rejects(stopped, /offline/);
  h.state.attachments = [{ name: 'report.pdf' }];
  await h.send('continue this task');
  assert.equal(h.context.$('#input').value, 'continue this task');
  assert.equal(h.state.attachments[0].name, 'report.pdf');
  assert.equal(h.state.runs.get('a').finished, true);
  assert.ok(h.state.messages.some(m => m.blocks?.some(b => b.text === 'partial')));
  h.cancel = deferred();
  const retry = h.context.send();
  await tick();
  assert.equal(h.posts.at(-1).body.run_id, original.id);
  h.cancel.resolve({ ok: true });
  await tick();
  assert.equal(h.requests.length, 2);
  h.requests[0].read.resolve({ done: true });
  h.requests[1].read.resolve({ done: true });
  await Promise.all([oldCompletion, retry]);
});

test('retry failure preserves newer input typed while cancellation was pending', async () => {
  const h = harness();
  const oldCompletion = h.send('first');
  await tick();
  const stopped = h.context.stopRun();
  h.cancel.reject(new Error('offline'));
  await assert.rejects(stopped, /offline/);
  h.cancel = deferred();
  const resumed = h.send('second');
  await tick();
  h.context.$('#input').value = 'newer draft';
  h.cancel.reject(new Error('still offline'));
  await resumed;
  assert.equal(h.context.$('#input').value, 'newer draft');
  assert.equal(h.state.runs.get('a').finished, true);
  assert.equal(h.requests.length, 1);
  h.requests[0].read.resolve({ done: true });
  await oldCompletion;
});

test('stopping an already locally stopped run retries a failed confirmation', async () => {
  const h = harness();
  const run = h.context.ensureRun('a');
  run.abort = new AbortController();
  const stopped = h.context.stopRun();
  h.cancel.reject(new Error('offline'));
  await assert.rejects(stopped, /offline/);
  h.cancel = deferred();
  const retry = h.context.stopRun();
  await tick();
  assert.equal(h.posts.length, 2);
  assert.equal(h.posts[1].body.run_id, run.id);
  h.cancel.resolve({ ok: true });
  await retry;
  assert.equal(run.cancelAcknowledged, true);
});
