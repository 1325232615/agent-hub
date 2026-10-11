# -*- coding: utf-8 -*-
"""QuantHub 机器人执行看板（网页预览）。

用途
----
给用户一个可实时查看「各机器人当前在跑什么 / 任务状态 / 事件流」的网页，
并可**点进任一任务，查看它完整的思考与工具调用轨迹**。

架构
----
    浏览器 ──GET /              ──▶ 本服务（返回看板 HTML，单文件自包含）
    浏览器 ──GET /api/<path>    ──▶ 本服务（反向代理）──▶ 中枢 FastAPI 127.0.0.1:8765
    浏览器 ──GET /trace?...     ──▶ 本服务（直接读 daemon_traces/<agent>/<task>.jsonl）
    （同源，规避 CORS；不修改中枢任何代码）

启动
----
    .venv\\Scripts\\python.exe dashboard_server.py [--port 8770] [--hub http://127.0.0.1:8765]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HUB_BASE = os.environ.get("HUB_API", "http://127.0.0.1:8765").rstrip("/")
BASE_DIR = Path(__file__).resolve().parent
TRACE_ROOT = BASE_DIR / "daemon_traces"      # 与 hub_daemon.py 的 TRACE_DIR 对应
_SAFE = re.compile(r"^[A-Za-z0-9_.\-]+$")
TOKEN_FILE = BASE_DIR / ".hub_token"
# 可选大模型（取自 `codebuddy --help` 支持列表；前端下拉用，允许自定义追加）
MODELS = [
    "custom-local:deepseek-flash",
    "custom-local:deepseek-v4-pro",
    "custom-local:claude-sonnet-5",
    "custom-local:gpt-6-luna",
    "custom-local:gpt-5.6-luna",
    "custom-local:gemini-3.8-flash",
    "custom-local:glm-5.3-flash",
    "custom-local:qwen3.8-flash",
    "custom-local:grok-4.6",
    "custom-local:MiniMax-M3",
    "custom-local:auto",
    "auto",
    "glm-5.1", "glm-5.0-turbo", "glm-5.0", "glm-5v-turbo", "glm-4.7",
    "kimi-k2.5", "minimax-m2.7", "deepseek-v3-2-volc",
]

# ============================ 看板页面（自包含） ============================
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QuantHub · 机器人执行看板</title>
<style>
  :root{
    --bg:#0f1216; --panel:#171b21; --panel2:#1e242c; --line:#2a323c;
    --tx:#e6e9ee; --mut:#8b97a5; --brand:#2fbf7f;
    --warn:#e0a33c; --err:#e05c4b; --run:#3d8bfd; --think:#b98cff;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--tx);
       font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  header{position:sticky;top:0;z-index:5;background:rgba(15,18,22,.94);
         backdrop-filter:blur(6px);border-bottom:1px solid var(--line);
         padding:12px 18px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  .logo{font-weight:700;font-size:16px}
  .logo b{color:var(--brand)}
  .stats{display:flex;gap:14px;flex-wrap:wrap;margin-left:auto;color:var(--mut);font-size:12.5px}
  .stats b{color:var(--tx);font-variant-numeric:tabular-nums}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:middle}
  main{padding:16px 18px 40px;max-width:1500px;margin:0 auto}
  h2{font-size:13px;color:var(--mut);font-weight:600;margin:20px 0 10px;letter-spacing:.6px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .card .top{display:flex;align-items:center;gap:8px;margin-bottom:6px}
  .card .nm{font-weight:700;font-size:14.5px}
  .card .role{color:var(--mut);font-size:12px;margin-bottom:8px}
  .card .task{background:var(--panel2);border-radius:7px;padding:8px 10px;font-size:12.5px;min-height:38px;word-break:break-word}
  .badge{font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid var(--line);color:var(--mut)}
  .b-run{color:var(--run);border-color:#2b4c85;background:#16233a}
  .b-off{color:var(--mut)}
  table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);
        border-radius:10px;overflow:hidden;font-size:12.5px}
  th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--mut);font-weight:600;background:var(--panel2)}
  tr:last-child td{border-bottom:none}
  tbody tr.row{cursor:pointer}
  tbody tr.row:hover{background:#1b2129}
  td.tid{font-family:ui-monospace,Consolas,monospace;color:#9fd4ff;white-space:nowrap}
  .st{font-weight:600;padding:1px 7px;border-radius:5px;font-size:11.5px;white-space:nowrap}
  .s-done{color:var(--brand);background:#12241b}
  .s-failed{color:var(--err);background:#2a1512}
  .s-running{color:var(--run);background:#14203a}
  .s-pending,.s-claimed{color:var(--warn);background:#251f10}
  .s-other{color:var(--mut);background:var(--panel2)}
  .ev{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--mut)}
  .ev .a{color:var(--tx)}
  .empty{color:var(--mut);padding:14px;text-align:center}
  .foot{color:var(--mut);font-size:12px;margin-top:18px;text-align:center}
  .hint{color:var(--mut);font-size:12px;margin:-4px 0 8px}
  .mrow{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px;color:var(--mut)}
  .mrow .mlab{white-space:nowrap}
  .msel{flex:1;min-width:0;background:var(--panel2);color:var(--tx);border:1px solid var(--line);
        border-radius:6px;padding:4px 8px;font-size:12px}
  .card.clickable{cursor:pointer}
  .card.clickable:hover{border-color:#3a4653}
  /* 对话面板 */
  .chatdrawer .cb{flex:1;overflow:auto;padding:14px 16px;display:flex;flex-direction:column;gap:2px}
  .bub{max-width:84%;padding:8px 12px;border-radius:12px;margin:3px 0;white-space:pre-wrap;
       word-break:break-word;font-size:13px}
  .bub.u{align-self:flex-end;background:#1c4a7a;color:#eaf3ff;border-bottom-right-radius:4px}
  .bub.a{align-self:flex-start;background:#1e242c;border:1px solid var(--line);border-bottom-left-radius:4px}
  .bub.pending{color:var(--mut)}
  .bub.err{color:var(--err)}
  .cf{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--line);background:var(--panel)}
  .cf textarea{flex:1;resize:none;height:46px;background:var(--panel2);color:var(--tx);
               border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:13px/1.4 inherit}
  .cf button{background:var(--brand);color:#04140c;border:none;border-radius:8px;padding:0 18px;
             font-weight:700;cursor:pointer;font-size:13px}
  .cf button:disabled{opacity:.5;cursor:default}
  .chatname{color:#9fd4ff;font-family:ui-monospace,Consolas,monospace}
  .thwrap{margin-top:14px;border-top:1px dashed var(--line);padding-top:10px}
  .thhd{color:var(--mut);font-size:12px;margin-bottom:8px}
  .thitem{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:8px;cursor:pointer;background:var(--panel2)}
  .thitem:hover{border-color:var(--brand)}
  .thhead{display:flex;gap:8px;align-items:center;font-size:12px;font-family:ui-monospace,Consolas,monospace;flex-wrap:wrap}
  .thst{padding:1px 6px;border-radius:6px;background:var(--panel);border:1px solid var(--line);color:var(--mut)}
  .thst.ok{color:var(--brand)} .thst.err{color:var(--err)} .thst.blk{color:var(--warn)} .thst.run{color:var(--run)}
  .thid{color:var(--mut)} .thtime{margin-left:auto;color:var(--mut)}
  .thlive{color:var(--brand);border:1px solid var(--line);border-radius:6px;padding:1px 7px;cursor:pointer}
  .thlive:hover{background:var(--panel)}
  .thttl{margin-top:6px;font-size:13px;color:var(--tx);line-height:1.5}
  .thres{margin-top:8px;white-space:pre-wrap;font-size:12px;color:var(--tx);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px;max-height:320px;overflow:auto}

  /* 详情抽屉 */
  .drawer{position:fixed;top:0;right:0;height:100vh;width:min(760px,94vw);
          background:#12161b;border-left:1px solid var(--line);z-index:20;
          transform:translateX(102%);transition:transform .22s ease;display:flex;flex-direction:column}
  .drawer.open{transform:translateX(0)}
  .dh{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line);
      background:var(--panel)}
  .dh .t{font-weight:700;font-family:ui-monospace,Consolas,monospace;color:#9fd4ff;font-size:13px}
  .dh .x{margin-left:auto;cursor:pointer;border:1px solid var(--line);background:var(--panel2);
         color:var(--tx);border-radius:6px;padding:2px 10px;font-size:14px}
  .db{padding:14px 16px 40px;overflow:auto}
  .sec{margin:0 0 8px;color:var(--mut);font-size:12px;font-weight:600;letter-spacing:.5px}
  .meta{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:12.5px;margin-bottom:12px}
  .meta .k{color:var(--mut)}
  pre{white-space:pre-wrap;word-break:break-word;margin:4px 0;padding:8px 10px;background:#0d1014;
      border:1px solid var(--line);border-radius:7px;font-family:ui-monospace,Consolas,monospace;
      font-size:12px;max-height:420px;overflow:auto}
  .mtext{background:#0d1014;border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:6px 0;white-space:pre-wrap}
  .think{border-left:3px solid var(--think);background:#161029;border-radius:0 8px 8px 0;
         padding:8px 10px;margin:6px 0;color:#d8c9ff;white-space:pre-wrap}
  .tool{border-left:3px solid var(--brand);background:#0e1a14;border-radius:0 8px 8px 0;padding:8px 10px;margin:6px 0}
  .tool b{color:var(--brand)}
  .toolres{border-left:3px solid #3a4653;background:#101418;border-radius:0 8px 8px 0;padding:6px 10px;margin:6px 0;color:var(--mut)}
  .toolres.err{border-left-color:var(--err)}
  .sys{color:var(--mut);font-family:ui-monospace,monospace;font-size:11.5px;padding:2px 0}
  .resline{color:var(--brand);font-size:12.5px;padding:4px 0}
  .rerr{color:var(--err)}
  .spin{color:var(--mut);padding:20px;text-align:center}
</style>
</head>
<body>
<header>
  <div class="logo">QuantHub · <b>机器人执行看板</b></div>
  <div class="stats" id="stats"></div>
</header>
<main>
  <h2>机器人（Agent）</h2>
  <div class="grid" id="agents"></div>

  <h2>任务队列（最近 40 条）</h2>
  <div class="hint">点击任意一行 → 查看该任务的完整思考与工具调用轨迹</div>
  <table id="tasks"><thead><tr>
    <th style="width:230px">任务 ID</th><th style="width:130px">机器人</th>
    <th style="width:90px">状态</th><th>标题</th><th style="width:150px">更新时间</th>
  </tr></thead><tbody></tbody></table>

  <h2>实时事件流（最近 30 条）</h2>
  <table id="events"><thead><tr>
    <th style="width:150px">时间</th><th style="width:150px">动作</th>
    <th style="width:130px">目标</th><th>详情</th>
  </tr></thead><tbody></tbody></table>

  <div class="foot" id="foot">加载中…</div>
</main>

<div class="drawer" id="drawer">
  <div class="dh"><span class="t" id="dt">任务详情</span><button class="x" id="dclose">关闭 ✕</button></div>
  <div class="db" id="dbody"></div>
</div>

<div class="drawer chatdrawer" id="chat">
  <div class="dh"><span class="t chatname" id="ct">对话</span><button class="x" id="cclose">关闭 ✕</button></div>
  <div class="cb" id="cbody"></div>
  <div class="cf">
    <textarea id="cinput" placeholder="输入消息，Enter 发送 / Shift+Enter 换行"></textarea>
    <button id="csend">发送</button>
  </div>
</div>

<script>
const $ = (s)=>document.querySelector(s);
const esc = (s)=> (s==null?'':String(s)).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const STCLS = {done:'s-done',failed:'s-failed',running:'s-running',pending:'s-pending',claimed:'s-claimed'};
const stBadge = (s)=>'<span class="st '+(STCLS[s]||'s-other')+'">'+esc(s||'?')+'</span>';
const dt = (ts)=> ts ? String(ts).replace('T',' ').slice(0,19) : '';
const MODELS = __MODELS__;
let TASKMAP = {};
let MODELMAP = {};
function modelSelect(agent){
  const cur = MODELMAP[agent] || '';
  let list = MODELS.slice();
  let opts = '<option value="">（默认 · custom-local:deepseek-flash）</option>';
  if(cur && list.indexOf(cur)<0) list.unshift(cur);
  opts += list.map(m=>'<option value="'+m+'"'+(m===cur?' selected':'')+'>'+m+'</option>').join('');
  return '<select class="msel" data-agent="'+esc(agent)+'">'+opts+'</select>';
}

function renderAgents(agents){
  const box=$('#agents');
  if(!agents.length){ box.innerHTML='<div class="empty">无</div>'; return; }
  box.innerHTML = agents.map(a=>{
    const cls = a.status==='in_progress' ? 'b-run' : 'b-off';
    return '<div class="card clickable" data-agent="'+esc(a.name)+'"><div class="top"><span class="nm">'+esc(a.name)+'</span>'
      +'<span class="badge '+cls+'" style="margin-left:auto">'+esc(a.status||'?')+'</span></div>'
      +'<div class="role">'+esc(a.role||'')+'</div>'
      +'<div class="task">'+(a.current_task?esc(a.current_task):'<span style="color:#6f7b89">（空闲）</span>')+'</div>'
      +'<div class="mrow"><span class="mlab">🧠 模型</span>'+modelSelect(a.name)+'</div></div>';
  }).join('');
}
function renderTasks(tasks){
  TASKMAP={}; tasks.forEach(t=>TASKMAP[t.task_id]=t);
  const tb=$('#tasks tbody');
  const rows=tasks.slice().sort((x,y)=>(y.updated_at||'').localeCompare(x.updated_at||'')).slice(0,40);
  if(!rows.length){ tb.innerHTML='<tr><td colspan="5" class="empty">无任务</td></tr>'; return; }
  tb.innerHTML = rows.map(t=>'<tr class="row" data-tid="'+esc(t.task_id)+'">'
    +'<td class="tid">'+esc(t.task_id)+'</td><td>'+esc(t.agent_name)+'</td>'
    +'<td>'+stBadge(t.status)+'</td><td>'+esc((t.title||'').slice(0,120))+'</td>'
    +'<td>'+esc(dt(t.updated_at))+'</td></tr>').join('');
}
function renderEvents(evs){
  const tb=$('#events tbody');
  if(!evs.length){ tb.innerHTML='<tr><td colspan="4" class="empty">无事件</td></tr>'; return; }
  tb.innerHTML=evs.map(e=>{let d=e.detail; if(d&&typeof d==='object') d=JSON.stringify(d);
    return '<tr class="ev"><td>'+esc(e.ts)+'</td><td class="a">'+esc(e.action)+'</td>'
      +'<td>'+esc(e.target_agent||e.ref_task_id||'')+'</td><td>'+esc((d||'').slice(0,160))+'</td></tr>';}).join('');
}
function renderStats(snap,tasks){
  const ag=snap.agents||{}, nAll=Object.keys(ag).length;
  const nRun=Object.values(ag).filter(v=>v==='in_progress').length;
  const tc={}; tasks.forEach(t=>tc[t.status]=(tc[t.status]||0)+1);
  const gate=(snap.audit_gate||{}).decision||'?';
  $('#stats').innerHTML='<span><span class="dot" style="background:var(--brand)"></span>机器人 <b>'+nRun+'/'+nAll+'</b> 运行中</span>'
    +'<span>任务 <b>'+tasks.length+'</b>（run '+(tc.running||0)+' / pend '+(tc.pending||0)+' / done '+(tc.done||0)+' / fail '+(tc.failed||0)+'）</span>'
    +'<span>审计关口 <b style="color:'+(gate==='PASS'?'var(--brand)':'var(--err)')+'">'+esc(gate)+'</b></span>';
}

async function j(u){const r=await fetch(u,{cache:'no-store'}); if(!r.ok) throw new Error(u+' '+r.status); return r.json();}
async function jopt(u){ try{ return await j(u); }catch(e){ return null; } }

/* ---- 详情抽屉 ---- */
function metaHtml(t){
  const r=t.result||{};
  const rows=[['机器人',t.agent_name],['状态',t.status],['任务 ID',t.task_id],
    ['模型',r.model],['耗时(秒)',r.elapsed_sec],['轮次',r.num_turns],['退出码',r.exit_code],
    ['轨迹',r.trace],['创建',dt(t.created_at)],['更新',dt(t.updated_at)]];
  return '<div class="sec">任务信息</div><div class="meta">'
    + rows.filter(x=>x[1]!=null&&x[1]!=='').map(x=>'<div class="k">'+esc(x[0])+'</div><div>'+esc(x[1])+'</div>').join('')
    + '</div>';
}
function resultHtml(t){
  const r=t.result||{};
  let h='<div class="sec">终态结论</div>';
  if(t.title) h+='<div class="mtext">'+esc(t.title)+'</div>';
  if(r.result) h+='<div class="mtext">'+esc(r.result)+'</div>';
  if(r.detail) h+='<div class="sec rerr">错误详情</div><pre>'+esc(typeof r.detail==='string'?r.detail:JSON.stringify(r.detail,null,1))+'</pre>';
  if(!r.result && !r.detail) h+='<div class="empty">（该任务无终态文本）</div>';
  return h;
}
function evHtml(ev){
  const ty=ev.type;
  if(ty==='assistant'){
    const parts=(ev.message&&ev.message.content)||[];
    return parts.map(c=>{
      if(c.type==='text') return '<div class="mtext">'+esc(c.text)+'</div>';
      if(c.type==='thinking') return '<div class="think">💭 思考\n'+esc(c.thinking||'')+'</div>';
      if(c.type==='tool_use') return '<div class="tool">🔧 调用工具 <b>'+esc(c.name)+'</b><pre>'+esc(JSON.stringify(c.input,null,1))+'</pre></div>';
      return '<div class="toolres">'+esc(JSON.stringify(c))+'</div>';
    }).join('');
  }
  if(ty==='user'){
    const parts=(ev.message&&ev.message.content)||[];
    return parts.map(c=>{
      if(c.type==='tool_result'){
        let tx=c.content; if(Array.isArray(tx)) tx=tx.map(x=>x.text||'').join('\n');
        return '<div class="toolres'+(c.is_error?' err':'')+'">↳ 工具结果<pre>'+esc(String(tx==null?'':tx).slice(0,5000))+'</pre></div>';
      }
      if(c.type==='text') return '<div class="mtext">'+esc(c.text)+'</div>';
      return '';
    }).join('');
  }
  if(ty==='system'){
    if(ev.subtype==='init') return '<div class="sys">⚙ 会话初始化 · 模型工具 '+((ev.tools||[]).length)+' 个 · cwd='+esc(ev.cwd||'')+'</div>';
    return '';
  }
  if(ty==='result') return '<div class="resline">✅ 终态 result · 轮次 '+esc(ev.num_turns)+' · is_error='+esc(ev.is_error)+' · 用时 '+esc(ev.duration_ms)+'ms</div>';
  if(ty==='_stderr') return '<div class="sec rerr">stderr</div><pre>'+esc(ev.text||'')+'</pre>';
  return '';
}
function traceHtml(tr){
  let h='<div class="sec">执行轨迹（逐步）</div>';
  if(!tr.exists){ return h+'<div class="empty">暂无轨迹记录：该任务在「轨迹功能」上线前执行，或轨迹文件缺失。</div>'; }
  const evs=tr.events||[];
  const body=evs.map(evHtml).filter(s=>s&&s.trim()).join('');
  h+='<div class="hint">轨迹文件：'+esc(tr.path||'')+' · 事件 '+evs.length+' 条</div>';
  h+= body || '<div class="empty">轨迹为空</div>';
  return h;
}
async function openDetail(tid){
  const t=TASKMAP[tid]||{task_id:tid};
  $('#dt').textContent = tid + (t.agent_name?(' · '+t.agent_name):'');
  $('#dbody').innerHTML = metaHtml(t)+resultHtml(t)+'<div class="spin">加载轨迹中…</div>';
  $('#drawer').classList.add('open');
  try{
    const tr=await j('/trace?agent='+encodeURIComponent(t.agent_name||'')+'&task='+encodeURIComponent(tid));
    $('#dbody').innerHTML = metaHtml(t)+resultHtml(t)+traceHtml(tr);
  }catch(e){
    $('#dbody').innerHTML = metaHtml(t)+resultHtml(t)+'<div class="empty">轨迹读取失败：'+esc(e.message)+'</div>';
  }
}
$('#dclose').addEventListener('click',()=>$('#drawer').classList.remove('open'));
document.addEventListener('keydown',e=>{ if(e.key==='Escape') $('#drawer').classList.remove('open'); });
$('#tasks tbody').addEventListener('click',e=>{
  const tr=e.target.closest('tr.row'); if(tr) openDetail(tr.getAttribute('data-tid'));
});
$('#agents').addEventListener('change', async (e)=>{
  const sel=e.target.closest('select.msel'); if(!sel) return;
  const agent=sel.getAttribute('data-agent'), model=sel.value;
  sel.disabled=true;
  try{
    const r=await fetch('/setmodel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({agent:agent,model:model})});
    const d=await r.json();
    if(!d||!d.ok) alert('设置失败：'+((d&&d.error)||'未知错误'));
    await tick();
  }catch(err){ alert('设置失败：'+err.message); }
  finally{ sel.disabled=false; }
});

/* ---- 对话面板（点机器人卡片进入） ---- */
let CURAGENT = null;
let LIVE_TIMERS = [];
function clearLive(){ LIVE_TIMERS.forEach(t=>clearInterval(t)); LIVE_TIMERS=[]; }
function openChat(agent){
  CURAGENT = agent;
  document.querySelectorAll('.drawer.open').forEach(d=>d.classList.remove('open'));
  $('#ct').textContent = '与 ' + agent + ' 对话';
  $('#chat').classList.add('open');
  loadChat();
}
async function loadChat(){
  if(!CURAGENT) return;
  let tasks = [];
  try{ tasks = await j('/api/tasks?agent='+encodeURIComponent(CURAGENT)); }catch(e){ tasks = []; }
  const msgs = tasks.filter(t=>String(t.task_id).startsWith('CHAT-'))
                    .sort((a,b)=>String(a.created_at||'').localeCompare(String(b.created_at||'')));
  const box=$('#cbody');
  let html='';
  if(!msgs.length){ html+='<div class="empty">还没有对话。在下方输入开始。</div>'; }
  else{
    html += msgs.map(m=>{
      let h='<div class="bub u">'+esc(m.title||'')+'</div>';
      if(m.status==='done' && m.result && m.result.result) h+='<div class="bub a">'+esc(m.result.result)+'</div>';
      else if(['running','pending','claimed'].includes(m.status)) h+='<div class="bub a pending">…思考中</div>';
      else if(m.status==='failed') h+='<div class="bub a err">（执行失败）'+esc((m.result&&m.result.detail)||'')+'</div>';
      return h;
    }).join('');
  }
  const work = tasks.filter(t=>!String(t.task_id).startsWith('CHAT-'))
                    .sort((a,b)=>String(b.updated_at||'').localeCompare(String(a.updated_at||'')))
                    .slice(0,25);
  html += '<div class="thwrap"><div class="thhd">任务历史（近 '+work.length+' 条 · 点条目展开结果）</div>';
  if(!work.length){ html += '<div class="empty">暂无</div>'; }
  else{
    html += work.map(t=>{
      const st=String(t.status||'');
      const cls = st==='done'?'ok':(st==='failed'?'err':(st==='blocked'?'blk':'run'));
      const res=(t.result&&t.result.result)?String(t.result.result):'';
      const det=(t.result&&t.result.detail)?String(t.result.detail):'';
      const body = res? res.slice(0,6000) : (det? det.slice(0,3000) : '(无结果文本；状态='+st+')');
      const by=(t.result&&t.result.by)?(' · by='+t.result.by):'';
      return '<div class="thitem" data-tid="'+esc(t.task_id)+'">'
        +'<div class="thhead"><span class="thst '+cls+'">'+esc(st)+'</span>'
        +'<span class="thid">'+esc(t.task_id)+by+'</span>'
        +'<span class="thlive">▶ 实时输出</span>'
        +'<span class="thtime">'+esc(String(t.updated_at||'').replace('T',' ').slice(0,19))+'</span></div>'
        +'<div class="thttl">'+esc(String(t.title||'').slice(0,160))+'</div>'
        +'<div class="thres" style="display:none">'+esc(body)+'</div>'
        +'</div>';
    }).join('');
  }
  html += '</div>';
  box.innerHTML = html;
  box.querySelectorAll('.thitem').forEach(el=>{
    el.addEventListener('click',()=>{
      const r=el.querySelector('.thres');
      if(r) r.style.display = (r.style.display==='none' ? 'block' : 'none');
    });
  });
  box.querySelectorAll('.thlive').forEach(b=>{
    b.addEventListener('click',(ev)=>{
      ev.stopPropagation();
      const item=b.closest('.thitem'); if(!item) return;
      const tid=item.getAttribute('data-tid');
      const res=item.querySelector('.thres');
      res.style.display='block';
      res.textContent='(连接中…)';
      if(item._liveTimer) clearInterval(item._liveTimer);
      const tick=async()=>{
        try{
          const d=await j('/live?agent='+encodeURIComponent(CURAGENT)+'&task='+encodeURIComponent(tid));
          if(!d||!d.exists){ res.textContent='(暂无轨迹文件；执行体开跑后才会出现)'; return; }
          res.textContent=(d.lines||[]).join('\n')+'\n\n-- 更新于 '+new Date().toLocaleTimeString()+' · '+d.size+'B --';
          res.scrollTop=res.scrollHeight;
        }catch(e){ res.textContent='读取失败：'+e.message; }
      };
      tick();
      item._liveTimer=setInterval(tick,2000);
      LIVE_TIMERS.push(item._liveTimer);
    });
  });
  box.scrollTop = 0;
}
async function sendChat(){
  const ta=$('#cinput'); const txt=(ta.value||'').trim();
  if(!txt || !CURAGENT) return;
  ta.value=''; $('#csend').disabled=true;
  try{
    const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
                                 body:JSON.stringify({agent:CURAGENT,text:txt})});
    const d=await r.json();
    if(!d||!d.ok){ alert('发送失败：'+((d&&d.error)||'未知错误')); return; }
    await loadChat();
    const tid=d.task_id;
    for(let i=0;i<50;i++){
      await new Promise(s=>setTimeout(s,2500));
      const all=await j('/api/tasks?agent='+encodeURIComponent(CURAGENT));
      const t=all.find(x=>x.task_id===tid);
      await loadChat();
      if(t && (t.status==='done'||t.status==='failed')) break;
    }
  }catch(e){ alert('发送失败：'+e.message); }
  finally{ $('#csend').disabled=false; }
}
$('#cclose').addEventListener('click',()=>{ clearLive(); $('#chat').classList.remove('open'); });
$('#csend').addEventListener('click',sendChat);
$('#cinput').addEventListener('keydown',e=>{ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); sendChat(); } });
document.addEventListener('keydown',e=>{ if(e.key==='Escape'){ clearLive(); $('#chat').classList.remove('open'); } });
$('#agents').addEventListener('click',e=>{
  if(e.target.closest('.msel')) return;   // 点模型下拉时不进入对话
  const card=e.target.closest('.card.clickable');
  if(card) openChat(card.getAttribute('data-agent'));
});

let busy=false;
async function tick(){
  if(busy) return; busy=true;
  try{
    const [snap,tasks,evs,kv]=await Promise.all([j('/api/snapshot'),j('/api/tasks'),j('/api/events?limit=30&order=desc'),jopt('/api/kv/daemon_model')]);
    MODELMAP = (kv && kv.value) || {};
    renderStats(snap,tasks); renderAgents(snap.agent_detail||[]); renderTasks(tasks); renderEvents(evs);
    $('#foot').textContent='最后刷新 '+new Date().toLocaleTimeString()+' · 每 3 秒自动刷新';
  }catch(e){
    $('#foot').innerHTML='<span style="color:var(--err)">无法连接中枢（'+esc(e.message)+'）—— 请确认中枢 FastAPI 127.0.0.1:8765 在运行</span>';
  }finally{ busy=false; }
}
tick(); setInterval(tick,3000);
</script>
</body>
</html>
"""
# ==========================================================================


class Handler(BaseHTTPRequestHandler):
    server_version = "QuantHubDashboard/1.1"

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):  # noqa: N802
        pr = urllib.parse.urlparse(self.path)
        path, qs = pr.path, urllib.parse.parse_qs(pr.query)

        if path in ("/", "/index.html", "/dashboard"):
            html = PAGE.replace("__MODELS__", json.dumps(MODELS, ensure_ascii=False))
            self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
            return

        # 轨迹读取：直接读本地 daemon_traces 文件（无需改中枢）
        if path == "/trace":
            agent = (qs.get("agent", [""])[0] or "unknown")
            task = (qs.get("task", [""])[0] or "")
            if not _SAFE.match(agent) or not _SAFE.match(task):
                self._json({"exists": False, "events": [], "error": "非法的 agent/task 名"}, 400)
                return
            f = TRACE_ROOT / agent / (task + ".jsonl")
            if not f.is_file():
                self._json({"exists": False, "events": [], "path": str(f)})
                return
            evs = []
            try:
                for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        evs.append(json.loads(ln))
                    except Exception:
                        evs.append({"type": "raw", "text": ln})
                self._json({"exists": True, "events": evs, "path": str(f)})
            except Exception as e:  # noqa: BLE001
                self._json({"exists": False, "events": [], "error": str(e)}, 502)
            return

        if path.startswith("/live") or pr.path == "/live":
            qs = urllib.parse.parse_qs(pr.query)
            agent = (qs.get("agent", [""])[0] or "")
            task = (qs.get("task", [""])[0] or "")
            if not _SAFE.match(agent) or not _SAFE.match(task):
                self._json({"exists": False, "error": "非法的 agent/task 名"}, 400)
                return
            f = TRACE_ROOT / agent / (task + ".jsonl")
            if not f.is_file():
                self._json({"exists": False, "lines": []})
                return
            try:
                st = f.stat()
                txt = f.read_text(encoding="utf-8", errors="replace")
            except Exception as e:  # noqa: BLE001
                self._json({"exists": False, "error": str(e)}, 502)
                return
            out = []
            for ln in txt.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    o = json.loads(ln)
                except Exception:  # noqa: BLE001
                    out.append(ln[:2000])
                    continue
                if not isinstance(o, dict):
                    out.append(str(o)[:500])
                    continue
                t = o.get("type")
                s = None
                if t == "_meta":
                    s = "▶ 开始 · agent=%s model=%s %s" % (
                        o.get("agent"), o.get("model"), o.get("started_at") or "")
                elif t == "_stderr":
                    s = "[stderr] " + str(o.get("text") or "")[:2000]
                elif t == "_timeout":
                    s = "⏱ 超时 " + str(o.get("timeout_sec"))
                elif t == "result":
                    s = ("❌ " if o.get("is_error") else "✅ 完成 · ") + str(o.get("result") or "")[:3000]
                elif t in ("system", "file-history-snapshot", "summary"):
                    s = str(o.get("subtype") or t)
                else:
                    for k in ("text", "content", "delta"):
                        v = o.get(k)
                        if isinstance(v, str) and v.strip():
                            s = v.strip()[:2000]
                            break
                    if s is None:
                        msg = o.get("message")
                        if isinstance(msg, dict):
                            c = msg.get("content")
                            if isinstance(c, str):
                                s = c[:2000]
                            elif isinstance(c, list):
                                parts = []
                                for it in c:
                                    if not isinstance(it, dict):
                                        continue
                                    if it.get("type") == "text":
                                        parts.append(str(it.get("text") or ""))
                                    elif it.get("type") == "tool_use":
                                        parts.append("[工具] " + str(it.get("name") or ""))
                                    elif it.get("type") == "tool_result":
                                        parts.append("[结果] " + str(it.get("content") or "")[:400])
                                s = "\n".join(p for p in parts if p)[:2000]
                    if s is None:
                        s = json.dumps(o, ensure_ascii=False)[:400]
                out.append(("[" + str(t) + "] " if t else "") + s)
            self._json({"exists": True, "lines": out[-400:], "size": st.st_size,
                        "mtime": time.strftime("%H:%M:%S", time.localtime(st.st_mtime))})
            return

        if path.startswith("/api/"):
            target = HUB_BASE + self.path[len("/api"):]  # 保留 query
            try:
                req = urllib.request.Request(target, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    self._send(200, "application/json; charset=utf-8", r.read())
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e), "target": target}, 502)
            return

        self._send(404, "text/plain; charset=utf-8", b"not found (try / )")

    def do_POST(self):  # noqa: N802
        pr = urllib.parse.urlparse(self.path)
        if pr.path == "/setmodel":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
                return
            agent = str(data.get("agent") or "")
            model = str(data.get("model") or "")
            if not _SAFE.match(agent) or (model and not re.match(r"^[A-Za-z0-9_.:\-]+$", model)):
                self._json({"ok": False, "error": "非法的 agent/model"}, 400)
                return
            try:
                tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
            except Exception:
                tok = ""
            # 读现值 → 合并 → 写回中枢 KV
            cur = {}
            try:
                with urllib.request.urlopen(HUB_BASE + "/kv/daemon_model", timeout=8) as r:
                    cur = (json.loads(r.read()) or {}).get("value") or {}
                if not isinstance(cur, dict):
                    cur = {}
            except Exception:
                cur = {}
            if model:
                cur[agent] = model
            else:
                cur.pop(agent, None)
            body = json.dumps({"key": "daemon_model", "value": cur}).encode("utf-8")
            req = urllib.request.Request(HUB_BASE + "/kv", data=body, method="PUT",
                                         headers={"Content-Type": "application/json", "X-Hub-Token": tok})
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    r.read()
                self._json({"ok": True, "agent": agent, "model": model, "map": cur})
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": str(e)}, 502)
            return

        if pr.path == "/chat":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
                return
            agent = str(data.get("agent") or "")
            text = str(data.get("text") or "").strip()
            if not _SAFE.match(agent) or not text:
                self._json({"ok": False, "error": "agent 或 text 缺失"}, 400)
                return
            try:
                tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
            except Exception:
                tok = ""
            # 取该 Agent 的历史对话任务（task_id 以 CHAT- 开头）
            hist = []
            try:
                with urllib.request.urlopen(
                        HUB_BASE + "/tasks?agent=" + urllib.parse.quote(agent), timeout=8) as r:
                    tasks = json.loads(r.read()) or []
                hist = [t for t in tasks if str(t.get("task_id", "")).startswith("CHAT-")]
                hist.sort(key=lambda t: t.get("created_at") or t.get("updated_at") or "")
            except Exception:
                hist = []
            lines = ["你正在与「总控（人类用户）」进行**持续对话**。下面是本线程的历史上下文，"
                     "请保持前后一致，并只针对本轮最后一句话**直接回复**"
                     "（像聊天一样自然作答；不要复述任务流程、不要套报告格式、不要罗列无关内容）。"]
            for h in hist[-6:]:
                lines.append("用户：" + str(h.get("title") or ""))
                u = (h.get("result") or {}).get("result")
                if u:
                    lines.append("你：" + str(u))
            lines.append("用户：" + text)
            prompt = "\n\n".join(lines)

            tid = "CHAT-%s-%s" % (agent, time.strftime("%Y%m%d-%H%M%S"))
            body = json.dumps({"task_id": tid, "agent_name": agent, "title": text,
                               "payload": {"prompt": prompt, "chat": True},
                               "priority": "high"}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                HUB_BASE + "/tasks", data=body, method="POST",
                headers={"Content-Type": "application/json; charset=utf-8", "X-Hub-Token": tok})
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    r.read()
                self._json({"ok": True, "task_id": tid})
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": str(e)}, 502)
            return

        self._send(404, "text/plain; charset=utf-8", b"not found")

    def log_message(self, *args):  # 静默访问日志
        pass


def main() -> None:
    global HUB_BASE
    ap = argparse.ArgumentParser(description="QuantHub 机器人执行看板")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--hub", default=HUB_BASE)
    args = ap.parse_args()
    HUB_BASE = args.hub.rstrip("/")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("[dashboard] http://127.0.0.1:%d/  →  中枢 %s" % (args.port, HUB_BASE), flush=True)
    print("[dashboard] 轨迹目录 %s" % TRACE_ROOT, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
