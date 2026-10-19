#!/usr/bin/env python3
"""Build a self-contained HTML dashboard from Agentic run logs.

The dashboard is intentionally read-only: it only scans the run-log directory
and embeds a bounded amount of log text into the generated HTML file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from agentic import default_state_dir, legacy_runs_dir, state_paths


def default_logs_root(repo: Path, state_dir: Path | None = None) -> Path:
    return state_paths(repo, state_dir).runs


def _json_for_script(value: Any) -> str:
    # Prevent log content containing </script> from terminating the data block.
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026"))


def render_dashboard(runs: list[dict[str, Any]], repo_name: str, logs_name: str) -> str:
    generated = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    data = _json_for_script({"runs": runs, "repo": repo_name, "logs": logs_name, "generated": generated})
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agentic run dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b1020; --surface: #121a2d; --surface-2: #18233b; --text: #edf3ff;
      --muted: #9aa9c4; --line: #2a3855; --blue: #6ea8fe; --green: #55d187;
      --yellow: #f2c86b; --red: #ff7d8b; --purple: #be9cff; --shadow: 0 18px 45px #0005;
    }}
    [data-theme="light"] {{
      color-scheme: light; --bg: #f4f7fb; --surface: #fff; --surface-2: #eef3fb;
      --text: #17233b; --muted: #5b6a83; --line: #d8e0ee; --blue: #2869d8;
      --green: #16834b; --yellow: #996900; --red: #bf3045; --purple: #7041b3; --shadow: 0 18px 45px #31466a1c;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: radial-gradient(circle at 12% 0%, #25376255, transparent 35%), var(--bg); color: var(--text); font: 15px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; }}
    button, input, select {{ font: inherit; }}
    button, select {{ cursor: pointer; }}
    .shell {{ max-width: 1380px; margin: 0 auto; padding: 28px; }}
    header {{ display: flex; justify-content: space-between; gap: 20px; align-items: flex-start; margin-bottom: 24px; }}
    h1 {{ margin: 0 0 4px; font-size: clamp(26px, 4vw, 38px); letter-spacing: -.03em; }}
    h2, h3 {{ margin: 0; }}
    .subtle, .meta {{ color: var(--muted); }}
    .subtle {{ margin: 0; }}
    .actions {{ display: flex; gap: 8px; }}
    .button, input, select {{ border: 1px solid var(--line); border-radius: 10px; background: var(--surface); color: var(--text); padding: 9px 12px; }}
    .button:hover, select:hover, input:focus {{ border-color: var(--blue); }}
    .button {{ white-space: nowrap; }}
    .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 12px; margin-bottom: 20px; }}
    .metric, .panel {{ background: color-mix(in srgb, var(--surface) 94%, transparent); border: 1px solid var(--line); border-radius: 16px; box-shadow: var(--shadow); }}
    .metric {{ padding: 16px 18px; }}
    .metric strong {{ display: block; font-size: 30px; line-height: 1; margin-top: 5px; font-variant-numeric: tabular-nums; }}
    .metric .label {{ color: var(--muted); font-size: 13px; }}
    .workspace {{ display: grid; grid-template-columns: minmax(300px, .85fr) minmax(0, 1.6fr); gap: 18px; align-items: start; }}
    .panel {{ overflow: hidden; }}
    .toolbar {{ display: flex; gap: 8px; padding: 14px; border-bottom: 1px solid var(--line); }}
    .toolbar input {{ min-width: 0; flex: 1; }}
    .run-list {{ max-height: 780px; overflow: auto; }}
    .run-card {{ display: block; width: 100%; text-align: left; padding: 16px; border: 0; border-bottom: 1px solid var(--line); background: transparent; color: inherit; }}
    .run-card:hover, .run-card.selected {{ background: var(--surface-2); }}
    .run-card:last-child {{ border-bottom: 0; }}
    .run-top, .detail-top, .row {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; }}
    .run-title {{ font-weight: 700; }}
    .badge {{ display: inline-flex; align-items: center; gap: 6px; border-radius: 999px; padding: 3px 9px; font-size: 12px; font-weight: 700; white-space: nowrap; }}
    .badge::before {{ content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }}
    .approved {{ color: var(--green); background: color-mix(in srgb, var(--green) 15%, transparent); }}
    .needs-fix, .tests-failed, .investigation-invalid, .blocked {{ color: var(--red); background: color-mix(in srgb, var(--red) 15%, transparent); }}
    .awaiting-development {{ color: var(--yellow); background: color-mix(in srgb, var(--yellow) 15%, transparent); }}
    .in-progress {{ color: var(--blue); background: color-mix(in srgb, var(--blue) 15%, transparent); }}
    .awaiting-review {{ color: var(--yellow); background: color-mix(in srgb, var(--yellow) 15%, transparent); }}
    .no-activity {{ color: var(--muted); background: color-mix(in srgb, var(--muted) 15%, transparent); }}
    .summary {{ margin: 10px 0 8px; color: var(--muted); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 8px 14px; font-size: 12px; }}
    .detail {{ padding: 22px; min-height: 420px; }}
    .chart-panel {{ padding: 20px; margin-bottom: 18px; }}
    .panel-heading {{ display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; margin-bottom: 18px; }}
    .panel-heading h2 {{ font-size: 18px; }}
    .token-chart {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(74px, 1fr)); gap: 10px; align-items: end; min-height: 190px; overflow-x: auto; padding: 12px 4px 0; border-bottom: 1px solid var(--line); }}
    .token-column {{ min-width: 0; display: grid; grid-template-rows: 22px 132px 34px; gap: 4px; align-items: end; text-align: center; }}
    .token-value {{ color: var(--text); font-size: 11px; font-variant-numeric: tabular-nums; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .token-bar-track {{ height: 132px; display: flex; align-items: end; justify-content: center; }}
    .token-bar {{ width: min(42px, 72%); min-height: 4px; border-radius: 7px 7px 0 0; background: linear-gradient(180deg, var(--purple), var(--blue)); box-shadow: 0 5px 18px color-mix(in srgb, var(--blue) 25%, transparent); }}
    .token-bar.unknown {{ height: 4px; background: repeating-linear-gradient(135deg, var(--line) 0 5px, transparent 5px 10px); box-shadow: none; }}
    .token-label {{ color: var(--muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .token-legend {{ color: var(--muted); font-size: 12px; white-space: nowrap; }}
    .token-breakdown {{ margin-top: 10px; }}
    .token-breakdown table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    .token-breakdown th, .token-breakdown td {{ padding: 6px 8px; border-bottom: 1px solid var(--line); text-align: left; }}
    .token-breakdown th {{ color: var(--muted); font-weight: 600; }}
    .detail h2 {{ font-size: 25px; }}
    .detail h3 {{ margin: 24px 0 9px; font-size: 15px; }}
    .detail .branch {{ color: var(--muted); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }}
    .callout {{ margin-top: 18px; padding: 14px 16px; border-left: 3px solid var(--blue); background: var(--surface-2); border-radius: 8px; }}
    .callout.rejected {{ border-color: var(--red); }}
    .callout.approved {{ border-color: var(--green); }}
    .findings {{ margin: 10px 0 0; padding-left: 20px; }}
    .findings li + li {{ margin-top: 7px; }}
    .file-list {{ display: grid; gap: 8px; }}
    details {{ border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }}
    summary {{ padding: 10px 12px; cursor: pointer; background: var(--surface-2); }}
    pre {{ margin: 0; padding: 13px; max-height: 300px; overflow: auto; background: var(--bg); color: var(--muted); white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; }}
    .empty {{ padding: 38px 20px; text-align: center; color: var(--muted); }}
    .sr-only {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}
    @media (max-width: 880px) {{ .workspace {{ grid-template-columns: 1fr; }} .run-list {{ max-height: none; }} }}
    @media (max-width: 620px) {{ .shell {{ padding: 18px 12px; }} header {{ display: block; }} .actions {{ margin-top: 14px; }} .toolbar {{ flex-wrap: wrap; }} .toolbar input {{ flex-basis: 100%; }} .panel-heading {{ display: block; }} .token-legend {{ display: block; margin-top: 5px; }} }}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div>
        <h1>Agentic run dashboard</h1>
        <p class="subtle" id="source"></p>
      </div>
      <div class="actions"><button class="button" id="theme" type="button">Toggle theme</button></div>
    </header>
    <section class="metrics" aria-label="Run summary" id="metrics"></section>
    <section class="panel chart-panel" aria-labelledby="token-chart-title">
      <div class="panel-heading">
        <div><h2 id="token-chart-title">Token usage by run</h2><p class="subtle">Known totals for each provider invocation, ordered from oldest to newest. The highest run number is the latest.</p></div>
        <span class="token-legend">Striped bars = usage unavailable</span>
      </div>
      <div class="token-chart" id="token-chart" role="img" aria-label="Token usage chart"></div>
    </section>
    <section class="workspace">
      <div class="panel">
        <div class="toolbar">
          <label class="sr-only" for="search">Search runs</label>
          <input id="search" type="search" placeholder="Search issue, task, or branch…">
          <label class="sr-only" for="status">Filter status</label>
          <select id="status"><option value="all">All statuses</option><option value="blocked">Blocked</option><option value="needs-fix">Needs fix</option><option value="investigation-invalid">Investigation invalid</option><option value="awaiting-development">Awaiting development</option><option value="approved">Approved</option><option value="in-progress">In progress</option><option value="tests-failed">Tests failed</option><option value="awaiting-review">Awaiting review</option></select>
        </div>
        <div class="run-list" id="run-list" aria-live="polite"></div>
      </div>
      <article class="panel detail" id="detail" aria-live="polite"></article>
    </section>
  </main>
  <script>
    const DATA = {data};
    const state = {{ selected: DATA.runs[0]?.id || null, query: "", status: "all" }};
    const labels = {{
      "approved": "Approved", "needs-fix": "Needs fix", "in-progress": "In progress",
      "tests-failed": "Tests failed", "awaiting-review": "Awaiting review", "no-activity": "No activity", "blocked": "Blocked",
      "investigation-invalid": "Investigation invalid", "awaiting-development": "Awaiting development"
    }};
    const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[char]));
    const badge = status => `<span class="badge ${{esc(status)}}">${{esc(labels[status] || status)}}</span>`;
    const tokenValue = entry => {{
      const value = Number(entry?.total_tokens ?? entry?.usage?.total_tokens);
      return Number.isFinite(value) && value >= 0 ? value : null;
    }};
    const formatTokens = value => value == null ? "unknown" : Number(value).toLocaleString();
    const formatDuration = seconds => {{
      if (seconds == null || !Number.isFinite(Number(seconds))) return "duration unavailable";
      const value = Number(seconds);
      if (value < 60) return `${{value.toFixed(1)}}s`;
      const minutes = Math.floor(value / 60);
      const remainder = Math.round(value % 60);
      return `${{minutes}}m ${{remainder}}s`;
    }};
    const runDuration = run => formatDuration(run.usage?.metrics?.duration_seconds);
    const filtered = () => DATA.runs.filter(run =>
      (state.status === "all" || run.status === state.status) &&
      (`${{run.label}} ${{run.branch}}`.toLowerCase().includes(state.query.toLowerCase()))
    );
    const runSummary = run => {{
      const investigationSummary = run.investigation.state === "not-run" ? "" : run.investigation.summary;
      return investigationSummary || run.review.summary || run.developer.summary || "No summary recorded.";
    }};
    function renderMetrics() {{
      const runs = DATA.runs;
      const observedTokens = runs.reduce((total, run) => total + (tokenValue(run) ?? 0), 0);
      const tokenRuns = runs.filter(run => tokenValue(run) != null).length;
      const values = [
        ["Total runs", runs.length, ""],
        ["Approved", runs.filter(run => run.status === "approved").length, "approved"],
        ["Needs attention", runs.filter(run => ["needs-fix", "tests-failed"].includes(run.status)).length, "needs-fix"],
        ["In progress", runs.filter(run => run.status === "in-progress").length, "in-progress"],
        ["Tokens observed", tokenRuns ? formatTokens(observedTokens) : "unknown", ""]
      ];
      document.querySelector("#metrics").innerHTML = values.map(([label, value, kind]) => `<div class="metric"><span class="label">${{esc(label)}}</span><strong class="${{kind}}">${{esc(value)}}</strong></div>`).join("");
    }}
    function renderTokenChart() {{
      const chart = document.querySelector("#token-chart");
      if (!DATA.runs.length) {{ chart.innerHTML = `<div class="empty">No runs recorded.</div>`; return; }}
      const entries = [...DATA.runs].reverse().flatMap(run => {{
        const invocations = Array.isArray(run.usage?.by_invocation)
          ? run.usage.by_invocation.filter(item => item && typeof item === "object") : [];
        if (!invocations.length) return [{{ run, invocation: null }}];
        return invocations.map((invocation, index) => ({{ run, invocation, index }}));
      }});
      const known = entries.map(entry => tokenValue(entry.invocation || entry.run.usage)).filter(value => value != null);
      const max = Math.max(...known, 1);
      chart.setAttribute("aria-label", `Token usage chart for ${{entries.length}} executions`);
      chart.innerHTML = entries.map((entry, index) => {{
        const run = entry.run;
        const invocation = entry.invocation;
        const tokens = tokenValue(invocation || run.usage);
        const runNumber = index + 1;
        const latest = index === entries.length - 1;
        const runTag = `Run ${{runNumber}}${{latest ? " · latest" : ""}}`;
        const runLabel = run.issue == null ? run.label.replace(/^Local task: /, "") : `#${{run.issue}}`;
        const invocationLabel = invocation ? (invocation.name || invocation.role || `run ${{entry.index + 1}}`) : runLabel;
        const label = `${{runTag}} · ${{runLabel}}${{invocation ? ` · ${{invocationLabel}}` : ""}}`;
        const height = tokens == null ? 4 : Math.max(4, Math.round(tokens / max * 132));
        return `<div class="token-column" title="${{esc(runTag)}} · ${{esc(run.label)}} · ${{esc(invocationLabel)}}: ${{esc(formatTokens(tokens))}} tokens">
          <div class="token-value">${{esc(formatTokens(tokens))}}</div>
          <div class="token-bar-track"><div class="token-bar ${{tokens == null ? "unknown" : ""}}" style="height:${{height}}px"></div></div>
          <div class="token-label">${{esc(label)}}</div>
        </div>`;
      }}).join("");
    }}
    function renderList() {{
      const visible = filtered();
      if (!visible.some(run => run.id === state.selected)) state.selected = visible[0]?.id || null;
      document.querySelector("#run-list").innerHTML = visible.length ? visible.map(run => `<button class="run-card ${{run.id === state.selected ? "selected" : ""}}" data-run="${{esc(run.id)}}" type="button">
        <div class="run-top"><span class="run-title">${{esc(run.label)}}</span>${{badge(run.status)}}</div>
        <div class="summary">${{esc(runSummary(run))}}</div>
        <div class="meta"><span>${{esc(run.attempts)}} attempt${{run.attempts === 1 ? "" : "s"}}</span><span>Tokens: ${{esc(formatTokens(tokenValue(run)))}}</span><span>Tests: ${{esc(run.tests.status)}}</span><span>${{esc(runDuration(run))}}</span><span>${{esc(run.last_activity)}}</span></div>
      </button>`).join("") : `<div class="empty">No runs match this filter.</div>`;
      document.querySelectorAll("[data-run]").forEach(button => button.addEventListener("click", () => {{ state.selected = button.dataset.run; renderList(); renderDetail(); }}));
    }}
    function renderDetail() {{
      const run = DATA.runs.find(item => item.id === state.selected);
      const detail = document.querySelector("#detail");
      if (!run) {{ detail.innerHTML = `<div class="empty">No run selected.</div>`; return; }}
      const findings = run.review.findings?.length ? `<ul class="findings">${{run.review.findings.map(item => `<li>${{esc(item)}}</li>`).join("")}}</ul>` : `<p class="subtle">No findings recorded.</p>`;
      const skipped = run.review.skipped?.length ? `<section class="callout"><strong>Skipped validation</strong><ul class="findings">${{run.review.skipped.map(item => `<li>${{esc(item)}}</li>`).join("")}}</ul></section>` : "";
      const blockedSummary = run.blocked?.developer_summary ? `<p><strong>Developer blocker:</strong> ${{esc(run.blocked.developer_summary)}}</p>` : "";
      const blockedLogs = run.blocked?.logs_path ? `<p><strong>Run logs:</strong> ${{esc(run.blocked.logs_path)}}</p>` : "";
      const blocked = run.blocked ? `<section class="callout rejected"><strong>Blocked</strong><p>${{esc(run.blocked.reason || "Repeated unchanged review blocker")}}</p>${{blockedSummary}}${{blockedLogs}}</section>` : "";
      const investigationFindings = run.investigation.findings?.length ? `<ul class="findings">${{run.investigation.findings.map(item => `<li>${{esc(item)}}</li>`).join("")}}</ul>` : `<p class="subtle">No findings recorded.</p>`;
      const recurring = run.review.recurring?.length ? `<section class="callout rejected"><strong>Recurring findings</strong><ul class="findings">${{run.review.recurring.map(item => `<li>[${{esc(item.severity)}}] ${{esc(item.text)}} <span class="meta">(${{esc(item.count)}} reviews)</span></li>`).join("")}}</ul></section>` : "";
      const policy = run.usage?.security_policy;
      const policyHistory = Array.isArray(run.usage?.security_policy_history) ? run.usage.security_policy_history : [];
      const latestPolicy = policyHistory.length && policyHistory[policyHistory.length - 1]?.policy || policy;
      const policyChange = latestPolicy && policyHistory.length > 1 ? `<p class="subtle">Latest recorded policy: ${{esc(latestPolicy.mode || "unknown")}} · Network: ${{esc(latestPolicy.network || "unknown")}} · Git metadata: ${{esc(latestPolicy.git_metadata_mount || "unknown")}}</p>` : "";
      const securityPolicy = policy ? `<section class="callout"><strong>Security policy</strong><p>Initial mode: ${{esc(policy.mode || "unknown")}} · Network: ${{esc(policy.network || "unknown")}} · Git metadata: ${{esc(policy.git_metadata_mount || "unknown")}}</p>${{policyChange}}</section>` : "";
      const instruction = run.instruction ? `<section class="callout"><strong>Per-run instruction</strong><pre>${{esc(run.instruction)}}</pre></section>` : "";
      const invocations = Array.isArray(run.usage?.by_invocation) ? run.usage.by_invocation.filter(item => item && typeof item === "object") : [];
      const invocationRows = invocations.map((item, index) => {{
        const runLabel = `Run ${{index + 1}}${{index === invocations.length - 1 ? " · latest" : ""}}`;
        return `<tr><td>${{esc(runLabel)}}</td><td>${{esc(item.name || "unknown")}}</td><td>${{esc(item.role || "unknown")}}</td><td>${{esc(formatTokens(item.prompt_tokens))}}</td><td>${{esc(formatTokens(item.completion_tokens))}}</td><td>${{esc(formatTokens(item.total_tokens))}}</td></tr>`;
      }}).join("");
      const tokenBreakdown = invocations.length ? `<div class="token-breakdown"><table><thead><tr><th>Run</th><th>Invocation</th><th>Role</th><th>Prompt</th><th>Completion</th><th>Total</th></tr></thead><tbody>${{invocationRows}}</tbody></table></div>` : "";
      const files = run.files.length ? run.files.map(file => `<details><summary>${{esc(file.kind)}} · ${{esc(file.name)}} <span class="meta">${{esc(file.updated)}}</span></summary><pre>${{esc(file.content)}}</pre></details>`).join("") : `<div class="empty">No log files recorded.</div>`;
      detail.innerHTML = `<div class="detail-top"><div><h2>${{esc(run.label)}}</h2><div class="branch">${{esc(run.branch)}}</div></div>${{badge(run.status)}}</div>
        ${{blocked}}
        <div class="meta" style="margin-top:12px"><span>${{esc(run.attempts)}} developer attempt${{run.attempts === 1 ? "" : "s"}}</span><span>Last activity: ${{esc(run.last_activity)}}</span><span>${{esc(run.file_count)}} log files</span></div>
        <section class="callout"><strong>Run metrics</strong><p>${{esc(tokenValue(run) == null ? "Provider usage unavailable" : `${{formatTokens(tokenValue(run))}} tokens`)}}${{run.usage.estimated_cost_usd == null ? "" : ` · $${{Number(run.usage.estimated_cost_usd).toFixed(4)}} estimated`}} · ${{esc(runDuration(run))}} · ${{esc(run.usage?.metrics?.operation_count || 0)}} operations</p>${{tokenBreakdown}}</section>
        <section class="callout"><strong>Investigation feedback</strong><p>${{esc(run.investigation.summary)}}</p>${{investigationFindings}}</section>
        <section class="callout ${{esc(run.review.status)}}"><strong>Review · ${{esc(labels[run.review.status] || run.review.status)}}</strong><p>${{esc(run.review.summary)}}</p>${{findings}}</section>
        ${{skipped}}
        ${{recurring}}
        ${{instruction}}
        ${{securityPolicy}}
        <h3>Tests · ${{esc(run.tests.status)}}</h3><pre>${{esc(run.tests.summary)}}</pre>
        <h3>Latest developer result</h3><pre>${{esc(run.developer.summary)}}</pre>
        <h3>Run logs</h3><div class="file-list">${{files}}</div>`;
    }}
    document.querySelector("#source").textContent = `${{DATA.repo}} · generated ${{DATA.generated}} · ${{DATA.runs.length}} run${{DATA.runs.length === 1 ? "" : "s"}}`;
    document.querySelector("#search").addEventListener("input", event => {{ state.query = event.target.value; renderList(); renderDetail(); }});
    document.querySelector("#status").addEventListener("change", event => {{ state.status = event.target.value; renderList(); renderDetail(); }});
    document.querySelector("#theme").addEventListener("click", () => {{ document.documentElement.dataset.theme = document.documentElement.dataset.theme === "light" ? "dark" : "light"; }});
    renderMetrics(); renderTokenChart(); renderList(); renderDetail();
  </script>
</body>
</html>
'''


# Keep the historical dashboard names importable while the log-schema parser
# lives in its own module. The renderer above intentionally remains independent
# of the parser so either side can evolve without changing the CLI contract.
from agentic_dashboard_data import (  # noqa: E402, F401
    MAX_FILES_PER_RUN,
    MAX_TEXT,
    NUMBERED_FILE,
    RUN_NAME,
    _all_log_files,
    _blocked,
    _file_record,
    _investigation,
    _latest,
    _latest_developer,
    _numbered_files,
    _parse_json_object,
    _process_running,
    _read,
    _review,
    _run_snapshot,
    _tests,
    _timestamp,
    collect_runs,
    collect_runs_from_roots,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("help",), help="Show this help message and exit")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Target repository root")
    parser.add_argument("--logs", type=Path, help="Run-log directory (default: centralized Agentic state)")
    parser.add_argument("--state-dir", type=Path, help="Agentic state root (shared /mnt path recommended for WSL/Windows)")
    parser.add_argument("--output", type=Path, help="HTML output path (default: centralized Agentic state)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "help":
        parser.print_help()
        return 0
    repo = args.repo.resolve()
    state_dir = (args.state_dir or default_state_dir()).expanduser().resolve()
    logs_root = (args.logs or default_logs_root(repo, state_dir)).resolve()
    roots = [logs_root]
    if args.logs is None:
        legacy = legacy_runs_dir(repo).resolve()
        if legacy != logs_root:
            roots.append(legacy)
    output = (args.output or state_paths(repo, state_dir).dashboards / "dashboard.html").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    runs = collect_runs_from_roots(roots)
    output.write_text(render_dashboard(runs, repo.name, logs_root.name), encoding="utf-8")
    print(f"Dashboard written to {output}")
    print(f"Runs found: {len(runs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
