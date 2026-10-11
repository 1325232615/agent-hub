// QuantHub Trae 桥接扩展 v0.3.0 —— Trae 侧执行体（每窗口绑定单一 agent）
//
// 核心保证：**A 任务只会给 A 做**
//   - 每个窗口由用户【显式接管一个 agent】（命令里点名），本窗口只领取该 agent 的任务；
//   - 锁按 agent 分（trae_worker_lock_<agent>）→ 同一 agent 至多一个窗口接管；
//   - 认领用中枢原子 claim + 严格校验 HTTP 状态码 → 不重复执行；
//   - 未被任何窗口接管的 agent 的任务，保持 pending（看板可见），绝不误派。
//
// 投递说明：im.bridge.sendPromptStreaming 投递到【本窗口的当前活跃会话】，
//   因此每个窗口请保持停留在该机器人自己的会话上（不要切走），以保证上下文隔离。
const vscode = require('vscode');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');

// 全部可在 settings 里覆盖（quantHub.*），默认值不依赖任何个人绝对路径
const CFG = vscode.workspace.getConfiguration('quantHub');
const HUB = CFG.get('hubUrl') || 'http://127.0.0.1:8765';
// 中枢令牌文件：默认 <用户目录>/.agent-hub/.hub_token
const TOKEN_FILE = CFG.get('tokenFile') || path.join(os.homedir(), '.agent-hub', '.hub_token');
// 本窗口可接管的 agent 名单（示例值，按需改）
const TRAE_AGENTS = ['data_env', 'audit', 'strategy_5m', 'strategy_5m_2'];
// 运行态目录：默认 <用户目录>/.agent-hub
const OUT_DIR = CFG.get('stateDir') || path.join(os.homedir(), '.agent-hub');
const STATE_FILE = path.join(OUT_DIR, 'trae_worker.json');

const LOCK_PREFIX = 'trae_worker_lock_';   // 按 agent 分锁
const LOCK_TTL_MS = 60000;                // 锁有效期 60s
const HEARTBEAT_MS = 15000;               // 心跳 15s

const OWNER = 'w' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);

let enabled = false;
let boundAgent = null;     // 本窗口接管的 agent（null = 未接管）
let lastId = 0;
let handled = 0;
let statusItem = null;
let hbTimer = null;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const nowIso = () => new Date().toISOString();

function getToken() { try { return fs.readFileSync(TOKEN_FILE, 'utf8').trim(); } catch (e) { return ''; } }
function writeState(extra) {
  try {
    fs.mkdirSync(OUT_DIR, { recursive: true });
    fs.writeFileSync(STATE_FILE, JSON.stringify(Object.assign({
      ts: nowIso(), owner: OWNER, boundAgent: boundAgent, enabled: enabled, handled: handled, lastId: lastId,
    }, extra || {}), null, 1), 'utf8');
  } catch (e) { /* ignore */ }
}
function setStatus(t) { if (statusItem) { statusItem.text = t; } }
function refreshStatus() {
  if (!statusItem) { return; }
  statusItem.text = boundAgent
    ? ('QuantHub[' + boundAgent + ']: ' + (enabled ? 'ON' : 'OFF') + ' (' + handled + ')')
    : 'QuantHub: 未接管';
}

// ★ 串行闸：所有任务处理串成一条链，保证【同一时刻只处理一条】，
//    避免 sweepPending 与事件循环并发导致同窗口内多条任务交错注入。
let chain = Promise.resolve();
function ser(fn) {
  const p = chain.then(fn).catch((e) => { writeState({ chainErr: String((e && e.message) || e) }); });
  chain = p;
  return p;
}

// HTTP → {status, data}
function httpReq(method, url, body, token) {
  return new Promise((resolve, reject) => {
    let u;
    try { u = new URL(url); } catch (e) { return reject(e); }
    const data = body ? Buffer.from(JSON.stringify(body), 'utf8') : null;
    const headers = { Accept: 'application/json' };
    if (data) { headers['Content-Type'] = 'application/json; charset=utf-8'; headers['Content-Length'] = data.length; }
    if (token) { headers['X-Hub-Token'] = token; }
    const req = http.request({
      hostname: u.hostname, port: u.port || 80, path: u.pathname + u.search, method: method, headers: headers,
    }, (res) => {
      let buf = '';
      res.on('data', (c) => { buf += c; });
      res.on('end', () => { let p = null; try { p = buf ? JSON.parse(buf) : null; } catch (e) { p = buf; } resolve({ status: res.statusCode, data: p }); });
    });
    req.on('error', reject);
    req.setTimeout(70000, () => req.destroy(new Error('http timeout')));
    if (data) { req.write(data); }
    req.end();
  });
}

// ---------------- 按 agent 的锁 ----------------
const lockKey = (agent) => LOCK_PREFIX + agent;
async function readLock(agent) {
  const r = await httpReq('GET', HUB + '/kv/' + lockKey(agent));
  if (r.status === 200 && r.data && typeof r.data.value === 'object') { return r.data.value; }
  return null;
}
async function writeLock(agent, obj) { await httpReq('PUT', HUB + '/kv', { key: lockKey(agent), value: obj }, getToken()); }
function lockFresh(v) { return !!(v && v.owner && typeof v.ts === 'number' && (Date.now() - v.ts) < LOCK_TTL_MS); }

async function acquireLock(agent) {
  try {
    const cur = await readLock(agent);
    if (lockFresh(cur) && cur.owner !== OWNER) { return false; }
    await writeLock(agent, { owner: OWNER, ts: Date.now(), agent: agent });
    const back = await readLock(agent);
    return !!(back && back.owner === OWNER);
  } catch (e) { return false; }
}
async function renewLock(agent) {
  try {
    const cur = await readLock(agent);
    if (cur && cur.owner && cur.owner !== OWNER) { return false; }
    await writeLock(agent, { owner: OWNER, ts: Date.now(), agent: agent });
    return true;
  } catch (e) { return false; }
}
async function releaseLock(agent) {
  try {
    const cur = await readLock(agent);
    if (cur && cur.owner === OWNER) { await writeLock(agent, { owner: '', ts: 0, agent: agent }); }
  } catch (e) { /* ignore */ }
}

function identityLine(agent, role, cur) {
  const L = [];
  L.push('你是 QuantHub 调度中枢下属的专职机器人，代号「' + agent + '」。');
  if (role) { L.push('你的角色与职责：' + role + '。'); }
  if (cur) { L.push('岗位背景：' + cur); }
  L.push('你是这个岗位的专职机器人，不是通用助手，不得自称通用 AI 编码工具；被问身份时按上述角色如实确认。');
  return L.join('\n');
}

async function buildPrompt(agent, task) {
  let role = '', cur = '';
  try {
    const r = await httpReq('GET', HUB + '/agents/' + encodeURIComponent(agent));
    if (r.status === 200 && r.data) { role = r.data.role || ''; cur = r.data.current_task || ''; }
  } catch (e) { /* ignore */ }
  const tid = task.task_id;
  const head = identityLine(agent, role, cur);
  if (task.payload && typeof task.payload === 'object' && task.payload.prompt) {
    return head + '\n\n' + String(task.payload.prompt) + '\n\n【回执】完成后请把结论 POST 回中枢该任务（task_id=' + tid + '）。';
  }
  const L = [head];
  L.push('【本次任务】' + (task.title || '(无标题)'));
  if (task.payload && Object.keys(task.payload).length) { L.push('载荷：' + JSON.stringify(task.payload, null, 1)); }
  L.push('【执行要求】按任务描述实际执行（可读文件 / 分析 / 产出）。严格边界：除任务明确授权外，不得启动或触碰任何外部交易终端进程；不得修改项目文件，除非任务明确要求。');
  L.push('【完成后必须回执到中枢】请执行以下 PowerShell（把 <你的结论> 换成实际结论文本，产物路径写入 artifacts）：\n'
    + '$tok = (Get-Content \'' + TOKEN_FILE + '\' -Raw).Trim()\n'
    + '$b = @{ status=\'done\'; result=@{ by=\'trae-agent\'; agent=\'' + agent + '\'; text=\'<你的结论>\'; artifacts=@() } } | ConvertTo-Json -Compress\n'
    + 'Invoke-RestMethod -Method Post -Uri \'http://127.0.0.1:8765/tasks/' + tid + '/result\' -Headers @{ \'X-Hub-Token\' = $tok } -ContentType \'application/json; charset=utf-8\' -Body ([Text.Encoding]::UTF8.GetBytes($b))');
  return L.join('\n\n');
}

async function handleTask(tid, agent) {
  if (!tid || !enabled || agent !== boundAgent) { return; }   // ★ 双保险：只做本窗口 agent
  const token = getToken();
  let t = null;
  try {
    const r = await httpReq('GET', HUB + '/tasks?agent=' + encodeURIComponent(agent));
    t = (r.data || []).find((x) => x.task_id === tid);
  } catch (e) { return; }
  if (!t || t.status !== 'pending') { return; }

  // 原子认领 + 严格状态码：非 200（如 409 已被领走）一律放弃
  try {
    const c = await httpReq('POST', HUB + '/tasks/' + tid + '/claim', null, token);
    if (c.status !== 200) { writeState({ skipped: tid, claimStatus: c.status }); return; }
  } catch (e) { return; }
  try { await httpReq('POST', HUB + '/tasks/' + tid + '/status', { status: 'running', note: 'trae-bridge:' + agent }, token); } catch (e) { /* ignore */ }

  const prompt = await buildPrompt(agent, t);
  try {
    await vscode.commands.executeCommand('im.bridge.sendPromptStreaming', prompt);
    handled += 1; refreshStatus(); writeState({ lastTask: tid, lastAgent: agent });
  } catch (e) { writeState({ lastError: String((e && e.message) || e) }); return; }

  const t0 = Date.now();
  while (enabled && (Date.now() - t0) < 30 * 60 * 1000) {
    await sleep(5000);
    try {
      const r2 = await httpReq('GET', HUB + '/tasks?agent=' + encodeURIComponent(agent));
      const cur = (r2.data || []).find((x) => x.task_id === tid);
      if (!cur || cur.status === 'done' || cur.status === 'failed') { break; }
    } catch (e) { /* ignore */ }
  }
}

async function sweepPending() {
  const agent = boundAgent;
  if (!agent) { return; }
  try {
    const r = await httpReq('GET', HUB + '/tasks?agent=' + encodeURIComponent(agent));
    const pend = (r.data || []).filter((x) => x.status === 'pending');
    for (const t of pend) { if (!enabled) { return; } await ser(() => handleTask(t.task_id, agent)); }
  } catch (e) { /* ignore */ }
}

async function loop() {
  while (enabled) {
    const agent = boundAgent;
    if (!agent) { return; }
    try {
      const ok = await renewLock(agent);
      if (!ok) {
        enabled = false;
        writeState({ lostLockAt: nowIso() });
        refreshStatus();
        vscode.window.showWarningMessage('本窗口对 ' + agent + ' 的执行体身份已被其它窗口接管，已自动停止。');
        return;
      }
      const r = await httpReq('GET', HUB + '/events/wait?since=' + lastId + '&timeout=25');
      if (r.status === 200 && r.data && typeof r.data.last_id === 'number' && r.data.last_id > 0) { lastId = r.data.last_id; writeState({}); }
      const evs = (r.data && r.data.events) || [];
      for (const ev of evs) {
        if (!enabled) { break; }
        if (ev.action !== 'create_task') { continue; }
        if (ev.target_agent !== boundAgent) { continue; }   // ★ 只认本窗口 agent 的事件
        await ser(() => handleTask(ev.ref_task_id, ev.target_agent));
      }
    } catch (e) { writeState({ lastError: String((e && e.message) || e) }); await sleep(3000); }
  }
}

/** 本窗口接管指定 agent */
async function takeOver(agent) {
  if (TRAE_AGENTS.indexOf(agent) < 0) { vscode.window.showErrorMessage('未知 agent：' + agent); return; }
  if (enabled && boundAgent === agent) { vscode.window.showInformationMessage('本窗口已接管 ' + agent); return; }
  if (enabled && boundAgent !== agent) { await stopWorker(); }
  const got = await acquireLock(agent);
  if (!got) {
    vscode.window.showWarningMessage('已另有窗口接管 ' + agent + '（同一 agent 仅允许一个窗口，防重复）。本窗口未启动。');
    writeState({ refusedAt: nowIso(), refusedAgent: agent });
    return;
  }
  boundAgent = agent;
  enabled = true;
  lastId = 0;
  refreshStatus();
  writeState({ enabledAt: nowIso(), boundAgent: agent });
  vscode.window.showInformationMessage('本窗口已接管「' + agent + '」（请保持本窗口停留在该机器人的会话上）');
  hbTimer = setInterval(async () => {
    if (!enabled || !boundAgent) { return; }
    const ok = await renewLock(boundAgent);
    if (!ok) { enabled = false; refreshStatus(); }
  }, HEARTBEAT_MS);
  sweepPending();
  loop();
}

async function stopWorker() {
  const agent = boundAgent;
  enabled = false;
  if (hbTimer) { clearInterval(hbTimer); hbTimer = null; }
  if (agent) { await releaseLock(agent); }
  boundAgent = null;
  lastId = 0;
  refreshStatus();
  writeState({ disabledAt: nowIso() });
  vscode.window.showInformationMessage('已停止本窗口的执行体（锁已释放）');
}

async function showStatus() {
  const parts = [];
  for (const a of TRAE_AGENTS) {
    let s = '空闲';
    try { const v = await readLock(a); if (lockFresh(v)) { s = (v.owner === OWNER ? '本窗口' : v.owner); } } catch (e) { s = '?'; }
    parts.push(a + '=' + s);
  }
  vscode.window.showInformationMessage('本窗口=' + (boundAgent ? boundAgent : '未接管')
    + ' | owner=' + OWNER + ' | 已处理=' + handled + '\n锁占用：' + parts.join('  '));
}

function activate(context) {
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  refreshStatus();
  statusItem.show();
  context.subscriptions.push(statusItem);

  TRAE_AGENTS.forEach((a) => {
    context.subscriptions.push(vscode.commands.registerCommand('quantHub.takeover.' + a, () => takeOver(a)));
  });
  context.subscriptions.push(vscode.commands.registerCommand('quantHub.stopWorker', stopWorker));
  context.subscriptions.push(vscode.commands.registerCommand('quantHub.workerStatus', showStatus));
  context.subscriptions.push(vscode.commands.registerCommand('quantHub.drive', async () => {
    const q = await vscode.window.showInputBox({ prompt: '要发给当前 Trae 会话的问题（会真实发送）' });
    if (!q) { return; }
    try { await vscode.commands.executeCommand('im.bridge.sendPromptStreaming', q); vscode.window.showInformationMessage('已发送'); }
    catch (e) { vscode.window.showErrorMessage('发送失败：' + ((e && e.message) || e)); }
  }));

  writeState({ activatedAt: nowIso(), note: 'v0.3.1' });
}

function deactivate() { enabled = false; if (hbTimer) { clearInterval(hbTimer); } }

module.exports = { activate: activate, deactivate: deactivate };
