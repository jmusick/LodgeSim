#!/usr/bin/env python3
"""Local web UI for launching guild droptimizer runs and monitoring progress."""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from flask import Flask, jsonify, request, send_file


APP_ROOT = pathlib.Path(__file__).resolve().parent


def _find_wowsim_root() -> pathlib.Path:
    """Find the WoWSim root directory, searching in multiple locations."""
    locations: list[pathlib.Path] = []

    # In frozen mode (__file__ points to a temp extraction dir), prefer the
    # directory containing the EXE that the user launched.
    if getattr(sys, "frozen", False):
        try:
            locations.append(pathlib.Path(sys.executable).resolve().parent)
        except Exception:
            pass

    locations.extend([
        # Current APP_ROOT (when running as script)
        APP_ROOT,
        # Parent directories (in case running from a subdirectory)
        APP_ROOT.parent,
        APP_ROOT.parent.parent,
        # Common dev locations
        pathlib.Path.cwd(),
        pathlib.Path.cwd().parent,
    ])

    for loc in locations:
        # Check for marker files that indicate this is the WoWSim root
        if (
            (loc / ".env.simrunner.local").exists()
            or (loc / ".env.simrunner.local.example").exists()
            or (loc / "webapp.py").exists()
            or (loc / "config.guild.json").exists()
            or (loc / "update-simc.ps1").exists()
        ):
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
                os.environ[key.strip()] = value.strip()
    except FileNotFoundError:
        pass


# Load .env from multiple possible locations
WOWSIM_ROOT = _find_wowsim_root()
_load_dotenv(WOWSIM_ROOT / ".env.simrunner.local")

# Also try current working directory (for when running as standalone exe)
_load_dotenv(pathlib.Path.cwd() / ".env.simrunner.local")


active_environment_lock = threading.Lock()
active_environment = (os.environ.get("WOWSIM_ENV") or "dev").strip().lower()
if active_environment not in {"dev", "prod"}:
    active_environment = "dev"


def _get_active_environment() -> str:
    with active_environment_lock:
        return active_environment


def _set_active_environment(value: str) -> None:
    global active_environment
    normalized = (value or "").strip().lower()
    if normalized not in {"dev", "prod"}:
        normalized = "dev"
    with active_environment_lock:
        active_environment = normalized


def _site_base_url() -> str:
    env_name = _get_active_environment().upper()
    return (
        os.environ.get(f"SIM_SITE_BASE_URL_{env_name}")
        or os.environ.get("SIM_SITE_BASE_URL")
        or ""
    ).rstrip("/")


def _runner_key() -> str:
    env_name = _get_active_environment().upper()
    return (
        os.environ.get(f"SIM_RUNNER_KEY_{env_name}")
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
    priority: int = 0
    queue_seq: int = 0
    source: str = "manual"
    task_id: str | None = None


jobs: dict[str, JobState] = {}
job_processes: dict[str, subprocess.Popen[str]] = {}
job_lock = threading.Lock()
job_cond = threading.Condition(job_lock)
job_worker_thread: threading.Thread | None = None
passive_scheduler_thread: threading.Thread | None = None
job_queue_seq = 0
job_seq_lock = threading.Lock()
passive_enqueued_at: dict[str, int] = {}


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
PASSIVE_DEFAULT_INTERVAL_SECS = 300
PASSIVE_DEFAULT_STALE_SECS = 24 * 60 * 60
PASSIVE_ENQUEUE_COOLDOWN_SECS = 2 * 60 * 60
PASSIVE_ENDPOINT_404_BACKOFF_SECS = 30 * 60
SIM_API_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) WoWSimRunner/1.0"


shutdown_event = threading.Event()
_passive_endpoint_404_backoff_until = 0
fallback_state_lock = threading.Lock()
FALLBACK_STATE_PATH = WOWSIM_ROOT / "generated" / "passive-fallback-state.json"
fallback_task_updated_at: dict[str, int] = {}


def _load_fallback_state() -> None:
    global fallback_task_updated_at
    try:
        raw = FALLBACK_STATE_PATH.read_text(encoding="utf-8")
        payload = json.loads(raw)
        entries = payload.get("task_updated_at", {}) if isinstance(payload, dict) else {}
        if isinstance(entries, dict):
            normalized: dict[str, int] = {}
            for key, value in entries.items():
                if not isinstance(key, str):
                    continue
                try:
                    ts = int(value)
                except Exception:
                    continue
                if ts > 0:
                    normalized[key] = ts
            fallback_task_updated_at = normalized
    except FileNotFoundError:
        fallback_task_updated_at = {}
    except Exception as exc:
        print(f"[passive] failed to load fallback state: {exc}")
        fallback_task_updated_at = {}


def _save_fallback_state() -> None:
    try:
        FALLBACK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "task_updated_at": fallback_task_updated_at,
            "saved_at": int(dt.datetime.now(dt.UTC).timestamp()),
        }
        FALLBACK_STATE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        print(f"[passive] failed to save fallback state: {exc}")


_load_fallback_state()


def _next_queue_seq() -> int:
    global job_queue_seq
    with job_seq_lock:
        job_queue_seq += 1
        return job_queue_seq


def _queued_jobs_sorted_locked() -> list[JobState]:
    queued = [job for job in jobs.values() if job.status == "queued"]
    queued.sort(key=lambda j: (-j.priority, j.queue_seq, j.started_at))
    return queued


def _passive_enabled() -> bool:
    raw = (os.environ.get("WOWSIM_PASSIVE_ENABLED") or "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _passive_interval_secs() -> int:
    raw = (os.environ.get("WOWSIM_PASSIVE_INTERVAL_SECS") or "").strip()
    try:
        value = int(raw) if raw else PASSIVE_DEFAULT_INTERVAL_SECS
    except ValueError:
        value = PASSIVE_DEFAULT_INTERVAL_SECS
    return max(30, min(3600, value))


def _passive_stale_secs() -> int:
    raw = (os.environ.get("WOWSIM_PASSIVE_STALE_SECS") or "").strip()
    try:
        value = int(raw) if raw else PASSIVE_DEFAULT_STALE_SECS
    except ValueError:
        value = PASSIVE_DEFAULT_STALE_SECS
    return max(3600, min(7 * 24 * 60 * 60, value))


def _passive_startup_stale_secs() -> int:
    """Stale threshold used only for the first passive poll after app startup.

    Defaults to 0 so startup can immediately enqueue pending work.
    """
    raw = (os.environ.get("WOWSIM_PASSIVE_STARTUP_MAX_AGE_SECS") or "").strip()
    try:
        value = int(raw) if raw else 0
    except ValueError:
        value = 0
    return max(0, min(7 * 24 * 60 * 60, value))


def _manual_jobs_active_locked() -> bool:
    for job in jobs.values():
        if not job.source.startswith("manual"):
            continue
        if job.status in {"queued", "running", "canceling"}:
            return True
    return False


def _has_active_task_locked(task_id: str) -> bool:
    for job in jobs.values():
        if job.task_id != task_id:
            continue
        if job.status in {"queued", "running", "canceling"}:
            return True
    return False


def _open_url_with_proxy_fallback(req: urllib.request.Request, timeout: float):
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        # WinError 10061 here is commonly caused by a dead local proxy.
        if getattr(reason, "winerror", None) != 10061:
            raise

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)


def _fetch_passive_tasks(max_tasks: int, stale_secs: int) -> list[dict[str, Any]]:
    global _passive_endpoint_404_backoff_until

    base_url = _site_base_url()
    runner_key = _runner_key()
    if not base_url or not runner_key:
        return []

    now_epoch = int(dt.datetime.now(dt.UTC).timestamp())
    if now_epoch < _passive_endpoint_404_backoff_until:
        return _fetch_passive_tasks_from_targets(base_url, runner_key, max_tasks, stale_secs)

    query = urllib.parse.urlencode({
        "max_tasks": str(max_tasks),
        "max_age_seconds": str(stale_secs),
    })
    url = f"{base_url}/api/sim/passive/tasks?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Sim-Runner-Key": runner_key,
            "User-Agent": SIM_API_USER_AGENT,
        },
        method="GET",
    )

    try:
        with _open_url_with_proxy_fallback(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw.strip() else {}
            tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
            return tasks if isinstance(tasks, list) else []
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _passive_endpoint_404_backoff_until = now_epoch + PASSIVE_ENDPOINT_404_BACKOFF_SECS
            print("[passive] endpoint /api/sim/passive/tasks not found; backing off and using targets fallback")
            return _fetch_passive_tasks_from_targets(base_url, runner_key, max_tasks, stale_secs)
        print(f"[passive] task fetch HTTP error {exc.code}: {exc}")
    except Exception as exc:
        print(f"[passive] task fetch failed: {exc}")

    return []


def _fetch_passive_tasks_from_targets(base_url: str, runner_key: str, max_tasks: int, stale_secs: int) -> list[dict[str, Any]]:
    """Fallback for older website instances that do not expose passive task endpoint.

    This fallback queues single-target tasks derived from /api/sim/targets.
    """
    url = f"{base_url}/api/sim/targets"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Sim-Runner-Key": runner_key,
            "User-Agent": SIM_API_USER_AGENT,
        },
        method="GET",
    )

    try:
        with _open_url_with_proxy_fallback(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        print(f"[passive] targets fallback failed: {exc}")
        return []

    teams = payload.get("teams", []) if isinstance(payload, dict) else []
    if not isinstance(teams, list):
        return []

    tasks: list[dict[str, Any]] = []
    now_epoch = int(dt.datetime.now(dt.UTC).timestamp())
    for team in teams:
        if not isinstance(team, dict):
            continue

        team_id = int(team.get("team_id") or 0)
        difficulty = str(team.get("difficulty") or "heroic").strip().lower()
        if team_id <= 0 or difficulty not in {"heroic", "mythic"}:
            continue

        raiders = team.get("raiders", [])
        if not isinstance(raiders, list):
            continue

        for raider in raiders:
            if not isinstance(raider, dict):
                continue

            char_id = int(raider.get("blizzard_char_id") or 0)
            char_name = str(raider.get("name") or "").strip()
            realm_slug = str(raider.get("realm_slug") or "").strip()
            if char_id <= 0 or not char_name or not realm_slug:
                continue

            task_id = f"{team_id}:{difficulty}:{char_id}:single_target"
            with fallback_state_lock:
                last_updated = int(fallback_task_updated_at.get(task_id, 0) or 0)
            stale_seconds = (now_epoch - last_updated) if last_updated > 0 else (stale_secs + 1)
            if stale_seconds < stale_secs:
                continue

            tasks.append({
                "task_id": task_id,
                "task_type": "single_target",
                "site_team_id": team_id,
                "difficulty": difficulty,
                "char_id": char_id,
                "char_name": char_name,
                "realm_slug": realm_slug,
                "region": "us",
                "sim_raid": "all",
                "sim_difficulty": "all",
                "stale_seconds": stale_seconds,
                "last_sim_updated_at": last_updated if last_updated > 0 else None,
            })

            if len(tasks) >= max_tasks:
                return tasks

    return tasks


def _build_passive_command(task: dict[str, Any]) -> list[str] | None:
    task_type = str(task.get("task_type") or "droptimizer").strip().lower()
    char_name = str(task.get("char_name") or "").strip()
    team_id = int(task.get("site_team_id") or 0)
    difficulty = str(task.get("difficulty") or "heroic").strip().lower()
    sim_raid = str(task.get("sim_raid") or "all").strip().lower()
    sim_difficulty = str(task.get("sim_difficulty") or "all").strip().lower()

    if not char_name or team_id <= 0:
        return None
    if task_type not in {"droptimizer", "single_target"}:
        task_type = "droptimizer"
    if difficulty not in {"heroic", "mythic"}:
        difficulty = "heroic"

    base_url = _site_base_url()
    runner_key = _runner_key()
    if not base_url or not runner_key:
        return None

    config_name = _website_config(difficulty)
    if not (WOWSIM_ROOT / config_name).exists():
        return None

    mode = "single_target" if task_type == "single_target" else "site"

    return _build_runner_command(
        "--config", config_name,
        "--site-base-url", base_url,
        "--runner-key", runner_key,
        "--character-name", char_name,
        "--team-id", str(team_id),
        "--sim-raid", sim_raid,
        "--sim-difficulty", sim_difficulty,
        "--mode", mode,
    )


def _ensure_passive_scheduler_started() -> None:
    global passive_scheduler_thread
    with job_cond:
        if passive_scheduler_thread is not None and passive_scheduler_thread.is_alive():
            return

        def passive_loop() -> None:
            first_poll = True
            while not shutdown_event.is_set():
                interval = _passive_interval_secs()
                if not _passive_enabled():
                    if shutdown_event.wait(interval):
                        break
                    continue

                with job_lock:
                    if _manual_jobs_active_locked():
                        if shutdown_event.wait(interval):
                            break
                        continue

                stale_secs = _passive_startup_stale_secs() if first_poll else _passive_stale_secs()
                first_poll = False
                tasks = _fetch_passive_tasks(max_tasks=10, stale_secs=stale_secs)
                if not tasks:
                    if shutdown_event.wait(interval):
                        break
                    continue

                now_epoch = int(dt.datetime.now(dt.UTC).timestamp())
                enqueued_any = False

                with job_cond:
                    for raw_task in tasks:
                        if not isinstance(raw_task, dict):
                            continue

                        task_id = str(raw_task.get("task_id") or "").strip()
                        if not task_id:
                            continue
                        if _has_active_task_locked(task_id):
                            continue

                        last_enqueued = passive_enqueued_at.get(task_id, 0)
                        if now_epoch - last_enqueued < PASSIVE_ENQUEUE_COOLDOWN_SECS:
                            continue

                        cmd = _build_passive_command(raw_task)
                        if not cmd:
                            continue

                        job_id = uuid.uuid4().hex
                        job = JobState(
                            id=job_id,
                            command=cmd,
                            status="queued",
                            started_at=dt.datetime.now().isoformat(timespec="seconds"),
                            priority=30 if str(raw_task.get("task_type") or "") == "single_target" else 10,
                            queue_seq=_next_queue_seq(),
                            source=(
                                "passive-single-target"
                                if str(raw_task.get("task_type") or "") == "single_target"
                                else "passive-droptimizer"
                            ),
                            task_id=task_id,
                        )
                        jobs[job_id] = job
                        passive_enqueued_at[task_id] = now_epoch
                        enqueued_any = True

                    if enqueued_any:
                        job_cond.notify_all()

                if enqueued_any:
                    print("[passive] queued one or more stale background tasks")

                if shutdown_event.wait(interval):
                    break

        passive_scheduler_thread = threading.Thread(target=passive_loop, daemon=True, name="wowsim-passive-scheduler")
        passive_scheduler_thread.start()


def _ensure_worker_started() -> None:
    global job_worker_thread
    with job_cond:
        if job_worker_thread is not None and job_worker_thread.is_alive():
            return

        def worker_loop() -> None:
            while not shutdown_event.is_set():
                with job_cond:
                    while True:
                        if shutdown_event.is_set():
                            return
                        queued = _queued_jobs_sorted_locked()
                        if queued:
                            next_job = queued[0]
                            break
                        job_cond.wait(timeout=1.0)
                if shutdown_event.is_set():
                    return
                _run_job(next_job.id)

        job_worker_thread = threading.Thread(target=worker_loop, daemon=True, name="wowsim-job-worker")
        job_worker_thread.start()


def list_config_files() -> list[str]:
    files = sorted(WOWSIM_ROOT.glob("config*.json"))
    return [p.name for p in files if p.is_file()]


def _build_runner_command(*args: str) -> list[str]:
    runner_script = WOWSIM_ROOT / "website_sim_runner.py"
    if getattr(sys, "frozen", False):
        preferred_python = os.environ.get("WOWSIM_RUNNER_PYTHON", "").strip()
        if preferred_python and pathlib.Path(preferred_python).exists() and runner_script.exists():
            return [preferred_python, str(runner_script), *args]

        venv_python = WOWSIM_ROOT / ".venv" / "Scripts" / "python.exe"
        if venv_python.exists() and runner_script.exists():
            return [str(venv_python), str(runner_script), *args]

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
        if shutdown_event.is_set():
            job.status = "canceled"
            job.ended_at = dt.datetime.now().isoformat(timespec="seconds")
            _append_log(job, "Job canceled due to app shutdown.")
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

        if cur.status == "completed" and cur.task_id and cur.source == "passive-single-target":
            updated_at = int(dt.datetime.now(dt.UTC).timestamp())
            with fallback_state_lock:
                fallback_task_updated_at[cur.task_id] = updated_at
                _save_fallback_state()
        
        # Clean up addon profile temp file if it exists
        if cur.addon_profile_path and pathlib.Path(cur.addon_profile_path).exists():
            try:
                pathlib.Path(cur.addon_profile_path).unlink()
            except Exception:
                pass


def _shutdown_background_workers() -> None:
    shutdown_event.set()

    processes_to_kill: list[subprocess.Popen[str]] = []
    with job_cond:
        for job in jobs.values():
            if job.status == "queued":
                job.status = "canceled"
                job.ended_at = dt.datetime.now().isoformat(timespec="seconds")
                _append_log(job, "Job canceled due to app shutdown.")
            elif job.status in {"running", "canceling"}:
                proc = job_processes.get(job.id)
                if proc is not None:
                    processes_to_kill.append(proc)
                if job.status != "canceling":
                    job.status = "canceling"
                    _append_log(job, "App shutdown requested; terminating job process...")

        job_cond.notify_all()

    for proc in processes_to_kill:
        _terminate_process_tree(proc, force=True)


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
        "priority": job.priority,
        "queue_seq": job.queue_seq,
        "source": job.source,
        "task_id": job.task_id,
    }


app = Flask(__name__)
_ensure_worker_started()
_ensure_passive_scheduler_started()


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
        priority=80,
        queue_seq=_next_queue_seq(),
        source="manual-local",
    )
    with job_cond:
        jobs[job_id] = job
        job_cond.notify_all()

    _ensure_worker_started()

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
        priority=100,
        queue_seq=_next_queue_seq(),
        source="manual-website",
    )
    with job_cond:
        jobs[job_id] = job
        job_cond.notify_all()

    _ensure_worker_started()

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
        queue_positions = {job.id: idx + 1 for idx, job in enumerate(_queued_jobs_sorted_locked())}
        data = []
        for job in jobs.values():
            item = _serialize_job(job)
            item["queue_position"] = queue_positions.get(job.id)
            data.append(item)

    status_order = {
        "running": 0,
        "canceling": 1,
        "queued": 2,
        "completed": 3,
        "failed": 4,
        "timed_out": 5,
        "canceled": 6,
    }

    def sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        status = str(item.get("status") or "")
        group = status_order.get(status, 99)
        if status == "queued":
            qpos = int(item.get("queue_position") or 999999)
            return (group, qpos, str(item.get("started_at") or ""))
        # Reverse chronological for non-queued rows by using inverse lexical marker.
        started = str(item.get("started_at") or "")
        return (group, 0, "~" + started)

    data.sort(key=sort_key)
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
            with job_cond:
                job_cond.notify_all()
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


@app.post("/api/jobs/<job_id>/run-now")
def api_job_run_now(job_id: str) -> Any:
    with job_cond:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "job not found"}), 404

        if job.status != "queued":
            return jsonify({"error": "only queued jobs can be moved"}), 400

        min_seq = min((j.queue_seq for j in jobs.values() if j.status == "queued"), default=job.queue_seq)
        job.queue_seq = min_seq - 1
        job.priority = max(job.priority, 1000)
        _append_log(job, "Moved to front of queue.")
        job_cond.notify_all()

        queue_positions = {queued.id: idx + 1 for idx, queued in enumerate(_queued_jobs_sorted_locked())}
        payload = _serialize_job(job)
        payload["queue_position"] = queue_positions.get(job.id)
        return jsonify({"job": payload, "message": "moved to front"})


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


@app.post("/api/admin/shutdown")
def api_admin_shutdown() -> Any:
    remote = (request.remote_addr or "").strip()
    if remote not in {"127.0.0.1", "::1"}:
        return jsonify({"error": "forbidden"}), 403

    _shutdown_background_workers()
    return jsonify({"ok": True})


@app.post("/api/admin/environment")
def api_admin_environment() -> Any:
    remote = (request.remote_addr or "").strip()
    if remote not in {"127.0.0.1", "::1"}:
        return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    desired = str(payload.get("environment") or "dev").strip().lower()
    if desired not in {"dev", "prod"}:
        return jsonify({"error": "environment must be dev or prod"}), 400

    _set_active_environment(desired)
    return jsonify({
        "ok": True,
        "environment": _get_active_environment(),
        "site_base_url": _site_base_url(),
    })


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
    _ensure_worker_started()
    app.run(host="127.0.0.1", port=5050, debug=False)
