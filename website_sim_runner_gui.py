#!/usr/bin/env python3
"""Simple desktop launcher for website_sim_runner.py."""

from __future__ import annotations

import json
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
import urllib.request
from tkinter import ttk

APP_ROOT = pathlib.Path(__file__).resolve().parent
RUNNER_SCRIPT = APP_ROOT / "website_sim_runner.py"
WEBAPP_SCRIPT = APP_ROOT / "webapp.py"


class RunnerGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("WoWSim Website Runner")
        self.geometry("920x620")
        self.minsize(760, 500)

        self._proc: subprocess.Popen[str] | None = None
        self._api_proc: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[str] = queue.Queue()

        self.environment = tk.StringVar(value="dev")
        self.api_status = tk.StringVar(value="checking...")
        self.server_status = tk.StringVar(value="checking...")

        self._server_dot_canvas: tk.Canvas | None = None
        self._server_dot_id: int | None = None
        self._api_dot_canvas: tk.Canvas | None = None
        self._api_dot_id: int | None = None

        self._build_ui()
        self._ensure_api_server_started()
        self._refresh_server_status()
        self.after(100, self._drain_output)
        self._schedule_connection_check()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        header = ttk.Label(root, text="Website Sim Runner", font=("Segoe UI", 14, "bold"))
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

        button_row = ttk.Frame(root)
        button_row.pack(fill=tk.X, pady=(0, 10))

        self.run_btn = ttk.Button(button_row, text="Start", command=self._start_run)
        self.run_btn.pack(side=tk.LEFT)

        status_col = ttk.Frame(button_row)
        status_col.pack(side=tk.RIGHT, anchor=tk.E)

        server_row = ttk.Frame(status_col)
        server_row.pack(anchor=tk.E)
        self._server_dot_canvas = tk.Canvas(server_row, width=10, height=10, highlightthickness=0, bd=0)
        self._server_dot_canvas.pack(side=tk.LEFT, padx=(0, 6))
        self._server_dot_id = self._server_dot_canvas.create_oval(2, 2, 8, 8, fill="#d29922", outline="#d29922")
        ttk.Label(server_row, text="Sim Client API Server:").pack(side=tk.LEFT)
        ttk.Label(server_row, textvariable=self.server_status).pack(side=tk.LEFT, padx=(6, 0))

        api_row = ttk.Frame(status_col)
        api_row.pack(anchor=tk.E)
        self._api_dot_canvas = tk.Canvas(api_row, width=10, height=10, highlightthickness=0, bd=0)
        self._api_dot_canvas.pack(side=tk.LEFT, padx=(0, 6))
        self._api_dot_id = self._api_dot_canvas.create_oval(2, 2, 8, 8, fill="#d29922", outline="#d29922")
        ttk.Label(api_row, text="Website API:").pack(side=tk.LEFT)
        ttk.Label(api_row, textvariable=self.api_status).pack(side=tk.LEFT, padx=(6, 0))

        output_frame = ttk.LabelFrame(root, text="Console", padding=8)
        output_frame.pack(fill=tk.BOTH, expand=True)

        self.output = tk.Text(output_frame, wrap=tk.NONE, font=("Consolas", 10))
        self.output.pack(fill=tk.BOTH, expand=True)

    def _append(self, line: str) -> None:
        self.output.insert(tk.END, line)
        self.output.see(tk.END)

    def _set_running(self, running: bool) -> None:
        self.run_btn.configure(state=tk.DISABLED if running else tk.NORMAL)

    def _on_environment_change(self) -> None:
        self._set_api_status("checking...", "checking")
        self._check_connection_async()

    def _schedule_connection_check(self) -> None:
        self._check_connection_async()
        self.after(30000, self._schedule_connection_check)

    def _refresh_server_status(self) -> None:
        owner = "desktop" if self._api_proc is not None and self._api_proc.poll() is None else "external"
        if self._is_local_webapp_reachable():
            self._set_server_status(f"running ({owner})", "ok")
        else:
            self._set_server_status("stopped", "error")
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
        if not WEBAPP_SCRIPT.exists():
            self._set_server_status(f"missing {WEBAPP_SCRIPT.name}", "error")
            return

        if self._is_local_webapp_reachable():
            self._set_server_status("running (external)", "ok")
            return

        self._set_server_status("starting...", "checking")
        self._api_proc = subprocess.Popen(
            [sys.executable, str(WEBAPP_SCRIPT)],
            cwd=str(APP_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        for _ in range(15):
            if self._is_local_webapp_reachable():
                self._set_server_status("running (desktop)", "ok")
                return
            time.sleep(0.2)

        self._set_server_status("failed to start", "error")

    def _stop_api_server(self) -> None:
        proc = self._api_proc
        if proc is None or proc.poll() is not None:
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
        self._stop_api_server()
        self.destroy()

    def _resolve_api_settings(self) -> tuple[str, str]:
        env_name = self.environment.get().upper()
        base_url = os.getenv(f"SIM_SITE_BASE_URL_{env_name}") or os.getenv("SIM_SITE_BASE_URL") or ""
        runner_key = os.getenv(f"SIM_RUNNER_KEY_{env_name}") or os.getenv("SIM_RUNNER_KEY") or ""
        return base_url.strip(), runner_key.strip()

    def _manual_start_enabled(self) -> bool:
        raw = (os.getenv("WOWSIM_ALLOW_MANUAL_START") or "").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _check_connection_async(self) -> None:
        def worker() -> None:
            msg, state = self._check_connection_now()
            self.after(0, lambda: self._set_api_status(msg, state))

        threading.Thread(target=worker, daemon=True).start()

    def _check_connection_now(self) -> tuple[str, str]:
        base_url, runner_key = self._resolve_api_settings()
        if not base_url or not runner_key:
            return "missing SIM_* env vars", "error"

        url = f"{base_url.rstrip('/')}/api/sim/targets"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "X-Sim-Runner-Key": runner_key},
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                payload = json.loads(raw) if raw.strip() else {}
                teams = payload.get("teams") if isinstance(payload, dict) else None
                team_count = len(teams) if isinstance(teams, list) else 0
                return f"connected ({team_count} team(s))", "ok"
        except urllib.error.HTTPError as exc:
            return f"HTTP {exc.code}", "error"
        except Exception:
            return "unreachable", "error"

    def _build_command(self) -> list[str]:
        python_exe = sys.executable
        command = [
            python_exe,
            str(RUNNER_SCRIPT),
            "--environment",
            self.environment.get(),
            "--config",
            "config.guild.json",
        ]

        return command

    def _start_run(self) -> None:
        if not self._manual_start_enabled():
            self._append(
                "Manual starts are disabled. Trigger sims from HiddenLodgeWebsite. "
                "Set WOWSIM_ALLOW_MANUAL_START=1 to override.\n"
            )
            return

        if self._proc is not None and self._proc.poll() is None:
            self._append("A run is already in progress.\n")
            return

        base_url, runner_key = self._resolve_api_settings()
        if not base_url or not runner_key:
            self._append("Missing SIM_* API settings for selected environment.\n")
            return

        command = self._build_command()

        if not RUNNER_SCRIPT.exists():
            self._append(f"Could not find: {RUNNER_SCRIPT}\n")
            return

        self.output.delete("1.0", tk.END)
        self._append("$ " + " ".join(command) + "\n\n")
        self._set_running(True)

        self._proc = subprocess.Popen(
            command,
            cwd=str(APP_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        threading.Thread(target=self._read_output, daemon=True).start()

    def _read_output(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return

        for line in proc.stdout:
            self._queue.put(line)

        exit_code = proc.wait()
        self._queue.put(f"\nProcess exited with code {exit_code}.\n")
        self._queue.put("@@DONE@@")

    def _drain_output(self) -> None:
        done = False
        try:
            while True:
                item = self._queue.get_nowait()
                if item == "@@DONE@@":
                    done = True
                else:
                    self._append(item)
        except queue.Empty:
            pass

        if done:
            self._set_running(False)
            self._proc = None

        self.after(100, self._drain_output)


def main() -> int:
    app = RunnerGui()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
