#!/usr/bin/env python3
"""Local web UI for launching guild droptimizer runs and monitoring progress."""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from flask import Flask, jsonify, request, send_file


APP_ROOT = pathlib.Path(__file__).resolve().parent


def _find_wowsim_root() -> pathlib.Path:
    """Find the WoWSim root directory, searching in multiple locations."""
    locations = [
        # Current APP_ROOT (when running as script)
        APP_ROOT,
        # Parent directories (in case running from a subdirectory)
        APP_ROOT.parent,
        APP_ROOT.parent.parent,
        # Common dev locations
        pathlib.Path.cwd(),
        pathlib.Path.cwd().parent,
    ]

    for loc in locations:
        # Check for marker files that indicate this is the WoWSim root
        if (loc / ".env.simrunner.local.example").exists() or (loc / "webapp.py").exists():
            return loc

    # Fallback to APP_ROOT
    return APP_ROOT


def _load_dotenv(path: pathlib.Path) -> None:
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    except FileNotFoundError:
        pass


# Load .env from multiple possible locations
WOWSIM_ROOT = _find_wowsim_root()
_load_dotenv(WOWSIM_ROOT / ".env.simrunner.local")

# Also try current working directory (for when running as standalone exe)
_load_dotenv(pathlib.Path.cwd() / ".env.simrunner.local")


def _site_base_url() -> str:
    return (
        os.environ.get("SIM_SITE_BASE_URL_DEV")
        or os.environ.get("SIM_SITE_BASE_URL")
        or ""
    ).rstrip("/")


def _runner_key() -> str:
    return (
        os.environ.get("SIM_RUNNER_KEY_DEV")
        or os.environ.get("SIM_RUNNER_KEY")
        or ""
    )


def _wowsim_app_api_key() -> str:
    return (os.environ.get("WOWSIM_APP_API_KEY") or "").strip()


def _manual_start_enabled() -> bool:
    raw = (os.environ.get("WOWSIM_ALLOW_MANUAL_START") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _request_is_authorized_website_call() -> bool:
    expected = _wowsim_app_api_key()
    if not expected:
        # If no key is configured, keep behavior permissive for local/dev setups.
        return True
    provided = request.headers.get("X-WoWSim-Key", "").strip()
    return bool(provided) and provided == expected


def _website_config(difficulty: str) -> str:
    if difficulty == "mythic":
        cfg = os.environ.get("WOWSIM_CONFIG_MYTHIC", "")
    else:
        cfg = os.environ.get("WOWSIM_CONFIG_HEROIC", "")
    return cfg.strip() or default_config_name()


@dataclass
class JobState:
    id: str
    command: list[str]
    status: str
    started_at: str
    ended_at: str | None = None
    exit_code: int | None = None
    progress_label: str = ""
    progress_current: int = 0
    progress_total: int = 0
    progress_pct: int = 0
    progress_stage: str = ""
    progress_detail: str = ""
    last_line: str = ""
    log_lines: list[str] = field(default_factory=list)
    report_csv: str = ""
    report_md: str = ""
    addon_profile_path: str | None = None


jobs: dict[str, JobState] = {}
job_processes: dict[str, subprocess.Popen[str]] = {}
job_lock = threading.Lock()


RAIDER_START_RE = re.compile(
    r"^(?:\[team\s+\d+\]\s+)?\[(\d+)/(\d+)\]\s+(?:Importing\s*\+\s*simming|Simming)\s+(.+)$",
    re.IGNORECASE,
)
SCENARIO_RE = re.compile(r"^\[(.+?)\] \[(\d+)/(\d+)\]")
REPORT_CSV_RE = re.compile(r"^CSV:\s+(.+)$")
REPORT_MD_RE = re.compile(r"^Markdown:\s+(.+)$")
PROGRESS_RE = re.compile(r"^@@PROGRESS@@\s+pct=(\d+)\s+stage=(.*?)\s+detail=(.*)$")
ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

JOB_TIMEOUT_SECS = 6 * 60 * 60


def list_config_files() -> list[str]:
    files = sorted(WOWSIM_ROOT.glob("config*.json"))
    return [p.name for p in files if p.is_file()]


def _build_runner_command(*args: str) -> list[str]:
    runner_script = WOWSIM_ROOT / "website_sim_runner.py"
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-website-sim", *args]
    return [sys.executable, str(runner_script), *args]


def _windows_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}

    kwargs: dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    kwargs["startupinfo"] = startupinfo
    return kwargs


def default_config_name() -> str:
    configs = list_config_files()
    preferred = [
        "config.guild.json",
        "config.json",
        "config.hunter-survival.all-raids-hc-mythic.json",
    ]
    for name in preferred:
        if name in configs:
            return name

    non_example = [c for c in configs if not c.endswith("example.json")]
    if non_example:
        return non_example[0]

    if "config.example.json" in configs:
        return "config.example.json"
    return "config.json"


def _append_log(job: JobState, line: str) -> None:
    job.last_line = line
    job.log_lines.append(line)
    if len(job.log_lines) > 5000:
        job.log_lines = job.log_lines[-5000:]


def _terminate_process_tree(proc: subprocess.Popen[str], force: bool = True) -> None:
    if proc.poll() is not None:
        return

    if os.name == "nt":
        cmd = ["taskkill", "/PID", str(proc.pid), "/T"]
        if force:
            cmd.append("/F")
        subprocess.run(cmd, capture_output=True, text=True, check=False)
        return

    try:
        if force:
            proc.kill()
        else:
            proc.terminate()
    except Exception:
        pass


def _run_job(job_id: str) -> None:
    with job_lock:
        job = jobs[job_id]
        if job.status == "canceled":
            return
        job.status = "running"

    proc = subprocess.Popen(
        job.command,
        cwd=str(WOWSIM_ROOT),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **_windows_subprocess_kwargs(),
    )
    with job_lock:
        job_processes[job_id] = proc

    timed_out = {"value": False}

    def on_timeout() -> None:
        with job_lock:
            cur = jobs.get(job_id)
            if not cur or cur.status in {"completed", "failed", "canceled", "timed_out"}:
                return
            cur.status = "timed_out"
            _append_log(cur, f"Job timed out after {JOB_TIMEOUT_SECS}s. Terminating process tree...")
            p = job_processes.get(job_id)
        timed_out["value"] = True
        if p is not None:
            _terminate_process_tree(p, force=True)

    timeout_timer = threading.Timer(JOB_TIMEOUT_SECS, on_timeout)
    timeout_timer.daemon = True
    timeout_timer.start()

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        line_clean = ANSI_ESCAPE_RE.sub("", line).strip()
        with job_lock:
            cur = jobs[job_id]
            _append_log(cur, line)

            m = RAIDER_START_RE.search(line_clean)
            if m:
                cur.progress_current = int(m.group(1))
                cur.progress_total = int(m.group(2))
                cur.progress_label = f"Raider {m.group(1)}/{m.group(2)}: {m.group(3)}"
                if cur.progress_total > 0:
                    # Keep a visible moving bar even before scenario-level @@PROGRESS@@ events.
                    cur.progress_pct = max(
                        cur.progress_pct,
                        int((cur.progress_current / cur.progress_total) * 100),
                    )
                continue

            p = PROGRESS_RE.search(line_clean)
            if not p and "@@PROGRESS@@" in line_clean:
                p = re.search(r"@@PROGRESS@@\s+pct=(\d+)\s+stage=(.*?)\s+detail=(.*)$", line_clean)
            if p:
                cur.progress_pct = int(p.group(1))
                cur.progress_stage = p.group(2).strip()
                cur.progress_detail = p.group(3).strip()
                cur.progress_label = f"{cur.progress_stage}: {cur.progress_detail}".strip(": ")
                continue

            s = SCENARIO_RE.search(line_clean)
            if s:
                cur.progress_label = (
                    f"{s.group(1)} scenario {s.group(2)}/{s.group(3)}"
                )

            r_csv = REPORT_CSV_RE.search(line_clean)
            if r_csv:
                cur.report_csv = r_csv.group(1).strip()
                continue

            r_md = REPORT_MD_RE.search(line_clean)
            if r_md:
                cur.report_md = r_md.group(1).strip()
                continue

    exit_code = proc.wait()
    timeout_timer.cancel()
    with job_lock:
        job_processes.pop(job_id, None)
        cur = jobs[job_id]
        cur.exit_code = exit_code
        cur.ended_at = dt.datetime.now().isoformat(timespec="seconds")
        if cur.status == "timed_out" or timed_out["value"]:
            cur.status = "timed_out"
        elif cur.status == "canceling":
            cur.status = "canceled"
            _append_log(cur, "Job canceled by user.")
        else:
            cur.status = "completed" if exit_code == 0 else "failed"
        
        # Clean up addon profile temp file if it exists
        if cur.addon_profile_path and pathlib.Path(cur.addon_profile_path).exists():
            try:
                pathlib.Path(cur.addon_profile_path).unlink()
            except Exception:
                pass


def _serialize_job(job: JobState) -> dict[str, Any]:
    return {
        "id": job.id,
        "command": job.command,
        "status": job.status,
        "started_at": job.started_at,
        "ended_at": job.ended_at,
        "exit_code": job.exit_code,
        "progress_label": job.progress_label,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "progress_pct": job.progress_pct,
        "progress_stage": job.progress_stage,
        "progress_detail": job.progress_detail,
        "last_line": job.last_line,
        "report_csv": job.report_csv,
        "report_md": job.report_md,
    }


app = Flask(__name__)


@app.get("/")
def index() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>WoWSim Guild Runner</title>
  <style>
    :root{--bg:#0d1117;--panel:#161b22;--ink:#c9d1d9;--muted:#8b949e;--accent:#58a6ff;--ok:#3fb950;--bad:#f85149;}
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);font-family:Segoe UI,system-ui,sans-serif}
    .wrap{max-width:1200px;margin:24px auto;padding:0 16px}
    .card{background:var(--panel);border:1px solid #30363d;border-radius:10px;padding:14px;margin-bottom:14px}
    h1{font-size:22px;margin:0 0 10px 0} h2{font-size:16px;margin:0 0 8px 0;color:var(--muted)}
    .grid{display:grid;grid-template-columns:2fr 1fr 1fr 1fr 1fr;gap:10px}
    .row{display:grid;grid-template-columns:1fr 1.4fr 1.4fr .6fr .6fr .8fr;gap:10px}
    label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
    .hint{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.25}
    .toggle-wrap{display:flex;align-items:center;gap:8px;padding-top:6px}
    .toggle-wrap input[type=checkbox]{width:16px;height:16px}
    input,select{width:100%;padding:8px;border-radius:8px;border:1px solid #30363d;background:#0b0f14;color:var(--ink)}
    button{padding:10px 14px;border:0;border-radius:8px;background:var(--accent);color:#03162a;font-weight:700;cursor:pointer}
    table{width:100%;border-collapse:collapse} th,td{padding:8px;border-bottom:1px solid #30363d;text-align:left;font-size:13px}
    th{color:var(--muted)}
    .pill{padding:2px 8px;border-radius:999px;font-size:12px}
    .running{background:#1f6feb33;color:#8ab4ff}.completed{background:#3fb95033;color:#7ee787}.failed{background:#f8514933;color:#ffaba8}.queued{background:#8b949e33;color:#c9d1d9}.canceling{background:#d2992233;color:#f2cc60}.canceled{background:#6e768133;color:#c9d1d9}.timed_out{background:#f0883e33;color:#ffb77c}
    pre{margin:0;max-height:360px;overflow:auto;background:#0b0f14;padding:10px;border-radius:8px;border:1px solid #30363d;font-size:12px;line-height:1.35}
        @media (max-width: 1100px){
            .grid{grid-template-columns:1fr 1fr}
            .row{grid-template-columns:1fr 1fr}
        }
        @media (max-width: 700px){
            .grid,.row{grid-template-columns:1fr}
        }
  </style>
</head>
<body>
<div class=\"wrap\">
    <div class=\"card\">
        <h1>WoWSim Guild Runner</h1>
        <h2>Start a run</h2>
        <div class=\"grid\">
            <div><label>Guild URL</label><input id=\"guildUrl\" value=\"https://worldofwarcraft.blizzard.com/en-us/guild/us/illidan/hidden-lodge/\" /></div>
            <div><label>Difficulty</label><select id=\"difficulty\"><option value=\"heroic\">heroic</option><option value=\"mythic\">mythic</option></select></div>
            <div><label>Max Raiders</label><input id=\"maxRaiders\" type=\"number\" value=\"2\" min=\"0\"/></div>
            <div><label>Parallel Raiders</label><input id=\"parallelRaiders\" type=\"number\" value=\"1\" min=\"1\"/></div>
            <div><label>Level</label><input id=\"level\" type=\"number\" value=\"90\" /></div>
        </div>
        <div class=\"row\" style=\"margin-top:10px\">
            <div><label>Locale</label><input id=\"locale\" value=\"en-us\" /></div>
            <div>
                <label>Dry Run</label>
                <div class=\"toggle-wrap\">
                    <input id=\"dryRun\" type=\"checkbox\" />
                    <span>Enable preflight only</span>
                </div>
                <div class=\"hint\">Fetches roster and filters candidates, but does not run SimulationCraft.</div>
            </div>
            <div>
                <label>Positive Only</label>
                <div class=\"toggle-wrap\">
                    <input id=\"positiveOnly\" type=\"checkbox\" />
                    <span>Show strict upgrades only</span>
                </div>
                <div class=\"hint\">When enabled, excludes items whose best recipient still loses DPS.</div>
            </div>
            <div></div>
            <div></div>
            <div style=\"display:flex;align-items:end\"><button id=\"startBtn\" onclick=\"startRun()\">Start Run</button></div>
        </div>
    </div>

    <div class=\"card\">
        <h2>Jobs</h2>
        <div style=\"display:flex;justify-content:flex-end;margin-bottom:8px\"><button onclick=\"clearHistory()\">Clear Job History</button></div>
        <div style=\"width:100%;overflow-x:auto\">
            <table id=\"jobsTable\"><thead><tr><th>ID</th><th>Status</th><th>Progress</th><th>Started</th><th>Ended</th><th>Reports</th><th>Action</th><th>Open</th></tr></thead><tbody></tbody></table>
        </div>
    </div>

    <div class=\"card\">
        <h2>Selected Job Log</h2>
        <pre id=\"logView\">Select a job to view logs.</pre>
    </div>
</div>
<script>
let selectedJob = null;
let autoConfig = null;
let followLog = true;
let refreshTimer = null;
const ACTIVE_STATUSES = new Set(['queued', 'running', 'canceling']);

function boolVal(v){return String(v).trim().toLowerCase()==='true';}

async function loadConfigs(){
    const r = await fetch('/api/configs');
    const data = await r.json();
    autoConfig = data.default_config || null;
}

async function startRun(){
    try {
        if(!autoConfig){
            await loadConfigs();
        }

        const payload = {
            guild_url: document.getElementById('guildUrl').value,
            difficulty: document.getElementById('difficulty').value,
            max_raiders: parseInt(document.getElementById('maxRaiders').value||'0',10),
            parallel_raiders: parseInt(document.getElementById('parallelRaiders').value||'1',10),
            level: parseInt(document.getElementById('level').value||'90',10),
            locale: document.getElementById('locale').value,
            dry_run: document.getElementById('dryRun').checked,
            positive_only: document.getElementById('positiveOnly').checked,
        };
        if(autoConfig){
            payload.config = autoConfig;
        }

        const btn = document.getElementById('startBtn');
        btn.disabled = true;
        btn.textContent = 'Starting...';
        const r = await fetch('/api/start', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify(payload)
        });
        const j = await r.json();
        btn.disabled = false;
        btn.textContent = 'Start Run';
        if(!r.ok){ alert(j.error || 'Failed to start run'); return; }
        selectedJob = j.job.id;
        await refresh();
    } catch (err){
        const btn = document.getElementById('startBtn');
        btn.disabled = false;
        btn.textContent = 'Start Run';
        const msg = (err && err.message) ? err.message : String(err);
        alert('Failed to start run: ' + msg);
    }
}

async function refresh(){
  const r = await fetch('/api/jobs');
  const jobs = await r.json();
  const tbody = document.querySelector('#jobsTable tbody');
  tbody.innerHTML = '';
    const hasActiveJob = jobs.some(j => ACTIVE_STATUSES.has(j.status));
  for(const j of jobs){
    const tr = document.createElement('tr');
    const pill = `<span class=\"pill ${j.status}\">${j.status}</span>`;
    let prog = '';
    if((j.progress_pct||0) > 0){
        prog = `${j.progress_pct}% ${j.progress_stage||''} ${j.progress_detail||''}`.trim();
    } else if(j.progress_total>0){
        prog = `${j.progress_current}/${j.progress_total} ${j.progress_label||''}`.trim();
    } else {
        prog = (j.progress_label || '').trim();
    }
        const csv = j.report_csv ? `<button onclick=\"downloadReport('${j.id}','csv')\">CSV</button>` : '';
        const md = j.report_md ? `<button onclick=\"downloadReport('${j.id}','md')\">MD</button>` : '';
        const reports = `${csv} ${md}`.trim();
        const canStop = j.status === 'running' || j.status === 'queued' || j.status === 'canceling';
        const action = canStop
            ? `<button ${j.status === 'canceling' ? 'disabled' : ''} onclick="stopJob('${j.id}')">${j.status === 'canceling' ? 'Stopping...' : 'Stop'}</button>`
            : '';
        tr.innerHTML = `<td>${j.id.slice(0,8)}</td><td>${pill}</td><td>${prog}</td><td>${j.started_at||''}</td><td>${j.ended_at||''}</td><td>${reports}</td><td>${action}</td><td><button onclick="openJob('${j.id}')">View</button></td>`;
    tbody.appendChild(tr);
  }
  if(selectedJob){ await loadLog(selectedJob); }
    scheduleRefresh(hasActiveJob);
}

function scheduleRefresh(shouldPoll){
        if(refreshTimer){
                clearTimeout(refreshTimer);
                refreshTimer = null;
        }
        if(shouldPoll){
                refreshTimer = setTimeout(refresh, 2000);
        }
}

async function stopJob(id){
    const ok = confirm('Stop this running job?');
    if(!ok){ return; }
    const r = await fetch('/api/jobs/'+id+'/stop', { method: 'POST' });
    const j = await r.json();
    if(!r.ok){ alert(j.error || 'Failed to stop job'); return; }
    await refresh();
}

async function downloadReport(id, kind){
    window.open('/api/jobs/'+id+'/report/'+kind, '_blank');
}

async function clearHistory(){
    const ok = confirm('Clear completed and failed job history?');
    if(!ok){ return; }
    const r = await fetch('/api/jobs/clear', { method: 'POST' });
    const j = await r.json();
    if(!r.ok){ alert(j.error || 'Failed to clear history'); return; }
    if(selectedJob && !(j.remaining_ids || []).includes(selectedJob)){
        selectedJob = null;
        document.getElementById('logView').textContent = 'Select a job to view logs.';
    }
    await refresh();
}

async function openJob(id){ selectedJob = id; followLog = true; await loadLog(id); }

async function loadLog(id){
    const logView = document.getElementById('logView');
    const distanceFromBottom = logView.scrollHeight - logView.scrollTop - logView.clientHeight;
    const shouldStickToBottom = followLog || distanceFromBottom < 24;
  const r = await fetch('/api/jobs/'+id+'/log?tail=250');
  const j = await r.json();
    if(!r.ok){ logView.textContent = j.error || 'Failed to load log'; return; }
    logView.textContent = j.lines.join('\\n');
    if(shouldStickToBottom){
        logView.scrollTop = logView.scrollHeight;
    }
}

document.getElementById('logView').addEventListener('scroll', () => {
    const logView = document.getElementById('logView');
    const distanceFromBottom = logView.scrollHeight - logView.scrollTop - logView.clientHeight;
    followLog = distanceFromBottom < 24;
});

loadConfigs().then(refresh);
</script>
</body>
</html>"""


@app.post("/api/start")
def api_start() -> Any:
    if not _manual_start_enabled():
        return jsonify({
            "error": "Manual starts are disabled. Use website launch flow via /api/jobs/start.",
        }), 403

    payload = request.get_json(silent=True) or {}

    raw_config = payload.get("config")
    config = str(raw_config).strip() if raw_config else default_config_name()
    guild_url = str(payload.get("guild_url", "")).strip()
    difficulty = str(payload.get("difficulty", "")).strip().lower()
    locale = str(payload.get("locale", "en-us")).strip()
    level = int(payload.get("level", 90) or 90)
    max_raiders = int(payload.get("max_raiders", 0) or 0)
    parallel_raiders = int(payload.get("parallel_raiders", 1) or 1)
    dry_run = bool(payload.get("dry_run", False))
    positive_only = bool(payload.get("positive_only", False))

    if not guild_url:
        return jsonify({"error": "guild_url is required"}), 400
    if difficulty not in {"heroic", "mythic"}:
        return jsonify({"error": "difficulty must be heroic or mythic"}), 400
    if parallel_raiders < 1:
        return jsonify({"error": "parallel_raiders must be >= 1"}), 400
    if not (WOWSIM_ROOT / config).exists():
        return jsonify({
            "error": f"Config file not found: {config}",
            "default_config": default_config_name(),
        }), 400

    cmd = [
        sys.executable,
        "-u",
        "guild_droptimizer.py",
        "--config",
        config,
        "--guild-url",
        guild_url,
        "--difficulty",
        difficulty,
        "--level",
        str(level),
        "--locale",
        locale,
        "--parallel-raiders",
        str(parallel_raiders),
    ]
    if max_raiders > 0:
        cmd.extend(["--max-raiders", str(max_raiders)])
    if dry_run:
        cmd.append("--dry-run")
    if positive_only:
        cmd.append("--positive-only")

    job_id = uuid.uuid4().hex
    job = JobState(
        id=job_id,
        command=cmd,
        status="queued",
        started_at=dt.datetime.now().isoformat(timespec="seconds"),
    )
    with job_lock:
        jobs[job_id] = job

    t = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
    t.start()

    return jsonify({"job": _serialize_job(job)})


@app.post("/api/jobs/start")
def api_jobs_start() -> Any:
    if not _request_is_authorized_website_call():
        return jsonify({"error": "unauthorized website launch request"}), 401

    payload = request.get_json(silent=True) or {}

    char_name = str(payload.get("char_name", "")).strip()
    realm_slug = str(payload.get("realm_slug", "")).strip()
    region = str(payload.get("region", "us")).strip().lower() or "us"
    site_team_id = payload.get("site_team_id")
    difficulty = str(payload.get("difficulty", "heroic")).strip().lower()
    mode = str(payload.get("mode", "site")).strip().lower()
    addon_export = str(payload.get("addon_export", "")).strip() if mode == "addon" else ""
    sim_raid = str(payload.get("sim_raid", "all")).strip().lower()
    sim_difficulty = str(payload.get("sim_difficulty", "all")).strip().lower()

    if not char_name:
        return jsonify({"error": "char_name is required"}), 400
    if not realm_slug:
        return jsonify({"error": "realm_slug is required"}), 400
    if not isinstance(site_team_id, int) or site_team_id <= 0:
        return jsonify({"error": "site_team_id must be a positive integer"}), 400
    if difficulty not in {"heroic", "mythic"}:
        return jsonify({"error": "difficulty must be heroic or mythic"}), 400
    if mode not in {"site", "addon"}:
        return jsonify({"error": "mode must be site or addon"}), 400
    if mode == "addon" and not addon_export:
        return jsonify({"error": "addon_export is required when mode is addon"}), 400
    if sim_raid not in {"all", "voidspire", "dreamrift", "queldanas"}:
        sim_raid = "all"
    if sim_difficulty not in {"all", "normal", "heroic", "mythic"}:
        sim_difficulty = "all"

    base_url = _site_base_url()
    runner_key = _runner_key()
    if not base_url or not runner_key:
        return jsonify({"error": "SIM_SITE_BASE_URL_DEV and SIM_RUNNER_KEY_DEV must be set in .env.simrunner.local"}), 503

    config_name = _website_config(difficulty)
    if not (WOWSIM_ROOT / config_name).exists():
        return jsonify({"error": f"Config file not found: {config_name}"}), 503

    addon_profile_path: str | None = None
    if mode == "addon":
        # Write addon export to a temp file
        try:
            fd, addon_profile_path = tempfile.mkstemp(suffix=".txt", prefix="addon_", text=True)
            try:
                os.write(fd, addon_export.encode("utf-8"))
            finally:
                os.close(fd)
        except Exception as e:
            return jsonify({"error": f"Failed to write addon profile: {str(e)}"}), 500

    mode_args: list[str]
    if mode == "addon":
        # Guaranteed by validation and temp file creation above.
        mode_args = ["--mode", "addon", "--addon-profile", addon_profile_path or ""]
    else:
        mode_args = ["--mode", "site"]

    cmd = _build_runner_command(
        "--config", config_name,
        "--site-base-url", base_url,
        "--runner-key", runner_key,
        "--character-name", char_name,
        "--team-id", str(site_team_id),
        "--sim-raid", sim_raid,
        "--sim-difficulty", sim_difficulty,
        *mode_args,
    )

    job_id = uuid.uuid4().hex
    job = JobState(
        id=job_id,
        command=cmd,
        status="queued",
        started_at=dt.datetime.now().isoformat(timespec="seconds"),
        addon_profile_path=addon_profile_path,
    )
    with job_lock:
        jobs[job_id] = job

    t = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.get("/api/configs")
def api_configs() -> Any:
    configs = list_config_files()
    return jsonify({
        "configs": configs,
        "default_config": default_config_name(),
    })


@app.get("/api/jobs")
def api_jobs() -> Any:
    with job_lock:
        data = [_serialize_job(j) for j in jobs.values()]
    data.sort(key=lambda x: x["started_at"], reverse=True)
    return jsonify(data)


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str) -> Any:
    with job_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "job not found"}), 404
        return jsonify(_serialize_job(job))


@app.get("/api/jobs/<job_id>/log")
def api_job_log(job_id: str) -> Any:
    tail = int(request.args.get("tail", 200) or 200)
    tail = max(1, min(5000, tail))

    with job_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "job not found"}), 404
        lines = job.log_lines[-tail:]

    return jsonify({"job_id": job_id, "lines": lines})


@app.post("/api/jobs/<job_id>/stop")
def api_job_stop(job_id: str) -> Any:
    with job_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "job not found"}), 404

        if job.status in {"completed", "failed", "canceled"}:
            return jsonify({"job": _serialize_job(job), "message": "job already finished"})

        if job.status == "queued":
            job.status = "canceled"
            job.ended_at = dt.datetime.now().isoformat(timespec="seconds")
            _append_log(job, "Job canceled before start.")
            return jsonify({"job": _serialize_job(job), "message": "job canceled"})

        if job.status == "canceling":
            return jsonify({"job": _serialize_job(job), "message": "cancel already requested"})

        proc = job_processes.get(job_id)
        job.status = "canceling"
        _append_log(job, "Cancellation requested...")

    if proc is not None:
        _terminate_process_tree(proc, force=True)

    with job_lock:
        cur = jobs[job_id]
        return jsonify({"job": _serialize_job(cur), "message": "cancel requested"})


@app.post("/api/jobs/clear")
def api_jobs_clear() -> Any:
    with job_lock:
        keep: dict[str, JobState] = {}
        removed = 0
        for job_id, job in jobs.items():
            if job.status in {"running", "queued", "canceling"}:
                keep[job_id] = job
            else:
                removed += 1
        jobs.clear()
        jobs.update(keep)
        remaining_ids = list(jobs.keys())

    return jsonify({"removed": removed, "remaining_ids": remaining_ids})


@app.get("/api/jobs/<job_id>/report/<kind>")
def api_job_report(job_id: str, kind: str) -> Any:
    if kind not in {"csv", "md"}:
        return jsonify({"error": "kind must be csv or md"}), 400

    with job_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "job not found"}), 404
        report_path = job.report_csv if kind == "csv" else job.report_md

    if not report_path:
        return jsonify({"error": f"{kind} report not available for this job"}), 404

    file_path = pathlib.Path(report_path)
    if not file_path.is_absolute():
        file_path = (WOWSIM_ROOT / file_path).resolve()

    if not file_path.exists() or not file_path.is_file():
        return jsonify({"error": f"report file not found: {file_path}"}), 404

    return send_file(
        str(file_path),
        as_attachment=True,
        download_name=file_path.name,
        mimetype="text/csv" if kind == "csv" else "text/markdown",
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
