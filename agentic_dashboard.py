#!/usr/bin/env python3
"""Build a self-contained HTML dashboard from Agentic run logs.

The dashboard is intentionally read-only: it only scans the run-log directory
and embeds a bounded amount of log text into the generated HTML file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Iterable


RUN_NAME = re.compile(r"^agent-issue-(?P<issue>\d+)-(?P<token>[A-Za-z0-9_-]+)$")
NUMBERED_FILE = re.compile(r"^(?P<prefix>.+)-(?P<number>\d+)(?P<suffix>\.[^.]+)$")
MAX_TEXT = 5_000
MAX_FILES_PER_RUN = 24


def default_logs_root(repo: Path) -> Path:
    return repo.parent / f".{repo.name}-agent-runs"


def _numbered_files(folder: Path, prefixes: Iterable[str]) -> list[Path]:
    wanted = set(prefixes)
    files: list[tuple[int, float, Path]] = []
    if not folder.is_dir():
        return []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        match = NUMBERED_FILE.fullmatch(path.name)
        if not match or match.group("prefix") not in wanted:
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0
        files.append((int(match.group("number")), modified, path))
    return [path for _, _, path in sorted(files, key=lambda item: (item[0], item[1], item[2].name))]


def _latest(paths: Iterable[Path]) -> Path | None:
    candidates = list(paths)
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.name))


def _read(path: Path, limit: int = MAX_TEXT) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read {path.name}: {exc}"
    if len(text) > limit:
        return f"[… earlier output omitted; showing the last {limit:,} characters …]\n" + text[-limit:]
    return text


def _timestamp(path: Path) -> str:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return "unknown"


def _file_record(path: Path, kind: str) -> dict[str, str]:
    return {
        "name": path.name,
        "kind": kind,
        "updated": _timestamp(path),
        "content": _read(path),
    }


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Parse strict JSON or a JSON object wrapped in prose/code fences."""
    candidates = [text.strip()]
    candidates.extend(re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE))
    start = text.find("{")
    if start >= 0:
        candidates.append(text[start:])
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            try:
                value, _ = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                continue
        if isinstance(value, dict):
            return value
    return None


def _review(folder: Path) -> tuple[dict[str, Any], Path | None]:
    # A review has three related files: the structured .json result, a .log
    # progress trace, and a .jsonl Codex event trace. Only the first is the
    # review document.
    path = _latest(path for path in _numbered_files(folder, ["review"])
                   if path.suffix == ".json")
    if path is None:
        return {"status": "not-run", "summary": "No review result recorded.", "findings": []}, None
    raw = _parse_json_object(_read(path, 100_000))
    if raw is None:
        try:
            json.loads(_read(path, 100_000))
        except json.JSONDecodeError as exc:
            error = str(exc)
        else:
            error = "Review output is not a JSON object."
        return {
            "status": "invalid",
            "summary": f"Review output is not valid JSON: {error}",
            "findings": [],
        }, path
    approved = raw.get("approved") is True
    findings = raw.get("findings", [])
    if not isinstance(findings, list):
        findings = [str(findings)]
    return {
        "status": "approved" if approved else "rejected",
        "summary": str(raw.get("summary", "No review summary.")),
        "findings": [str(item) for item in findings],
        "file": path.name,
        "updated": _timestamp(path),
    }, path


def _tests(folder: Path) -> tuple[dict[str, Any], Path | None]:
    paths = _numbered_files(folder, ["test-command", "tests"])
    path = _latest(paths)
    if path is None:
        return {"status": "not-run", "summary": "No test result recorded."}, None
    content = _read(path)
    failed = re.search(r"\bFAILED\b|exit [1-9]\b|\[TIMEOUT\]", content, re.IGNORECASE)
    passed = re.search(r"exit 0|\bPASSED\b|All configured checks passed", content, re.IGNORECASE)
    status = "failed" if failed else "passed" if passed else "unknown"
    return {
        "status": status,
        "summary": content[-2_000:],
        "file": path.name,
        "updated": _timestamp(path),
    }, path


def _latest_developer(folder: Path) -> tuple[dict[str, Any], list[Path]]:
    outputs = _numbered_files(folder, ["developer"])
    output = _latest([path for path in outputs if path.suffix == ".txt"])
    progress = _latest([path for path in outputs if path.suffix == ".log"])
    return {
        "attempts": len({path.stem for path in outputs}),
        "summary": _read(output, 3_000) if output else "No developer result recorded.",
        "file": output.name if output else "",
        "updated": _timestamp(output) if output else "unknown",
        "progress": _read(progress, 3_000) if progress else "",
    }, outputs


def _all_log_files(folder: Path) -> list[Path]:
    files = [
        path for path in folder.iterdir()
        if path.is_file() and path.name != "review-schema.json"
    ] if folder.is_dir() else []
    return sorted(files, key=lambda path: (path.stat().st_mtime, path.name), reverse=True)


def _process_running(folder: Path) -> bool:
    progress = _latest(_numbered_files(folder, ["developer", "review", "test-command"]))
    if progress is None:
        return False
    content = _read(progress, 20_000)
    return "Started" in content and "Finished in" not in content


def _run_snapshot(folder: Path) -> dict[str, Any] | None:
    match = RUN_NAME.fullmatch(folder.name)
    if not match:
        return None
    review, review_path = _review(folder)
    tests, tests_path = _tests(folder)
    developer, developer_paths = _latest_developer(folder)
    all_files = _all_log_files(folder)
    last_activity = _latest(all_files)

    if _process_running(folder):
        status = "in-progress"
    elif review["status"] == "approved":
        status = "approved"
    elif review["status"] == "rejected":
        status = "needs-fix"
    elif tests["status"] == "failed":
        status = "tests-failed"
    elif all_files:
        status = "awaiting-review"
    else:
        status = "no-activity"

    file_records = []
    for path in all_files[:MAX_FILES_PER_RUN]:
        kind = path.name.split("-", 1)[0]
        file_records.append(_file_record(path, kind))

    return {
        "id": folder.name,
        "issue": int(match.group("issue")),
        "branch": f"agent/issue-{match.group('issue')}-{match.group('token')}",
        "status": status,
        "attempts": developer["attempts"],
        "last_activity": _timestamp(last_activity) if last_activity else "unknown",
        "review": review,
        "tests": tests,
        "developer": developer,
        "files": file_records,
        "file_count": len(all_files),
        "has_review": review_path is not None,
        "has_tests": tests_path is not None,
        "has_developer": bool(developer_paths),
    }


def collect_runs(logs_root: Path) -> list[dict[str, Any]]:
    if not logs_root.is_dir():
        return []
    runs = [snapshot for folder in logs_root.iterdir() if folder.is_dir()
            for snapshot in [_run_snapshot(folder)] if snapshot is not None]
    return sorted(runs, key=lambda run: (run["last_activity"], run["id"]), reverse=True)


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
    .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 20px; }}
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
    .needs-fix, .tests-failed {{ color: var(--red); background: color-mix(in srgb, var(--red) 15%, transparent); }}
    .in-progress {{ color: var(--blue); background: color-mix(in srgb, var(--blue) 15%, transparent); }}
    .awaiting-review {{ color: var(--yellow); background: color-mix(in srgb, var(--yellow) 15%, transparent); }}
    .no-activity {{ color: var(--muted); background: color-mix(in srgb, var(--muted) 15%, transparent); }}
    .summary {{ margin: 10px 0 8px; color: var(--muted); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 8px 14px; font-size: 12px; }}
    .detail {{ padding: 22px; min-height: 420px; }}
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
    @media (max-width: 620px) {{ .shell {{ padding: 18px 12px; }} header {{ display: block; }} .actions {{ margin-top: 14px; }} .metrics {{ grid-template-columns: repeat(2, 1fr); }} .toolbar {{ flex-wrap: wrap; }} .toolbar input {{ flex-basis: 100%; }} }}
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
    <section class="workspace">
      <div class="panel">
        <div class="toolbar">
          <label class="sr-only" for="search">Search runs</label>
          <input id="search" type="search" placeholder="Search issue or branch…">
          <label class="sr-only" for="status">Filter status</label>
          <select id="status"><option value="all">All statuses</option><option value="needs-fix">Needs fix</option><option value="approved">Approved</option><option value="in-progress">In progress</option><option value="tests-failed">Tests failed</option><option value="awaiting-review">Awaiting review</option></select>
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
      "tests-failed": "Tests failed", "awaiting-review": "Awaiting review", "no-activity": "No activity"
    }};
    const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[char]));
    const badge = status => `<span class="badge ${{esc(status)}}">${{esc(labels[status] || status)}}</span>`;
    const filtered = () => DATA.runs.filter(run =>
      (state.status === "all" || run.status === state.status) &&
      (`${{run.issue}} ${{run.branch}}`.toLowerCase().includes(state.query.toLowerCase()))
    );
    function renderMetrics() {{
      const runs = DATA.runs;
      const values = [
        ["Total runs", runs.length, ""],
        ["Approved", runs.filter(run => run.status === "approved").length, "approved"],
        ["Needs attention", runs.filter(run => ["needs-fix", "tests-failed"].includes(run.status)).length, "needs-fix"],
        ["In progress", runs.filter(run => run.status === "in-progress").length, "in-progress"]
      ];
      document.querySelector("#metrics").innerHTML = values.map(([label, value, kind]) => `<div class="metric"><span class="label">${{esc(label)}}</span><strong class="${{kind}}">${{esc(value)}}</strong></div>`).join("");
    }}
    function renderList() {{
      const visible = filtered();
      if (!visible.some(run => run.id === state.selected)) state.selected = visible[0]?.id || null;
      document.querySelector("#run-list").innerHTML = visible.length ? visible.map(run => `<button class="run-card ${{run.id === state.selected ? "selected" : ""}}" data-run="${{esc(run.id)}}" type="button">
        <div class="run-top"><span class="run-title">Issue #${{esc(run.issue)}}</span>${{badge(run.status)}}</div>
        <div class="summary">${{esc(run.review.summary || run.developer.summary || "No summary recorded.")}}</div>
        <div class="meta"><span>${{esc(run.attempts)}} attempt${{run.attempts === 1 ? "" : "s"}}</span><span>Tests: ${{esc(run.tests.status)}}</span><span>${{esc(run.last_activity)}}</span></div>
      </button>`).join("") : `<div class="empty">No runs match this filter.</div>`;
      document.querySelectorAll("[data-run]").forEach(button => button.addEventListener("click", () => {{ state.selected = button.dataset.run; renderList(); renderDetail(); }}));
    }}
    function renderDetail() {{
      const run = DATA.runs.find(item => item.id === state.selected);
      const detail = document.querySelector("#detail");
      if (!run) {{ detail.innerHTML = `<div class="empty">No run selected.</div>`; return; }}
      const findings = run.review.findings?.length ? `<ul class="findings">${{run.review.findings.map(item => `<li>${{esc(item)}}</li>`).join("")}}</ul>` : `<p class="subtle">No findings recorded.</p>`;
      const files = run.files.length ? run.files.map(file => `<details><summary>${{esc(file.kind)}} · ${{esc(file.name)}} <span class="meta">${{esc(file.updated)}}</span></summary><pre>${{esc(file.content)}}</pre></details>`).join("") : `<div class="empty">No log files recorded.</div>`;
      detail.innerHTML = `<div class="detail-top"><div><h2>Issue #${{esc(run.issue)}}</h2><div class="branch">${{esc(run.branch)}}</div></div>${{badge(run.status)}}</div>
        <div class="meta" style="margin-top:12px"><span>${{esc(run.attempts)}} developer attempt${{run.attempts === 1 ? "" : "s"}}</span><span>Last activity: ${{esc(run.last_activity)}}</span><span>${{esc(run.file_count)}} log files</span></div>
        <section class="callout ${{esc(run.review.status)}}"><strong>Review · ${{esc(labels[run.review.status] || run.review.status)}}</strong><p>${{esc(run.review.summary)}}</p>${{findings}}</section>
        <h3>Tests · ${{esc(run.tests.status)}}</h3><pre>${{esc(run.tests.summary)}}</pre>
        <h3>Latest developer result</h3><pre>${{esc(run.developer.summary)}}</pre>
        <h3>Run logs</h3><div class="file-list">${{files}}</div>`;
    }}
    document.querySelector("#source").textContent = `${{DATA.repo}} · generated ${{DATA.generated}} · ${{DATA.runs.length}} run${{DATA.runs.length === 1 ? "" : "s"}}`;
    document.querySelector("#search").addEventListener("input", event => {{ state.query = event.target.value; renderList(); renderDetail(); }});
    document.querySelector("#status").addEventListener("change", event => {{ state.status = event.target.value; renderList(); renderDetail(); }});
    document.querySelector("#theme").addEventListener("click", () => {{ document.documentElement.dataset.theme = document.documentElement.dataset.theme === "light" ? "dark" : "light"; }});
    renderMetrics(); renderList(); renderDetail();
  </script>
</body>
</html>
'''


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("help",), help="Show this help message and exit")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Target repository root")
    parser.add_argument("--logs", type=Path, help="Run-log directory (default: sibling .<repo>-agent-runs)")
    parser.add_argument("--output", type=Path, help="HTML output path (default: sibling .<repo>-agent-dashboard.html)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "help":
        parser.print_help()
        return 0
    repo = args.repo.resolve()
    logs_root = (args.logs or default_logs_root(repo)).resolve()
    output = (args.output or repo.parent / f".{repo.name}-agent-dashboard.html").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    runs = collect_runs(logs_root)
    output.write_text(render_dashboard(runs, repo.name, logs_root.name), encoding="utf-8")
    print(f"Dashboard written to {output}")
    print(f"Runs found: {len(runs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
