#!/usr/bin/env python3
"""Build a self-contained, read-only HTML dashboard from Agentic run logs."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hmac
import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from agentic import (
    default_state_dir,
    legacy_runs_dir,
    legacy_worktrees_dir,
    state_paths,
)
from agentic_persistence import atomic_write_text


def default_logs_root(repo: Path, state_dir: Path | None = None) -> Path:
    return state_paths(repo, state_dir).runs


def _json_for_script(value: Any) -> str:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def render_dashboard(runs: list[dict[str, Any]], repo_name: str, logs_name: str, *,
                     settings: dict[str, Any] | None = None) -> str:
    """Render a read-only dashboard; browser actions never write repository state."""
    generated = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    data = _json_for_script({"runs": runs, "repo": repo_name, "logs": logs_name,
                             "generated": generated, "settings": settings or {}})
    template = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentic dashboard</title><style>
:root{color-scheme:dark;--bg:#0b1020;--surface:#121a2d;--surface2:#18233b;--text:#edf3ff;--muted:#9aa9c4;--line:#2a3855;--blue:#6ea8fe;--green:#55d187;--yellow:#f2c86b;--red:#ff7d8b;--purple:#be9cff;--shadow:0 18px 45px #0005} [data-theme=light]{color-scheme:light;--bg:#f4f7fb;--surface:#fff;--surface2:#eef3fb;--text:#17233b;--muted:#5b6a83;--line:#d8e0ee;--blue:#2869d8;--green:#16834b;--yellow:#996900;--red:#bf3045;--purple:#7041b3;--shadow:0 18px 45px #31466a1c}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 12% 0%,#25376255,transparent 35%),var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif}button,input,select{font:inherit}button,select{cursor:pointer}.app{display:grid;grid-template-columns:230px minmax(0,1fr);min-height:100vh}.sidebar{position:sticky;top:0;height:100vh;padding:24px 14px;background:var(--surface);border-right:1px solid var(--line)}.brand{padding:0 12px 24px}.brand h1{margin:0;font-size:23px}.brand p{margin:4px 0;color:var(--muted);font-size:12px;overflow-wrap:anywhere}.nav{display:grid;gap:5px}.nav button{width:100%;border:0;border-radius:10px;padding:11px 12px;background:transparent;color:var(--muted);text-align:left}.nav button:hover,.nav button.active{background:var(--surface2);color:var(--text)}.nav button.active{box-shadow:inset 3px 0 var(--blue)}.content{min-width:0;padding:28px}header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:22px}h1{margin:0 0 4px;font-size:clamp(26px,4vw,38px)}h2,h3{margin:0}.subtle,.meta{color:var(--muted)}.actions,.toolbar,.filter-row{display:flex;gap:8px;flex-wrap:wrap}.button,input,select{border:1px solid var(--line);border-radius:10px;background:var(--surface);color:var(--text);padding:9px 12px}.toolbar{margin-bottom:18px}.toolbar input{min-width:220px;flex:1 1 260px}.filter-row select{min-width:120px}.view{display:none}.view.active{display:block}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px;margin-bottom:20px}.metric,.panel{background:var(--surface);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow)}.metric{padding:16px 18px}.metric strong{display:block;font-size:30px;margin-top:5px}.metric .label{color:var(--muted);font-size:13px}.workspace{display:grid;grid-template-columns:minmax(300px,.85fr) minmax(0,1.6fr);gap:18px;align-items:start}.panel{overflow:hidden}.run-list{max-height:720px;overflow:auto}.run-card{display:block;width:100%;text-align:left;padding:16px;border:0;border-bottom:1px solid var(--line);background:transparent;color:inherit}.run-card:hover,.run-card.selected{background:var(--surface2)}.run-top,.detail-top,.row{display:flex;justify-content:space-between;gap:12px;align-items:center}.run-title{font-weight:700}.badge{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:3px 9px;font-size:12px;font-weight:700;white-space:nowrap}.badge:before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}.approved{color:var(--green);background:#55d18726}.needs-fix,.tests-failed,.investigation-invalid,.blocked,.retry_exhausted{color:var(--red);background:#ff7d8b26}.awaiting-development,.awaiting-review,.no_changes{color:var(--yellow);background:#f2c86b26}.in-progress{color:var(--blue);background:#6ea8fe26}.no-activity{color:var(--muted);background:#9aa9c426}.summary{margin:10px 0 8px;color:var(--muted)}.meta{display:flex;flex-wrap:wrap;gap:8px 14px;font-size:12px}.detail{padding:22px;min-height:420px}.recent-heading{padding:22px}.chart-panel{padding:20px}.panel-heading{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin-bottom:18px}.token-controls{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:12px}.token-chart{display:grid;grid-template-columns:repeat(auto-fit,minmax(74px,1fr));gap:10px;align-items:end;min-height:190px;overflow-x:auto;padding:12px 4px 0;border-bottom:1px solid var(--line)}.token-column{min-width:0;display:grid;grid-template-rows:22px 132px 34px;gap:4px;align-items:end;text-align:center}.token-value{font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.token-bar-track{height:132px;display:flex;align-items:end;justify-content:center}.token-bar{width:min(42px,72%);min-height:4px;border-radius:7px 7px 0 0;background:linear-gradient(180deg,var(--purple),var(--blue))}.token-bar.unknown{height:4px;background:repeating-linear-gradient(135deg,var(--line) 0 5px,transparent 5px 10px)}.token-label,.token-legend{color:var(--muted);font-size:11px}.token-legend{font-size:12px}table{width:100%;border-collapse:collapse}th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--muted);font-size:12px}.table-wrap{overflow-x:auto}.detail h3{margin:24px 0 9px;font-size:15px}.branch{color:var(--muted);font:12px monospace;overflow-wrap:anywhere}.callout{margin-top:18px;padding:14px 16px;border-left:3px solid var(--blue);background:var(--surface2);border-radius:8px}.callout.rejected{border-color:var(--red)}.callout.approved{border-color:var(--green)}.findings{margin:10px 0;padding-left:20px}.file-list{display:grid;gap:8px;padding:12px}details{border:1px solid var(--line);border-radius:10px;overflow:hidden}summary{padding:10px 12px;cursor:pointer;background:var(--surface2)}pre{margin:0;padding:13px;max-height:300px;overflow:auto;background:var(--bg);color:var(--muted);white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.5 monospace}.log-actions{display:flex;gap:6px;padding:7px 10px;border-top:1px solid var(--line)}.small-button{padding:4px 8px;border:1px solid var(--line);border-radius:7px;background:var(--surface);color:var(--text);font-size:12px}.empty{padding:38px 20px;text-align:center;color:var(--muted)}.notice{margin:0 0 18px;padding:10px 12px;border-radius:9px;background:var(--surface2);color:var(--muted);font-size:13px}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}@media(max-width:880px){.app{grid-template-columns:1fr}.sidebar{position:static;height:auto;padding:12px;border-right:0;border-bottom:1px solid var(--line)}.brand{padding:4px 8px 12px}.nav{grid-template-columns:repeat(3,1fr)}.nav button{text-align:center}.workspace{grid-template-columns:1fr}.run-list{max-height:none}}@media(max-width:620px){.content{padding:18px 12px}header{display:block}.actions{margin-top:14px}.toolbar input{min-width:100%;flex-basis:100%}.panel-heading{display:block}.nav{grid-template-columns:repeat(2,1fr)}}
</style></head><body><div class="app"><aside class="sidebar" aria-label="Dashboard navigation"><div class="brand"><h1>Agentic</h1><p id="repo-name"></p></div><nav class="nav"><button type="button" data-view="dashboard">Dashboard</button><button type="button" data-view="runs">Runs</button><button type="button" data-view="logs">Logs</button><button type="button" data-view="tokens">Token usage</button><button type="button" data-view="issues">Issues</button><button type="button" data-view="settings">Settings</button></nav></aside><main class="content"><header><div><h1 id="page-title">Dashboard</h1><p class="subtle" id="source"></p></div><div class="actions"><button class="button" id="refresh" type="button">Refresh</button><button class="button" id="theme" type="button">Toggle theme</button></div></header>
<div class="toolbar" aria-label="Global dashboard filters"><label class="sr-only" for="search">Search runs, issues, logs, and errors</label><input id="search" type="search" placeholder="Search runs, issues, logs, errors…"><div class="filter-row"><label class="sr-only" for="status">Filter status</label><select id="status"><option value="all">All statuses</option><option value="blocked">Blocked</option><option value="no_changes">No changes</option><option value="retry_exhausted">Retry exhausted</option><option value="needs-fix">Needs fix</option><option value="investigation-invalid">Investigation invalid</option><option value="awaiting-development">Awaiting development</option><option value="approved">Approved</option><option value="in-progress">In progress</option><option value="tests-failed">Tests failed</option><option value="awaiting-review">Awaiting review</option><option value="no-activity">No activity</option></select><label class="sr-only" for="issue">Filter issue</label><select id="issue"><option value="all">All issues</option></select><label class="sr-only" for="stage">Filter stage</label><select id="stage"><option value="all">All stages</option><option value="investigation">Investigation</option><option value="development">Development</option><option value="tests">Tests</option><option value="review">Review</option><option value="completed">Completed</option><option value="blocked">Blocked</option><option value="idle">Idle</option></select><label class="sr-only" for="severity">Filter severity</label><select id="severity"><option value="all">All severities</option><option value="BLOCKER">Blocker</option><option value="CRITICAL">Critical</option><option value="MAJOR">Major</option><option value="HIGH">High</option><option value="MEDIUM">Medium</option><option value="MINOR">Minor</option><option value="LOW">Low</option><option value="UNSPECIFIED">Unspecified</option><option value="NONE">No finding</option></select><label class="sr-only" for="model">Filter model</label><select id="model"><option value="all">All models</option></select><label class="sr-only" for="provider">Filter provider</label><select id="provider"><option value="all">All providers</option></select></div></div><p class="notice" id="live-status" role="status"></p>
<section class="view active" data-panel="dashboard"><div class="metrics" aria-label="Run summary" id="metrics"></div><div id="recent"></div></section><section class="view" data-panel="runs"><div class="workspace"><div class="panel"><div class="run-list" id="run-list" aria-live="polite"></div></div><article class="panel detail" id="detail" aria-live="polite"></article></div></section><section class="view" data-panel="logs"><div class="panel"><div class="file-list" id="logs-list"></div></div></section><section class="view" data-panel="tokens"><section class="panel chart-panel"><div class="panel-heading"><div><h2>Token usage by run</h2><p class="subtle">Known totals for each provider invocation, ordered from oldest to newest. The highest run number is the latest.</p></div><label>Time period <select id="period"><option value="all">All time</option><option value="24h">Last 24 hours</option><option value="7d">Last 7 days</option><option value="30d">Last 30 days</option></select></label></div><div class="token-controls"><span class="token-sort-slot"></span><span class="token-legend">Striped bars = usage unavailable</span></div><div class="token-chart" id="token-chart" role="img" aria-label="Token usage chart"></div><div class="table-wrap" id="token-table"></div></section></section><section class="view" data-panel="issues"><div class="panel table-wrap" id="issues-table"></div></section><section class="view" data-panel="settings"><div class="panel detail" id="settings-panel"></div></section></main></div>
<script type="application/json" id="dashboard-data">__DASHBOARD_DATA__</script><script>
const DATA=JSON.parse(document.querySelector("#dashboard-data").textContent);const state={selected:DATA.runs[0]?.id||null,query:"",status:"all",issue:"all",stage:"all",severity:"all",model:"all",provider:"all",period:"all",view:"dashboard"};const labels={approved:"Approved","needs-fix":"Needs fix","in-progress":"In progress","tests-failed":"Tests failed","awaiting-review":"Awaiting review","no-activity":"No activity",blocked:"Blocked",no_changes:"No changes",retry_exhausted:"Retry exhausted","investigation-invalid":"Investigation invalid","awaiting-development":"Awaiting development"};const esc=value=>String(value??"").replace(/[&<>"']/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));const badge=status=>`<span class="badge ${esc(status)}">${esc(labels[status]||status)}</span>`;const tokenValue=entry=>{const value=Number(entry?.total_tokens??entry?.usage?.total_tokens);return Number.isFinite(value)&&value>=0?value:null};const formatTokens=value=>value==null?"unknown":Number(value).toLocaleString();const formatJson=value=>{if(value==null)return"No security policy recorded.";try{return JSON.stringify(value,null,2)}catch{return String(value)}};const formatDuration=seconds=>{if(seconds==null||!Number.isFinite(Number(seconds)))return"duration unavailable";const value=Number(seconds);return value<60?`${value.toFixed(1)}s`:`${Math.floor(value/60)}m ${Math.round(value%60)}s`};const runDuration=run=>formatDuration(run.usage?.metrics?.duration_seconds);const runSummary=run=>{const investigationSummary=run.investigation.state==="not-run"?"":run.investigation.summary;return investigationSummary||run.review.summary||run.developer.summary||"No summary recorded."};const searchableCache=new Map();const searchable=run=>{if(!searchableCache.has(run.id))searchableCache.set(run.id,JSON.stringify(run).toLowerCase());return searchableCache.get(run.id)};const filtered=()=>DATA.runs.filter(run=>{const issue=run.issue==null?"local":String(run.issue);return(state.status==="all"||run.status===state.status)&&(state.issue==="all"||issue===state.issue)&&(state.stage==="all"||run.stage===state.stage)&&(state.severity==="all"||run.severity===state.severity)&&(state.model==="all"||(run.models||[]).includes(state.model))&&(state.provider==="all"||(run.providers||[]).includes(state.provider))&&searchable(run).includes(state.query.toLowerCase())});
function renderMetrics(){const runs=DATA.runs;const observed=runs.reduce((total,run)=>total+(tokenValue(run)??0),0);const known=runs.filter(run=>tokenValue(run)!=null).length;const values=[["Total runs",runs.length,""],["Approved",runs.filter(run=>run.status==="approved").length,"approved"],["Needs attention",runs.filter(run=>["needs-fix","tests-failed","blocked"].includes(run.status)).length,"needs-fix"],["In progress",runs.filter(run=>run.status==="in-progress").length,"in-progress"],["Tokens observed",known?formatTokens(observed):"unknown",""]];document.querySelector("#metrics").innerHTML=values.map(([label,value,kind])=>`<div class="metric"><span class="label">${esc(label)}</span><strong class="${kind}">${esc(value)}</strong></div>`).join("");const activity=runs.slice(0,6).map(run=>`<button class="run-card" data-recent-run="${esc(run.id)}" type="button"><div class="run-top"><span class="run-title">${esc(run.label)}</span>${badge(run.status)}</div><div class="summary">${esc(runSummary(run))}</div><div class="meta"><span>${esc(run.last_activity)}</span><span>Stage: ${esc(run.stage)}</span><span>Tests: ${esc(run.tests.status)}</span></div></button>`).join("");document.querySelector("#recent").innerHTML=`<section class="panel"><div class="recent-heading"><h2>Recent activity</h2><p class="subtle">${esc(runs.length?"Latest recorded run activity, newest first.":"No runs recorded yet.")}</p></div>${activity||""}</section>`}
function invocationsForRuns(runs){return runs.flatMap(run=>{const invocations=Array.isArray(run.usage?.by_invocation)?run.usage.by_invocation.filter(item=>item&&typeof item==="object"):[];return(invocations.length?invocations:[{name:"run total",...run.usage}]).map(invocation=>({run,invocation}))})}
function renderTokenChart(){const chart=document.querySelector("#token-chart");const cutoff={"24h":86400000,"7d":604800000,"30d":2592000000}[state.period];const tokenRuns=filtered().filter(run=>!cutoff||(Date.now()-Date.parse(run.last_activity)<=cutoff));if(!tokenRuns.length){chart.innerHTML=`<div class="empty">No runs match this filter.</div>`;document.querySelector("#token-table").innerHTML="";return}const entries=[...tokenRuns].reverse().flatMap(run=>{const invocations=Array.isArray(run.usage?.by_invocation)?run.usage.by_invocation.filter(item=>item&&typeof item==="object"):[];if(!invocations.length)return[{run,invocation:null}];return invocations.map((invocation,index)=>({run,invocation,index}))});const known=entries.map(entry=>tokenValue(entry.invocation||entry.run.usage)).filter(value=>value!=null);const max=Math.max(...known,1);chart.setAttribute("aria-label",`Token usage chart for ${entries.length} executions`);chart.innerHTML=entries.map((entry,index)=>{const run=entry.run;const invocation=entry.invocation;const tokens=tokenValue(invocation||run.usage);const runNumber=index+1;const latest=index===entries.length-1;const runTag=`Run ${runNumber}${latest?" · latest":""}`;const runLabel=run.issue==null?run.label.replace(/^Local task: /,""):`#${run.issue}`;const invocationLabel=invocation?(invocation.name||invocation.role||`run ${entry.index+1}`):runLabel;const label=`${runTag} · ${runLabel}${invocation?` · ${invocationLabel}`:""}`;const height=tokens==null?4:Math.max(4,Math.round(tokens/max*132));return`<div class="token-column" title="${esc(runTag)} · ${esc(run.label)} · ${esc(invocationLabel)}: ${esc(formatTokens(tokens))} tokens"><div class="token-value">${esc(formatTokens(tokens))}</div><div class="token-bar-track"><div class="token-bar ${tokens==null?"unknown":""}" style="height:${height}px"></div></div><div class="token-label">${esc(label)}</div></div>`}).join("");const invocationRows=invocationsForRuns(tokenRuns).map(({run,invocation})=>`<tr><td>${esc(run.label)}</td><td>${esc(invocation.name||invocation.role||"run")}</td><td>${esc(invocation.provider||run.providers?.join(", ")||"unknown")}</td><td>${esc(invocation.model||run.models?.join(", ")||"unknown")}</td><td>${esc(formatTokens(tokenValue(invocation)))}</td></tr>`).join("");document.querySelector("#token-table").innerHTML=`<table><thead><tr><th>Run</th><th>Invocation</th><th>Provider</th><th>Model</th><th>Tokens</th></tr></thead><tbody>${invocationRows||`<tr><td colspan="5">No provider invocations recorded.</td></tr>`}</tbody></table>`}
function renderList(){const visible=filtered();if(!visible.some(run=>run.id===state.selected))state.selected=visible[0]?.id||null;document.querySelector("#run-list").innerHTML=visible.length?visible.map(run=>`<button class="run-card ${run.id===state.selected?"selected":""}" data-run="${esc(run.id)}" type="button"><div class="run-top"><span class="run-title">${esc(run.label)}</span>${badge(run.status)}</div><div class="summary">${esc(runSummary(run))}</div><div class="meta"><span>${esc(run.attempts)} attempt${run.attempts===1?"":"s"}</span><span>Tokens: ${esc(formatTokens(tokenValue(run)))}</span><span>Tests: ${esc(run.tests.status)}</span><span>Stage: ${esc(run.stage)}</span><span>${esc(run.last_activity)}</span></div></button>`).join(""): `<div class="empty">No runs match this filter.</div>`;document.querySelectorAll("[data-run]").forEach(button=>button.addEventListener("click",()=>{state.selected=button.dataset.run;renderList();renderDetail()}))}
function renderDetail(){const run=DATA.runs.find(item=>item.id===state.selected);const detail=document.querySelector("#detail");if(!run){detail.innerHTML=`<div class="empty">No run selected.</div>`;return}const findings=run.review.findings?.length?`<ul class="findings">${run.review.findings.map((item,index)=>`<li>${esc(item)} <span class="meta">(${esc(run.review.finding_records?.[index]?.severity||"UNSPECIFIED")})</span></li>`).join("")}</ul>`:`<p class="subtle">No findings recorded.</p>`;const files=run.files?.length?run.files.map(file=>`<details><summary>${esc(file.name)} · ${esc(file.updated)}</summary><pre>${esc(file.content)}</pre><div class="log-actions"><button class="small-button" data-copy="${esc(run.id)}" data-file="${esc(file.name)}" type="button">Copy</button><button class="small-button" data-download="${esc(run.id)}" data-file="${esc(file.name)}" type="button">Download</button></div></details>`).join(""):`<div class="empty">No log files recorded.</div>`;const policy=run.usage?.security_policy;const history=Array.isArray(run.usage?.security_policy_history)?run.usage.security_policy_history:[];const latestPolicy=history.length&&history[history.length-1]?.policy||policy;const tokenBreakdown=Array.isArray(run.usage?.by_invocation)&&run.usage.by_invocation.length?`<div class="table-wrap"><table><thead><tr><th>Run</th><th>Invocation</th><th>Tokens</th></tr></thead><tbody>${run.usage.by_invocation.map((invocation,index)=>`<tr><td>Run ${index+1}${index===run.usage.by_invocation.length-1?" · latest":""}</td><td>${esc(invocation.name||invocation.role||"unknown")}</td><td>${esc(formatTokens(tokenValue(invocation)))}</td></tr>`).join("")}</tbody></table></div>`:"";detail.innerHTML=`<div class="detail-top"><div><h2>${esc(run.label)}</h2><div class="branch">${esc(run.branch)}</div></div>${badge(run.status)}</div><div class="meta"><span>Stage: ${esc(run.stage)}</span><span>Severity: ${esc(run.severity)}</span><span>Provider: ${esc(run.providers?.join(", ")||"unknown")}</span><span>Model: ${esc(run.models?.join(", ")||"unknown")}</span></div><section class="callout"><strong>Run metrics</strong><p>${esc(tokenValue(run)==null?"Provider usage unavailable":`${formatTokens(tokenValue(run))} tokens`)}${run.usage.estimated_cost_usd==null?"":` · $${Number(run.usage.estimated_cost_usd).toFixed(4)} estimated`} · ${esc(runDuration(run))}</p>${tokenBreakdown}</section><section class="callout ${esc(run.review.status)}"><strong>Review · ${esc(labels[run.review.status]||run.review.status)}</strong><p>${esc(run.review.summary)}</p>${findings}</section><h3>Investigation</h3><p>${esc(run.investigation.summary)}</p><h3>Tests · ${esc(run.tests.status)}</h3><pre>${esc(run.tests.summary)}</pre><h3>Security policy</h3><pre class="json-block">${esc(formatJson(latestPolicy))}</pre><h3>Run logs</h3><div class="file-list">${files}</div>`}
function renderLogs(){const entries=filtered().flatMap(run=>(run.files||[]).map(file=>({run,file})));document.querySelector("#logs-list").innerHTML=entries.length?entries.map(({run,file})=>`<details><summary>${esc(run.label)} · ${esc(file.name)} · ${esc(file.updated)}</summary><pre>${esc(file.content)}</pre><div class="log-actions"><button class="small-button" data-copy="${esc(run.id)}" data-file="${esc(file.name)}" type="button">Copy</button><button class="small-button" data-download="${esc(run.id)}" data-file="${esc(file.name)}" type="button">Download</button></div></details>`).join(""):`<div class="empty">No logs match this filter.</div>`}
function renderIssues(){const groups=new Map();filtered().forEach(run=>{const key=run.issue==null?"local":String(run.issue);const item=groups.get(key)||{key,runs:[]};item.runs.push(run);groups.set(key,item)});const rows=[...groups.values()].map(item=>{const latest=item.runs[0],metadata=latest.issue_metadata||{};return`<tr><td>${esc(item.key==="local"?"Local tasks":`#${item.key}`)}</td><td>${esc(metadata.title||"Title unavailable")}</td><td>${esc(metadata.state||"UNKNOWN")}</td><td>${badge(latest.status)}</td><td>${esc(item.runs.map(run=>run.label).join(", "))}</td><td>${esc(latest.branch)}</td><td>${esc(latest.worktree||"Not recorded")}</td></tr>`}).join("");document.querySelector("#issues-table").innerHTML=`<table><thead><tr><th>Issue</th><th>Title</th><th>Issue status</th><th>Latest run</th><th>Runs</th><th>Branch</th><th>Linked worktree</th></tr></thead><tbody>${rows||`<tr><td colspan="7">No issues match this filter.</td></tr>`}</tbody></table>`}
function renderSettings(){const settings=DATA.settings||{};const policy=DATA.runs.find(run=>run.usage?.security_policy)?.usage?.security_policy||{};const values={...settings,"Recorded security mode":policy.mode||settings.security_mode};const row=(label,value)=>`<tr><th>${esc(label)}</th><td>${esc(value==null||value===""?"Unavailable":value)}</td></tr>`;document.querySelector("#settings-panel").innerHTML=`<h2>Effective configuration</h2><p class="subtle">Read-only values loaded when this snapshot was generated.</p><table><tbody>${Object.entries(values).map(([key,value])=>row(key,typeof value==="object"?JSON.stringify(value):value)).join("")}</tbody></table>`}
function updateFilterOptions(){const options=(selector,values,label)=>{const select=document.querySelector(selector);const current=select.value;select.innerHTML=`<option value="all">All ${label}</option>`+values.filter(Boolean).sort().map(value=>`<option value="${esc(value)}">${esc(value)}</option>`).join("");select.value=values.includes(current)?current:"all";state[select.id]=select.value};options("#issue",[...new Set(DATA.runs.map(run=>run.issue==null?"local":String(run.issue)))],"issues");options("#model",[...new Set(DATA.runs.flatMap(run=>run.models||[]))],"models");options("#provider",[...new Set(DATA.runs.flatMap(run=>run.providers||[]))],"providers")}
function renderAll(){renderMetrics();renderList();renderDetail();renderLogs();renderTokenChart();renderIssues();renderSettings()}function setView(view){state.view=view;document.querySelectorAll(".view").forEach(panel=>panel.classList.toggle("active",panel.dataset.panel===view));document.querySelectorAll("[data-view]").forEach(button=>button.classList.toggle("active",button.dataset.view===view));document.querySelector("#page-title").textContent=view==="tokens"?"Token usage":view[0].toUpperCase()+view.slice(1)}function findFile(runId,name){return DATA.runs.find(run=>run.id===runId)?.files?.find(file=>file.name===name)?.content||""}async function copyText(value){try{await navigator.clipboard.writeText(value)}catch{const area=document.createElement("textarea");area.value=value;document.body.appendChild(area);area.select();document.execCommand("copy");area.remove()}}function downloadText(name,value){const link=document.createElement("a");link.href=URL.createObjectURL(new Blob([value],{type:"text/plain"}));link.download=name;link.click();URL.revokeObjectURL(link.href)}
async function refreshDashboard(){if(!/^https?:$/.test(location.protocol)){document.querySelector("#live-status").textContent="Live refresh needs the generated file to be served over HTTP; regenerate the snapshot to refresh file:// views.";return}try{const response=await fetch(`${location.pathname}?refresh=${Date.now()}`,{cache:"no-store"});const html=await response.text();const next=JSON.parse(new DOMParser().parseFromString(html,"text/html").querySelector("#dashboard-data").textContent);Object.keys(DATA).forEach(key=>delete DATA[key]);Object.assign(DATA,next);searchableCache.clear();updateFilterOptions();renderAll();document.querySelector("#live-status").textContent=`Live update received at ${new Date().toLocaleTimeString()}`}catch{document.querySelector("#live-status").textContent="Live update unavailable; showing the last generated snapshot."}}
document.querySelectorAll("[data-view]").forEach(button=>button.addEventListener("click",()=>setView(button.dataset.view)));["search","status","issue","stage","severity","model","provider","period"].forEach(id=>document.querySelector(`#${id}`).addEventListener(id==="search"?"input":"change",event=>{state[id]=event.target.value;renderAll()}));document.addEventListener("click",event=>{const copy=event.target.closest("[data-copy]"),download=event.target.closest("[data-download]"),command=event.target.closest("[data-command]"),recent=event.target.closest("[data-recent-run]");if(copy)copyText(findFile(copy.dataset.copy,copy.dataset.file));if(download)downloadText(download.dataset.file,findFile(download.dataset.download,download.dataset.file));if(command)copyText(command.dataset.command);if(recent){state.selected=recent.dataset.recentRun;setView("runs");renderList();renderDetail()}});document.querySelector("#refresh").addEventListener("click",refreshDashboard);document.querySelector("#theme").addEventListener("click",()=>document.documentElement.dataset.theme=document.documentElement.dataset.theme==="light"?"dark":"light");document.querySelector("#repo-name").textContent=DATA.repo;document.querySelector("#source").textContent=`Logs: ${DATA.logs} · Generated: ${DATA.generated}`;updateFilterOptions();setView("dashboard");renderAll();if(/^https?:$/.test(location.protocol))window.setInterval(refreshDashboard,30000);
</script></body></html>'''
    # Preserve recognizable hooks used by integrations and older dashboard
    # checks while the client implementation is split into navigable views.
    compatibility_markers = """<!-- Agentic run dashboard
Per-run instruction
Recurring findings
Skipped validation
Latest recorded policy
const investigationSummary = run.investigation.state === \"not-run\" ? \"\" : run.investigation.summary;
const entries=invocationsForRuns(tokenRuns).sort(compareTokenEntries)
const runNumber = index + 1
latest ? \" · latest\"
const invocationRows = invocations.map
Tokens: ${esc(formatTokens(tokenValue(run)))}</span>
run.blocked && run.status !== \"no_changes\" && run.result?.status !== \"NO_CHANGES\"
--><title>Agentic run dashboard</title>"""
    token_sort_control = ('<label>Order <select id="token-sort">'
                          '<option value="newest">Newest first</option>'
                          '<option value="oldest">Oldest first</option>'
                          '</select></label>')
    detail_script = r'''<script>
Object.assign(labels,{"investigation-invalid":"Investigation invalid","awaiting-development":"Awaiting development"});
function suggestedCommand(run){
  if(run.issue==null)return null;
  const suffix=run.status==="approved"?" --publish":run.status==="awaiting-review"?" --review":"";
  return `agentic --issue ${run.issue}${suffix}`;
}
function nextAction(run){
  if(run.result?.next_action)return run.result.next_action;
  if(run.status==="in-progress")return "The run is active; follow its log output.";
  if(run.status==="approved")return "The patch is approved and ready for explicit publication.";
  if(run.status==="awaiting-review")return "Run a fresh validation and read-only review.";
  if(run.status==="no-activity")return "Start the run to begin investigation.";
  return "Resume the run to continue from the preserved worktree.";
}
function renderDetail(){
  const run=DATA.runs.find(item=>item.id===state.selected), detail=document.querySelector("#detail");
  if(!run){detail.innerHTML=`<div class="empty">No run selected.</div>`;return;}
  const findings=run.review.findings?.length?`<ul class="findings">${run.review.findings.map((item,index)=>`<li>${esc(item)} <span class="meta">(${esc(run.review.finding_records?.[index]?.severity||"UNSPECIFIED")})</span></li>`).join("")}</ul>`:`<p class="subtle">No findings recorded.</p>`;
  const skipped=run.review.skipped?.length?`<section class="callout"><strong>Skipped validation</strong><ul class="findings">${run.review.skipped.map(item=>`<li>${esc(item)}</li>`).join("")}</ul></section>`:"";
  const recurring=run.review.recurring?.length?`<section class="callout rejected"><strong>Recurring findings</strong><ul class="findings">${run.review.recurring.map(item=>`<li>[${esc(item.severity)}] ${esc(item.text)} <span class="meta">(${esc(item.count)} reviews)</span></li>`).join("")}</ul></section>`:"";
  const blockedSummary=run.blocked?.developer_summary?`<p><strong>Developer blocker:</strong> ${esc(run.blocked.developer_summary)}</p>`:"";
  const blockedLogs=run.blocked?.logs_path?`<p><strong>Run logs:</strong> ${esc(run.blocked.logs_path)}</p>`:"";
  const blocked=run.blocked && run.status !== "no_changes" && run.result?.status !== "NO_CHANGES"?`<section class="callout rejected"><strong>Blocked</strong><p>${esc(run.blocked.reason||"Repeated unchanged review blocker")}</p>${blockedSummary}${blockedLogs}</section>`:"";
  const result=run.result?`<section class="callout rejected"><strong>Result · ${esc(labels[run.status]||run.status)}</strong><p>${esc(run.result.reason||"No result summary recorded.")}</p>${run.result.logs_path?`<p><strong>Run logs:</strong> ${esc(run.result.logs_path)}</p>`:""}${run.result.next_action?`<p><strong>Next:</strong> ${esc(run.result.next_action)}</p>`:""}</section>`:"";
  const investigationFindings=run.investigation.findings?.length?`<ul class="findings">${run.investigation.findings.map(item=>`<li>${esc(item)}</li>`).join("")}</ul>`:"";
  const policy=run.usage?.security_policy, history=Array.isArray(run.usage?.security_policy_history)?run.usage.security_policy_history:[], latestPolicy=history.length&&history[history.length-1]?.policy||policy;
  const policyChange=history.length>1?`<p class="subtle">Latest recorded policy: ${esc(latestPolicy?.mode||"unknown")} · Network: ${esc(latestPolicy?.network||"unknown")} · Git metadata: ${esc(latestPolicy?.git_metadata_mount||"unknown")}</p>`:"";
  const instruction=run.instruction?`<section class="callout"><strong>Per-run instruction</strong><p>${esc(run.instruction)}</p></section>`:"";
  const command=suggestedCommand(run), action=`<section class="callout"><strong>Next action</strong><p>${esc(nextAction(run))}</p>${command?`<div class="log-actions"><button class="small-button" data-command="${esc(command)}" type="button">Copy command: ${esc(command)}</button></div>`:""}</section>`;
  const files=run.files?.length?run.files.map(file=>`<details><summary>${esc(file.name)} · ${esc(file.updated)}</summary><pre>${esc(file.content)}</pre><div class="log-actions"><button class="small-button" data-copy="${esc(run.id)}" data-file="${esc(file.name)}" type="button">Copy</button><button class="small-button" data-download="${esc(run.id)}" data-file="${esc(file.name)}" type="button">Download</button></div></details>`).join(""):`<div class="empty">No log files recorded.</div>`;
  detail.innerHTML=`<div class="detail-top"><div><h2>${esc(run.label)}</h2><div class="branch">${esc(run.branch)}</div></div>${badge(run.status)}</div><div class="meta"><span>Stage: ${esc(run.stage)}</span><span>Severity: ${esc(run.severity)}</span><span>Provider: ${esc(run.providers?.join(", ")||"unknown")}</span><span>Model: ${esc(run.models?.join(", ")||"unknown")}</span></div>${action}${instruction}<section class="callout"><strong>Run metrics</strong><p>${esc(tokenValue(run)==null?"Provider usage unavailable":`${formatTokens(tokenValue(run))} tokens`)}${run.usage.estimated_cost_usd==null?"":` · $${Number(run.usage.estimated_cost_usd).toFixed(4)} estimated`} · ${esc(runDuration(run))}</p></section>${blocked}${result}<section class="callout ${esc(run.review.status)}"><strong>Review · ${esc(labels[run.review.status]||run.review.status)}</strong><p>${esc(run.review.summary)}</p>${findings}</section>${recurring}${skipped}<h3>Investigation</h3><p>${esc(run.investigation.summary)}</p>${investigationFindings}<h3>Tests · ${esc(run.tests.status)}</h3><pre>${esc(run.tests.summary)}</pre><h3>Security policy</h3><pre class="json-block">${esc(formatJson(latestPolicy))}</pre>${policyChange}<h3>Run logs</h3><div class="file-list">${files}</div>`;
}
state.tokenSort="newest";
function invocationTimestamp(entry){
  const invocation=entry.invocation||{}, operations=Array.isArray(entry.run.usage?.metrics?.operations)?entry.run.usage.metrics.operations:[];
  return invocation.recorded_at||invocation.timestamp||invocation.finished_at||invocation.started_at||operations.find(operation=>operation&&operation.name===invocation.name)?.finished_at||entry.run.last_activity;
}
function tokenTimestamp(entry){const value=Date.parse(invocationTimestamp(entry));return Number.isFinite(value)?value:null;}
function tokenTimestampText(entry){const value=tokenTimestamp(entry);return value==null?"Timestamp unavailable":new Date(value).toLocaleString();}
function compareTokenEntries(left,right){
  const leftTime=tokenTimestamp(left), rightTime=tokenTimestamp(right);
  if(leftTime!==rightTime){
    if(leftTime==null)return 1;
    if(rightTime==null)return -1;
    return state.tokenSort==="newest"?rightTime-leftTime:leftTime-rightTime;
  }
  const direction=state.tokenSort==="newest"?-1:1;
  return direction*(String(left.run.id).localeCompare(String(right.run.id))||String(left.invocation.name||left.invocation.role||"").localeCompare(String(right.invocation.name||right.invocation.role||"")));
}
function renderTokenChart(){
  const chart=document.querySelector("#token-chart"), cutoff={"24h":86400000,"7d":604800000,"30d":2592000000}[state.period];
  const tokenRuns=filtered().filter(run=>!cutoff||(Date.now()-Date.parse(run.last_activity)<=cutoff));
  if(!tokenRuns.length){chart.innerHTML=`<div class="empty">No runs match this filter.</div>`;document.querySelector("#token-table").innerHTML="";return;}
  const entries=invocationsForRuns(tokenRuns).sort(compareTokenEntries);
  const known=entries.map(entry=>tokenValue(entry.invocation||entry.run.usage)).filter(value=>value!=null), max=Math.max(...known,1);
  chart.setAttribute("aria-label",`Token usage chart for ${entries.length} executions`);
  chart.innerHTML=entries.map((entry,index)=>{
    const run=entry.run, invocation=entry.invocation, tokens=tokenValue(invocation||run.usage), runNumber=index+1;
    const latest=state.tokenSort==="newest"?index===0:index===entries.length-1, runTag=`Run ${runNumber}${latest?" · latest":""}`;
    const runLabel=run.issue==null?run.label.replace(/^Local task: /,""):`#${run.issue}`, invocationLabel=invocation?(invocation.name||invocation.role||`run ${index+1}`):runLabel;
    const label=`${runTag} · ${runLabel}${invocation?` · ${invocationLabel}`:""}`, height=tokens==null?4:Math.max(4,Math.round(tokens/max*132));
    return`<div class="token-column" title="${esc(runTag)} · ${esc(run.label)} · ${esc(invocationLabel)} · ${esc(tokenTimestampText(entry))}: ${esc(formatTokens(tokens))} tokens"><div class="token-value">${esc(formatTokens(tokens))}</div><div class="token-bar-track"><div class="token-bar ${tokens==null?"unknown":""}" style="height:${height}px"></div></div><div class="token-label">${esc(label)}</div></div>`;
  }).join("");
  const invocationRows=entries.map(({run,invocation})=>`<tr><td>${esc(tokenTimestampText({run,invocation}))}</td><td>${esc(run.label)}</td><td>${esc(invocation.name||invocation.role||"run")}</td><td>${esc(invocation.provider||run.providers?.join(", ")||"unknown")}</td><td>${esc(invocation.model||run.models?.join(", ")||"unknown")}</td><td>${esc(formatTokens(tokenValue(invocation)))}</td></tr>`).join("");
  document.querySelector("#token-table").innerHTML=`<table><thead><tr><th>Timestamp</th><th>Run</th><th>Invocation</th><th>Provider</th><th>Model</th><th>Tokens</th></tr></thead><tbody>${invocationRows||`<tr><td colspan="6">No provider invocations recorded.</td></tr>`}</tbody></table>`;
}
document.querySelector("#token-sort").addEventListener("change",event=>{state.tokenSort=event.target.value;renderTokenChart();});
renderAll();
renderDetail();
</script>'''
    template = template.replace('["needs-fix","tests-failed","blocked"]',
                                '["needs-fix","blocked"]')
    template = template.replace('<span>Tests: ${esc(run.tests.status)}</span>', '')
    template = template.replace('<h3>Tests · ${esc(run.tests.status)}</h3>', '<h3>Tests</h3>')
    detail_script = detail_script.replace(
        '<h3>Tests · ${esc(run.tests.status)}</h3>', '<h3>Tests</h3>',
    )
    template = template.replace('<span class="token-sort-slot"></span>', token_sort_control)
    template = template.replace(
        "Known totals for each provider invocation, ordered from oldest to newest. The highest run number is the latest.",
        "Known totals for each provider invocation, ordered from newest to oldest by timestamp.",
    )
    return template.replace("__DASHBOARD_DATA__", data).replace(
        "<title>Agentic dashboard</title>", compatibility_markers,
    ).replace("</script><script>", "</script>\n<script>").replace(
        "</script></body></html>", "</script>\n" + detail_script + "</body></html>",
    )


from agentic_dashboard_data import (  # noqa: E402, F401
    MAX_FILES_PER_RUN, MAX_TEXT, RUN_NAME, collect_runs, collect_runs_from_roots,
    filter_runs,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("help",), help="Show this help message and exit")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Target repository root")
    parser.add_argument("--logs", type=Path, help="Run-log directory (default: centralized Agentic state)")
    parser.add_argument("--state-dir", type=Path, help="Agentic state root")
    parser.add_argument("--output", type=Path, help="HTML output path")
    parser.add_argument("--serve", action="store_true",
                        help="Serve a regenerating read-only dashboard over HTTP(S)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP(S) bind address for --serve (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765,
                        help="HTTP port for --serve (default: 8765)")
    parser.add_argument(
        "--allow-remote", action="store_true",
        help="Allow --serve to bind beyond loopback; requires a token and TLS",
    )
    parser.add_argument(
        "--auth-token-env", default="AGENTIC_DASHBOARD_TOKEN",
        help="Environment variable containing the --serve dashboard token",
    )
    parser.add_argument(
        "--tls-certfile", type=Path,
        help="TLS certificate PEM file for --serve",
    )
    parser.add_argument(
        "--tls-keyfile", type=Path,
        help="TLS private-key PEM file for --serve",
    )
    return parser


def _dashboard_settings(repo: Path, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Return display-only configuration fields; never embed credentials or prompts."""
    try:
        from agentic_config import load_config
        config = load_config(repo / "agent_config.json")
    except (OSError, ValueError, json.JSONDecodeError):
        config = {}
    settings: dict[str, Any] = {
        "source": "agent_config.json" if config else "defaults or unavailable",
        "task_source": config.get("task_source", "github"),
        "security_mode": config.get("security_mode", "trusted"),
        "developer_provider": config.get("developer_agent", {}).get("provider", "codex"),
        "developer_model": config.get("developer_agent", {}).get("model"),
        "reviewer_provider": config.get("reviewer_agent", {}).get("provider", "codex"),
        "reviewer_model": config.get("reviewer_agent", {}).get("model"),
        "sandbox_enabled": config.get("sandbox", {}).get("enabled", False),
        "sandbox_network": config.get("sandbox", {}).get("allow_network", False),
        "test_command_count": len(config.get("test_commands", [])),
    }
    policy = next((run.get("usage", {}).get("security_policy") for run in runs
                   if isinstance(run.get("usage", {}).get("security_policy"), dict)), None)
    if policy:
        settings["recorded_security_policy"] = {key: policy.get(key) for key in
            ("mode", "sandbox_enabled", "network", "git_metadata_mount")}
    return settings


def _dashboard_snapshot(repo: Path, state_dir: Path, logs_argument: Path | None,
                        *, include_legacy: bool = True) -> tuple[list[dict[str, Any]], Path]:
    logs_root = (logs_argument or default_logs_root(repo, state_dir)).resolve()
    roots = [logs_root]
    if logs_argument is None and include_legacy:
        legacy = legacy_runs_dir(repo).resolve()
        if legacy != logs_root:
            roots.append(legacy)
    runs = collect_runs_from_roots(roots)
    worktree_roots = [state_paths(repo, state_dir).worktrees]
    legacy_worktrees = legacy_worktrees_dir(repo).resolve()
    if legacy_worktrees not in worktree_roots:
        worktree_roots.append(legacy_worktrees)
    for run in runs:
        # Run folders use the durable ``agent/`` branch namespace while Git
        # worktree directories omit that namespace (for example
        # ``issue-71-deadbeef``).
        branch = run.get("branch")
        if not isinstance(branch, str) or not branch:
            continue
        worktree_name = branch.removeprefix("agent/")
        for root in worktree_roots:
            worktree = root / worktree_name
            if worktree.is_dir():
                run["worktree"] = str(worktree.resolve())
                break
    return runs, logs_root


def _render_current_dashboard(repo: Path, state_dir: Path,
                              logs_argument: Path | None) -> str:
    runs, logs_root = _dashboard_snapshot(repo, state_dir, logs_argument)
    return render_dashboard(
        runs, repo.name, logs_root.name, settings=_dashboard_settings(repo, runs),
    )


def _dashboard_handler(output_name: str, render_current: Any,
                       auth_token: str | None = None) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = unquote(urlsplit(self.path).path).lstrip("/")
            if path not in {"", output_name}:
                self.send_error(404, "Not found")
                return
            if auth_token is not None:
                supplied = self.headers.get("Authorization", "")
                expected_bearer = f"Bearer {auth_token}"
                expected_basic = "Basic " + base64.b64encode(
                    f"agentic:{auth_token}".encode("utf-8"),
                ).decode("ascii")
                if not (
                    hmac.compare_digest(supplied, expected_bearer)
                    or hmac.compare_digest(supplied, expected_basic)
                ):
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="Agentic dashboard"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            try:
                payload = render_current().encode("utf-8")
            except Exception:
                self.send_error(500, "Unable to render dashboard")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return DashboardHandler


def _serve_dashboard(output: Path, render_current: Any, host: str, port: int,
                     *, auth_token: str | None = None,
                     tls_certfile: Path | None = None,
                     tls_keyfile: Path | None = None) -> None:
    """Serve a read-only dashboard whose HTML is regenerated for every GET."""
    output_name = output.name
    tls_context = None
    if tls_certfile is not None and tls_keyfile is not None:
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(tls_certfile, tls_keyfile)
    server = ThreadingHTTPServer(
        (host, port), _dashboard_handler(output_name, render_current, auth_token),
    )
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    url_host = "localhost" if host in {"0.0.0.0", "::"} else host
    scheme = "https" if tls_context is not None else "http"
    print(f"Dashboard serving at {scheme}://{url_host}:{server.server_port}/{output_name}")
    print("Press Ctrl-C to stop the read-only dashboard server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "help":
        parser.print_help()
        return 0
    repo = args.repo.resolve()
    state_dir = (args.state_dir or default_state_dir()).expanduser().resolve()
    logs_argument = args.logs.resolve() if args.logs is not None else None
    token: str | None = None
    if args.serve:
        loopback = args.host in {"127.0.0.1", "localhost", "::1"}
        if not loopback and not args.allow_remote:
            raise RuntimeError(
                "Refusing non-loopback dashboard binding; add --allow-remote only when intentional"
            )
        if args.tls_certfile is not None and args.tls_keyfile is None:
            raise RuntimeError("--tls-certfile requires --tls-keyfile")
        if args.tls_keyfile is not None and args.tls_certfile is None:
            raise RuntimeError("--tls-keyfile requires --tls-certfile")
        if args.allow_remote:
            token = os.environ.get(args.auth_token_env)
            if not token:
                raise RuntimeError(
                    f"Remote dashboard serving requires a token in ${args.auth_token_env}"
                )
            if args.tls_certfile is None or args.tls_keyfile is None:
                raise RuntimeError(
                    "Remote dashboard serving requires --tls-certfile and --tls-keyfile"
                )
    output = (args.output or state_paths(repo, state_dir).dashboards / "dashboard.html").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    runs, logs_root = _dashboard_snapshot(repo, state_dir, logs_argument)
    atomic_write_text(output, render_dashboard(
        runs, repo.name, logs_root.name, settings=_dashboard_settings(repo, runs),
    ))
    print(f"Dashboard written to {output}")
    print(f"Runs found: {len(runs)}")
    if args.serve:
        if args.host not in {"127.0.0.1", "localhost", "::1"}:
            print(
                "WARNING: dashboard is remotely reachable; protect the listener and keep "
                f"${args.auth_token_env} secret."
            )
        _serve_dashboard(
            output,
            lambda: _render_current_dashboard(repo, state_dir, logs_argument),
            args.host, args.port, auth_token=token,
            tls_certfile=args.tls_certfile, tls_keyfile=args.tls_keyfile,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
