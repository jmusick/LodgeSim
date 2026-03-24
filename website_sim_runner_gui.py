#!/usr/bin/env python3
"""Simple desktop launcher for website_sim_runner.py."""

from __future__ import annotations

import json
import ipaddress
import os
import pathlib
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from tkinter import ttk

APP_ROOT = pathlib.Path(__file__).resolve().parent
WEBAPP_SCRIPT = APP_ROOT / "webapp.py"


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
    """Load environment variables from .env file."""
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


WOWSIM_ROOT = _find_wowsim_root()
_load_dotenv(WOWSIM_ROOT / ".env.simrunner.local")
# Also try current working directory (for when running as standalone exe)
_load_dotenv(pathlib.Path.cwd() / ".env.simrunner.local")

WEBAPP_SCRIPT = WOWSIM_ROOT / "webapp.py"
SIM_API_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) WoWSimRunner/1.0"


def _is_local_or_private_host(host: str) -> bool:
    normalized = (host or "").strip().lower().strip("[]")
    if not normalized:
        return False
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    # Single-label hostnames (for example, Ark-Prime) are typically LAN hosts.
    if "." not in normalized:
        return True
    try:
        addr = ipaddress.ip_address(normalized)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return False


def _open_url_with_proxy_fallback(req: urllib.request.Request, timeout: float):
    parsed = urllib.parse.urlparse(req.full_url)
    host = parsed.hostname or ""
    prefer_direct = _is_local_or_private_host(host)
    direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    if prefer_direct:
        try:
            return direct_opener.open(req, timeout=timeout)
        except Exception:
            pass

    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # Corporate/system proxies often return 403/407 for local hostnames.
        if exc.code in {403, 407} and not prefer_direct:
            return direct_opener.open(req, timeout=timeout)
        raise
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        # WinError 10061 here is commonly caused by a dead local proxy.
        if getattr(reason, "winerror", None) != 10061 and not prefer_direct:
            raise

        return direct_opener.open(req, timeout=timeout)


def _windows_subprocess_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}

    kwargs: dict[str, object] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    kwargs["startupinfo"] = startupinfo
    return kwargs


try:
    from webapp import app as flask_app
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False



class RunnerGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("WoWSim Website Runner")
        self.geometry("920x620")
        self.minsize(760, 500)

        self._api_proc: subprocess.Popen[str] | None = None
        self._simc_update_proc: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._simc_update_in_progress = False

        self.environment = tk.StringVar(value="dev")
        self.dev_pc_host = tk.StringVar(value=self._current_dev_host())
        self.api_status = tk.StringVar(value="checking...")
        self.server_status = tk.StringVar(value="checking...")
        self.simc_status = tk.StringVar(value="checking...")
        self.runner_state = tk.StringVar(value="offline")
        self.job_id = tk.StringVar(value="none")
        self.job_status = tk.StringVar(value="idle")
        self.job_progress = tk.StringVar(value="Waiting for a run to start")
        self.job_detail = tk.StringVar(value="-")
        self.job_last_output = tk.StringVar(value="-")
        self.job_progress_pct = tk.IntVar(value=0)

        self._server_dot_canvas: tk.Canvas | None = None
        self._server_dot_id: int | None = None
        self._api_dot_canvas: tk.Canvas | None = None
        self._api_dot_id: int | None = None
        self._simc_dot_canvas: tk.Canvas | None = None
        self._simc_dot_id: int | None = None
        self._simc_check_btn: ttk.Button | None = None
        self._online_btn: ttk.Button | None = None
        self._offline_btn: ttk.Button | None = None
        self.queue_selection = tk.StringVar(value="")
        self.queue_count = tk.StringVar(value="Queued: 0")
        self._queue_combo: ttk.Combobox | None = None
        self._run_now_btn: ttk.Button | None = None
        self._queue_label_to_id: dict[str, str] = {}

        # Track job logs to avoid re-displaying the same lines
        self._job_log_line_counts: dict[str, int] = {}
        self._seen_job_headers: set[str] = set()
        self._job_progress_state: dict[str, tuple[str, int, str, str]] = {}

        self._build_ui()
        self._ensure_api_server_started()
        self._apply_environment_to_local_app()
        self._refresh_runtime_state()
        self._refresh_server_status()
        self._start_simc_auto_update_check()
        self.after(100, self._drain_output)
        self.after(500, self._poll_jobs)
        self._schedule_connection_check()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        header = ttk.Label(root, text="WoWSim Website Runner", font=("Segoe UI", 14, "bold"))
        header.pack(anchor=tk.W)

        controls = ttk.LabelFrame(root, text="Run Options", padding=10)
        controls.pack(fill=tk.X, pady=(10, 10))

        env_row = ttk.Frame(controls)
        env_row.pack(fill=tk.X, pady=4)
        ttk.Label(env_row, text="Environment:", width=18).pack(side=tk.LEFT)
        ttk.Radiobutton(
            env_row,
            text="Dev",
            value="dev",
            variable=self.environment,
            command=self._on_environment_change,
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(
            env_row,
            text="Prod",
            value="prod",
            variable=self.environment,
            command=self._on_environment_change,
        ).pack(side=tk.LEFT)

        dev_host_row = ttk.Frame(controls)
        dev_host_row.pack(fill=tk.X, pady=4)
        ttk.Label(dev_host_row, text="Dev PC Name:", width=18).pack(side=tk.LEFT)
        ttk.Entry(dev_host_row, textvariable=self.dev_pc_host).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(dev_host_row, text="Apply", command=self._apply_dev_host_setting).pack(side=tk.LEFT, padx=(8, 0))

        button_row = ttk.Frame(root)
        button_row.pack(fill=tk.X, pady=(0, 10))

        runner_row = ttk.Frame(button_row)
        runner_row.pack(side=tk.LEFT, anchor=tk.W)
        ttk.Label(runner_row, text="Runner:").pack(side=tk.LEFT)
        ttk.Label(runner_row, textvariable=self.runner_state).pack(side=tk.LEFT, padx=(6, 10))
        self._online_btn = ttk.Button(runner_row, text="Bring Online", command=self._bring_online)
        self._online_btn.pack(side=tk.LEFT)
        self._offline_btn = ttk.Button(runner_row, text="Bring Offline", command=self._bring_offline)
        self._offline_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._offline_btn.configure(state=tk.DISABLED)

        status_col = ttk.Frame(button_row)
        status_col.pack(side=tk.RIGHT, anchor=tk.W)
        status_col.columnconfigure(2, weight=1)

        server_row = ttk.Frame(status_col)
        server_row.grid(row=0, column=0, sticky="w")
        self._server_dot_canvas = tk.Canvas(server_row, width=10, height=10, highlightthickness=0, bd=0)
        self._server_dot_canvas.grid(row=0, column=0, padx=(0, 6), sticky="w")
        self._server_dot_id = self._server_dot_canvas.create_oval(2, 2, 8, 8, fill="#d29922", outline="#d29922")
        ttk.Label(server_row, text="Sim Client API Server:", width=19, anchor="w").grid(row=0, column=1, sticky="w")
        ttk.Label(server_row, textvariable=self.server_status, width=18, anchor="w").grid(row=0, column=2, padx=(6, 0), sticky="w")

        api_row = ttk.Frame(status_col)
        api_row.grid(row=1, column=0, sticky="w")
        self._api_dot_canvas = tk.Canvas(api_row, width=10, height=10, highlightthickness=0, bd=0)
        self._api_dot_canvas.grid(row=0, column=0, padx=(0, 6), sticky="w")
        self._api_dot_id = self._api_dot_canvas.create_oval(2, 2, 8, 8, fill="#d29922", outline="#d29922")
        ttk.Label(api_row, text="Website API:", width=19, anchor="w").grid(row=0, column=1, sticky="w")
        ttk.Label(api_row, textvariable=self.api_status, width=18, anchor="w").grid(row=0, column=2, padx=(6, 0), sticky="w")

        simc_row = ttk.Frame(status_col)
        simc_row.grid(row=2, column=0, sticky="w")
        self._simc_dot_canvas = tk.Canvas(simc_row, width=10, height=10, highlightthickness=0, bd=0)
        self._simc_dot_canvas.grid(row=0, column=0, padx=(0, 6), sticky="w")
        self._simc_dot_id = self._simc_dot_canvas.create_oval(2, 2, 8, 8, fill="#d29922", outline="#d29922")
        ttk.Label(simc_row, text="SimC Auto-Update:", width=19, anchor="w").grid(row=0, column=1, sticky="w")
        ttk.Label(simc_row, textvariable=self.simc_status, width=18, anchor="w").grid(row=0, column=2, padx=(6, 0), sticky="w")
        self._simc_check_btn = ttk.Button(simc_row, text="Check Now", command=self._start_simc_auto_update_check, width=10)
        self._simc_check_btn.grid(row=0, column=3, padx=(4, 0), sticky="w")

        job_frame = ttk.LabelFrame(root, text="Current Job", padding=10)
        job_frame.pack(fill=tk.X, pady=(0, 10))

        job_row_1 = ttk.Frame(job_frame)
        job_row_1.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(job_row_1, text="Job:", width=12).pack(side=tk.LEFT)
        ttk.Label(job_row_1, textvariable=self.job_id).pack(side=tk.LEFT)
        ttk.Label(job_row_1, text="Status:", width=12).pack(side=tk.LEFT, padx=(18, 0))
        ttk.Label(job_row_1, textvariable=self.job_status).pack(side=tk.LEFT)

        job_row_2 = ttk.Frame(job_frame)
        job_row_2.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(job_row_2, text="Progress:", width=12).pack(side=tk.LEFT)
        ttk.Label(job_row_2, textvariable=self.job_progress).pack(side=tk.LEFT)

        self.job_progress_bar = ttk.Progressbar(
            job_frame,
            orient=tk.HORIZONTAL,
            mode="determinate",
            maximum=100,
            variable=self.job_progress_pct,
        )
        self.job_progress_bar.pack(fill=tk.X, pady=(0, 6))

        job_row_3 = ttk.Frame(job_frame)
        job_row_3.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(job_row_3, text="Detail:", width=12).pack(side=tk.LEFT)
        ttk.Label(job_row_3, textvariable=self.job_detail).pack(side=tk.LEFT, fill=tk.X, expand=True)

        job_row_4 = ttk.Frame(job_frame)
        job_row_4.pack(fill=tk.X)
        ttk.Label(job_row_4, text="Last Output:", width=12).pack(side=tk.LEFT)
        ttk.Label(job_row_4, textvariable=self.job_last_output).pack(side=tk.LEFT, fill=tk.X, expand=True)

        queue_frame = ttk.LabelFrame(root, text="Queued Work", padding=10)
        queue_frame.pack(fill=tk.X, pady=(0, 10))

        queue_row = ttk.Frame(queue_frame)
        queue_row.pack(fill=tk.X)
        ttk.Label(queue_row, textvariable=self.queue_count, width=14).pack(side=tk.LEFT)

        self._queue_combo = ttk.Combobox(
            queue_row,
            textvariable=self.queue_selection,
            state="readonly",
            values=[],
        )
        self._queue_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 8))

        self._run_now_btn = ttk.Button(queue_row, text="Run Now", command=self._run_selected_now)
        self._run_now_btn.pack(side=tk.LEFT)
        self._run_now_btn.configure(state=tk.DISABLED)

        output_frame = ttk.LabelFrame(root, text="Console", padding=8)
        output_frame.pack(fill=tk.BOTH, expand=True)

        self.output = tk.Text(output_frame, wrap=tk.CHAR, font=("Consolas", 10))
        output_scroll = ttk.Scrollbar(output_frame, orient=tk.VERTICAL, command=self.output.yview)
        self.output.configure(yscrollcommand=output_scroll.set)
        self.output.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        output_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _append(self, line: str) -> None:
        self.output.insert(tk.END, line)
        self.output.see(tk.END)

    def _set_job_panel(self, job: dict[str, object] | None) -> None:
        if not job:
            self.job_id.set("none")
            self.job_status.set("idle")
            self.job_progress.set("Waiting for a run to start")
            self.job_detail.set("-")
            self.job_last_output.set("-")
            self.job_progress_pct.set(0)
            return

        job_id = str(job.get("id") or "none")[:8]
        status = str(job.get("status") or "unknown")
        progress_pct_raw = job.get("progress_pct")
        progress_pct = int(progress_pct_raw) if isinstance(progress_pct_raw, int) else 0
        # Snap to 100% for any terminal state so the bar always completes
        if status in {"completed", "failed", "canceled", "timed_out"}:
            progress_pct = 100
        progress_stage = str(job.get("progress_stage") or "").strip()
        progress_detail = str(job.get("progress_detail") or "").strip()
        progress_label = str(job.get("progress_label") or "").strip()
        last_line = str(job.get("last_line") or "").strip()

        progress_text = progress_label or progress_stage or status.title()
        if progress_pct > 0:
            progress_text = f"{progress_pct}% - {progress_text}"

        detail_text = progress_detail or last_line or "-"
        last_output_text = last_line or progress_label or "-"

        self.job_id.set(job_id)
        self.job_status.set(status)
        self.job_progress.set(progress_text)
        self.job_detail.set(detail_text)
        self.job_last_output.set(last_output_text)
        self.job_progress_pct.set(progress_pct)

    def _queue_progress_update(self, job: dict[str, object]) -> None:
        job_id = str(job.get("id") or "")
        if not job_id:
            return

        status = str(job.get("status") or "unknown")
        progress_pct_raw = job.get("progress_pct")
        progress_pct = int(progress_pct_raw) if isinstance(progress_pct_raw, int) else 0
        progress_stage = str(job.get("progress_stage") or "").strip()
        progress_detail = str(job.get("progress_detail") or "").strip()
        progress_state = (status, progress_pct, progress_stage, progress_detail)
        previous_state = self._job_progress_state.get(job_id)
        if previous_state == progress_state:
            return

        self._job_progress_state[job_id] = progress_state

        summary_parts = [f"status={status}"]
        if progress_pct:
            summary_parts.append(f"pct={progress_pct}")
        if progress_stage:
            summary_parts.append(f"stage={progress_stage}")
        if progress_detail:
            summary_parts.append(f"detail={progress_detail}")

        self._queue.put(f"[website job {job_id[:8]}] {' | '.join(summary_parts)}\n")

    def _update_queue_panel(self, queued_jobs: list[dict[str, object]]) -> None:
        labels: list[str] = []
        mapping: dict[str, str] = {}

        for idx, job in enumerate(queued_jobs, start=1):
            job_id = str(job.get("id") or "")
            if not job_id:
                continue
            source = str(job.get("source") or "queued")
            qpos = int(job.get("queue_position") or idx)
            label = f"{qpos}. {job_id[:8]} ({source})"
            labels.append(label)
            mapping[label] = job_id

        self._queue_label_to_id = mapping
        self.queue_count.set(f"Queued: {len(labels)}")

        if self._queue_combo is not None:
            self._queue_combo["values"] = labels

        current = self.queue_selection.get().strip()
        if current not in mapping:
            self.queue_selection.set(labels[0] if labels else "")

        if self._run_now_btn is not None:
            self._run_now_btn.configure(state=tk.NORMAL if labels else tk.DISABLED)

    def _run_selected_now(self) -> None:
        selected = self.queue_selection.get().strip()
        job_id = self._queue_label_to_id.get(selected)
        if not job_id:
            return

        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:5050/api/jobs/{job_id}/run-now",
                headers={"Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw.strip() else {}
                self._queue.put(f"[queue] promoted {job_id[:8]} to the front\n")
                _ = data
        except Exception as exc:
            self._queue.put(f"[queue] failed to promote {job_id[:8]}: {exc}\n")

    def _refresh_run_button_state(self) -> None:
        if self._simc_check_btn is not None:
            self._simc_check_btn.configure(state=tk.DISABLED if self._simc_update_in_progress else tk.NORMAL)

    def _simc_auto_update_enabled(self) -> bool:
        raw = (os.getenv("WOWSIM_AUTO_UPDATE_SIMC_ON_LAUNCH") or "1").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _set_simc_status(self, message: str, state: str) -> None:
        self.simc_status.set(message)
        self._set_dot_state(self._simc_dot_canvas, self._simc_dot_id, state)

    def _start_simc_auto_update_check(self) -> None:
        if not self._simc_auto_update_enabled():
            self._set_simc_status("disabled", "warn")
            self._simc_update_in_progress = False
            self._refresh_run_button_state()
            return

        script_path = WOWSIM_ROOT / "update-simc.ps1"
        if not script_path.exists():
            self._set_simc_status("script missing", "error")
            self._simc_update_in_progress = False
            self._refresh_run_button_state()
            return

        self._simc_update_in_progress = True
        self._refresh_run_button_state()
        self._set_simc_status("checking...", "checking")
        self._queue.put("[simc-update] Checking for SimulationCraft nightly updates...\n")

        def worker() -> None:
            cmd = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ]
            try:
                proc = subprocess.Popen(
                    cmd,
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
            except Exception as exc:
                self.after(0, lambda: self._finish_simc_update(f"failed to start ({str(exc)[:40]})", "error"))
                return

            self._simc_update_proc = proc
            try:
                if proc.stdout is not None:
                    for line in proc.stdout:
                        self._queue.put(f"[simc-update] {line}")
                code = proc.wait()
            finally:
                self._simc_update_proc = None

            if code == 0:
                self.after(0, lambda: self._finish_simc_update("up to date", "ok"))
            else:
                self.after(0, lambda: self._finish_simc_update(f"failed (exit {code})", "error"))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_simc_update(self, message: str, state: str) -> None:
        self._simc_update_in_progress = False
        self._set_simc_status(message, state)
        self._refresh_run_button_state()

    def _on_environment_change(self) -> None:
        self._set_api_status("checking...", "checking")
        self._apply_environment_to_local_app()
        self._check_connection_async()

    def _current_dev_host(self) -> str:
        raw = (os.getenv("SIM_SITE_BASE_URL_DEV") or "").strip()
        if not raw:
            return "localhost"
        try:
            parsed = urllib.parse.urlparse(raw)
            host = (parsed.hostname or "").strip()
            return host or "localhost"
        except Exception:
            return "localhost"

    def _apply_dev_host_setting(self) -> None:
        requested_host = (self.dev_pc_host.get() or "").strip()
        if not requested_host:
            self._queue.put("[env] Dev PC name is required\n")
            return

        if "://" in requested_host:
            try:
                requested_host = (urllib.parse.urlparse(requested_host).hostname or "").strip()
            except Exception:
                requested_host = ""

        if not requested_host:
            self._queue.put("[env] Invalid Dev PC name\n")
            return

        try:
            body = json.dumps({"host": requested_host}).encode("utf-8")
            req = urllib.request.Request(
                "http://127.0.0.1:5050/api/admin/settings/dev-host",
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                method="POST",
                data=body,
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw.strip() else {}
                resolved_url = str(data.get("site_base_url_dev") or "").strip()
                if resolved_url:
                    os.environ["SIM_SITE_BASE_URL_DEV"] = resolved_url
                self.dev_pc_host.set(str(data.get("host") or requested_host))
                self._queue.put(f"[env] dev host set to {self.dev_pc_host.get()}\n")
                self._set_api_status("checking...", "checking")
                self._check_connection_async()
        except urllib.error.HTTPError as exc:
            self._queue.put(f"[env] failed to set dev host (HTTP {exc.code})\n")
        except Exception as exc:
            self._queue.put(f"[env] failed to set dev host: {exc}\n")

    def _apply_environment_to_local_app(self) -> None:
        env_value = (self.environment.get() or "dev").strip().lower()
        if env_value not in {"dev", "prod"}:
            env_value = "dev"

        try:
            body = json.dumps({"environment": env_value}).encode("utf-8")
            req = urllib.request.Request(
                "http://127.0.0.1:5050/api/admin/environment",
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                method="POST",
                data=body,
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw.strip() else {}
                selected = str(data.get("environment") or env_value)
                self._queue.put(f"[env] active environment set to {selected}\n")
        except Exception as exc:
            self._queue.put(f"[env] failed to set environment ({env_value}): {exc}\n")

    def _set_runner_state(self, online: bool) -> None:
        self.runner_state.set("online" if online else "offline")
        if self._online_btn is not None:
            self._online_btn.configure(state=tk.DISABLED if online else tk.NORMAL)
        if self._offline_btn is not None:
            self._offline_btn.configure(state=tk.NORMAL if online else tk.DISABLED)

    def _refresh_runtime_state(self) -> None:
        def worker() -> None:
            online = False
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:5050/api/admin/runtime-state",
                    headers={"Accept": "application/json"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    data = json.loads(raw) if raw.strip() else {}
                    online = bool(data.get("online", False))
            except Exception:
                online = False

            self.after(0, lambda: self._set_runner_state(online))

        threading.Thread(target=worker, daemon=True).start()

    def _bring_online(self) -> None:
        def worker() -> None:
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:5050/api/admin/online-start",
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    method="POST",
                    data=b"{}",
                )
                with urllib.request.urlopen(req, timeout=5):
                    pass
                self._queue.put("[runner] brought online\n")
            except urllib.error.HTTPError as exc:
                self._queue.put(f"[runner] failed to bring online (HTTP {exc.code})\n")
            except Exception as exc:
                self._queue.put(f"[runner] failed to bring online: {exc}\n")
            finally:
                self._refresh_runtime_state()

        threading.Thread(target=worker, daemon=True).start()

    def _bring_offline(self) -> None:
        def worker() -> None:
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:5050/api/admin/online-stop",
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    method="POST",
                    data=b"{}",
                )
                with urllib.request.urlopen(req, timeout=5):
                    pass
                self._queue.put("[runner] brought offline\n")
            except urllib.error.HTTPError as exc:
                self._queue.put(f"[runner] failed to bring offline (HTTP {exc.code})\n")
            except Exception as exc:
                self._queue.put(f"[runner] failed to bring offline: {exc}\n")
            finally:
                self._refresh_runtime_state()

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_connection_check(self) -> None:
        self._check_connection_async()
        self._refresh_runtime_state()
        self.after(30000, self._schedule_connection_check)

    def _refresh_server_status(self) -> None:
        if self._is_local_webapp_reachable():
            self._set_server_status("running", "ok")
        else:
            self._set_server_status("not running", "error")
        self.after(3000, self._refresh_server_status)

    def _set_dot_state(self, canvas: tk.Canvas | None, dot_id: int | None, state: str) -> None:
        if canvas is None or dot_id is None:
            return

        color_map = {
            "ok": "#3fb950",
            "checking": "#d29922",
            "warn": "#d29922",
            "error": "#f85149",
        }
        color = color_map.get(state, "#8b949e")
        canvas.itemconfigure(dot_id, fill=color, outline=color)

    def _set_server_status(self, message: str, state: str) -> None:
        self.server_status.set(message)
        self._set_dot_state(self._server_dot_canvas, self._server_dot_id, state)

    def _set_api_status(self, message: str, state: str) -> None:
        self.api_status.set(message)
        self._set_dot_state(self._api_dot_canvas, self._api_dot_id, state)

    def _is_local_webapp_reachable(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 5050), timeout=1.0):
                return True
        except OSError:
            return False

    def _ensure_api_server_started(self) -> None:
        # Check if already reachable
        if self._is_local_webapp_reachable():
            self._set_server_status("running", "ok")
            return

        # Try to start Flask app in-process if available
        if FLASK_AVAILABLE:
            self._set_server_status("starting...", "checking")
            try:
                def run_flask() -> None:
                    # Suppress Flask's default logging
                    import logging
                    log = logging.getLogger('werkzeug')
                    log.setLevel(logging.ERROR)
                    flask_app.run(host='0.0.0.0', port=5050, debug=False, use_reloader=False)
                
                self._api_proc = threading.Thread(target=run_flask, daemon=True)
                self._api_proc.start()
                
                # Wait for server to be reachable
                for _ in range(30):  # 6 seconds with 0.2s intervals
                    time.sleep(0.2)
                    if self._is_local_webapp_reachable():
                        self._set_server_status("running", "ok")
                        return
                
                self._set_server_status("failed to start", "error")
            except Exception as e:
                self._set_server_status(f"error: {str(e)[:30]}", "error")
            return
        
        # Fallback: try subprocess if Flask not directly available
        if getattr(sys, "frozen", False):
            self._set_server_status("embedded API unavailable", "error")
            return

        if not WEBAPP_SCRIPT.exists():
            self._set_server_status(f"missing {WEBAPP_SCRIPT.name}", "error")
            return

        self._set_server_status("starting...", "checking")
        self._api_proc = subprocess.Popen(
            [sys.executable, str(WEBAPP_SCRIPT)],
            cwd=str(WOWSIM_ROOT),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            **_windows_subprocess_kwargs(),
        )

        for _ in range(15):
            if self._is_local_webapp_reachable():
                self._set_server_status("running", "ok")
                return
            time.sleep(0.2)

        self._set_server_status("failed to start", "error")

    def _stop_api_server(self) -> None:
        if self._api_proc is None:
            return
        
        # If it's a Thread (in-process), it's a daemon so it will stop on exit
        if isinstance(self._api_proc, threading.Thread):
            return
        
        # Otherwise it's a subprocess
        proc = self._api_proc
        if proc.poll() is not None:
            return

        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
            return

        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _on_close(self) -> None:
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:5050/api/admin/shutdown",
                headers={"Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2):
                pass
        except Exception:
            pass

        if self._simc_update_proc is not None and self._simc_update_proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(self._simc_update_proc.pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                else:
                    self._simc_update_proc.terminate()
            except Exception:
                pass

        self._stop_api_server()
        self.destroy()

    def _resolve_api_settings(self) -> tuple[str, str]:
        env_name = self.environment.get().upper()
        base_url = os.getenv(f"SIM_SITE_BASE_URL_{env_name}") or os.getenv("SIM_SITE_BASE_URL") or ""
        runner_key = os.getenv(f"SIM_RUNNER_KEY_{env_name}") or os.getenv("SIM_RUNNER_KEY") or ""
        return base_url.strip(), runner_key.strip()

    def _check_connection_async(self) -> None:
        def worker() -> None:
            msg, state = self._check_connection_now()
            self.after(0, lambda: self._set_api_status(msg, state))

        threading.Thread(target=worker, daemon=True).start()

    def _check_connection_now(self) -> tuple[str, str]:
        base_url, runner_key = self._resolve_api_settings()
        if not base_url or not runner_key:
            return "not connected", "error"

        url = f"{base_url.rstrip('/')}/api/sim/targets"
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
            with _open_url_with_proxy_fallback(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                _ = json.loads(raw) if raw.strip() else {}
                return "connected", "ok"
        except urllib.error.HTTPError as exc:
            return f"not connected (HTTP {exc.code})", "error"
        except urllib.error.URLError as exc:
            reason = str(exc.reason) if exc.reason else str(exc)
            short = reason[:40]
            return f"not connected ({short})", "error"
        except Exception as exc:
            short = str(exc)[:40]
            return f"not connected ({short})", "error"

    def _drain_output(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                self._append(item)
        except queue.Empty:
            pass

        self.after(100, self._drain_output)

    def _poll_jobs(self) -> None:
        """Poll the Flask API for active jobs and display their logs."""
        def worker() -> None:
            try:
                with socket.create_connection(("127.0.0.1", 5050), timeout=2.0):
                    pass
            except OSError:
                self.after(2000, self._poll_jobs)
                return

            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:5050/api/jobs",
                    headers={"Accept": "application/json"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    jobs_data = json.loads(raw) if raw.strip() else []
                    if isinstance(jobs_data, list):
                        jobs_list = jobs_data
                    elif isinstance(jobs_data, dict):
                        jobs_list = jobs_data.get("jobs", [])
                    else:
                        jobs_list = []

                    queued_jobs: list[dict[str, object]] = []
                    running_job: dict[str, object] | None = None
                    display_job: dict[str, object] | None = None

                    for job in jobs_list:
                        if not isinstance(job, dict):
                            continue
                        job_id = job.get("id")
                        status = job.get("status")
                        if status == "queued":
                            queued_jobs.append(job)
                        if status in {"running", "canceling"} and running_job is None:
                            running_job = job
                        if not job_id or status in {"completed", "failed", "canceled", "timed_out"}:
                            continue

                        if job_id not in self._seen_job_headers:
                            progress_label = str(job.get("progress_label") or "").strip()
                            header = f"\n[website job {str(job_id)[:8]}] status={status}"
                            if progress_label:
                                header += f" progress={progress_label}"
                            self._queue.put(header + "\n")
                            self._seen_job_headers.add(job_id)

                        self._queue_progress_update(job)

                        # Fetch logs for this job
                        self._fetch_and_display_job_logs(job_id)

                    if running_job is not None:
                        display_job = running_job
                    elif queued_jobs:
                        display_job = queued_jobs[0]

                    queued_jobs.sort(key=lambda j: int(j.get("queue_position") or 999999))
                    self.after(0, lambda: self._set_job_panel(display_job))
                    self.after(0, lambda: self._update_queue_panel(queued_jobs))

            except Exception:
                pass

            self.after(500, self._poll_jobs)

        threading.Thread(target=worker, daemon=True).start()

    def _fetch_and_display_job_logs(self, job_id: str) -> None:
        """Fetch new log lines for a job and display them."""
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:5050/api/jobs/{job_id}/log?tail=1000",
                headers={"Accept": "application/json"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw.strip() else {}
                lines = data.get("lines", [])
                if not isinstance(lines, list):
                    return

                # Track how many lines we've already displayed for this job
                prev_count = self._job_log_line_counts.get(job_id, 0)
                current_count = len(lines)

                # Display only new lines
                if current_count > prev_count:
                    for line in lines[prev_count:]:
                        self._queue.put(line + "\n")

                self._job_log_line_counts[job_id] = current_count

        except Exception:
            pass


def _run_embedded_website_runner() -> int:
    import website_sim_runner

    sys.argv = ["website_sim_runner.py", *sys.argv[2:]]
    return website_sim_runner.main()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--run-website-sim":
        return _run_embedded_website_runner()

    app = RunnerGui()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
