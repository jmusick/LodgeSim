#!/usr/bin/env python3
"""Simple desktop launcher for website_sim_runner.py."""

from __future__ import annotations

import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import ttk

APP_ROOT = pathlib.Path(__file__).resolve().parent
RUNNER_SCRIPT = APP_ROOT / "website_sim_runner.py"


class RunnerGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("WoWSim Website Runner")
        self.geometry("920x620")
        self.minsize(760, 500)

        self._proc: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[str] = queue.Queue()

        self.environment = tk.StringVar(value="dev")
        self.api_status = tk.StringVar(value="API: checking...")

        self._build_ui()
        self.after(100, self._drain_output)
        self._schedule_connection_check()

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

        ttk.Label(button_row, textvariable=self.api_status).pack(side=tk.RIGHT)

        output_frame = ttk.LabelFrame(root, text="Console", padding=8)
        output_frame.pack(fill=tk.BOTH, expand=True)

        self.output = tk.Text(output_frame, wrap=tk.NONE, font=("Consolas", 10))
        self.output.pack(fill=tk.BOTH, expand=True)

        hint = ttk.Label(
            root,
            text=(
                "Uses SIM_SITE_BASE_URL_DEV/PROD and SIM_RUNNER_KEY_DEV/PROD env vars unless overridden in website_sim_runner.py options."
            ),
        )
        hint.pack(anchor=tk.W, pady=(8, 0))

    def _append(self, line: str) -> None:
        self.output.insert(tk.END, line)
        self.output.see(tk.END)

    def _set_running(self, running: bool) -> None:
        self.run_btn.configure(state=tk.DISABLED if running else tk.NORMAL)

    def _on_environment_change(self) -> None:
        self.api_status.set("API: checking...")
        self._check_connection_async()

    def _schedule_connection_check(self) -> None:
        self._check_connection_async()
        self.after(30000, self._schedule_connection_check)

    def _resolve_api_settings(self) -> tuple[str, str]:
        env_name = self.environment.get().upper()
        base_url = os.getenv(f"SIM_SITE_BASE_URL_{env_name}") or os.getenv("SIM_SITE_BASE_URL") or ""
        runner_key = os.getenv(f"SIM_RUNNER_KEY_{env_name}") or os.getenv("SIM_RUNNER_KEY") or ""
        return base_url.strip(), runner_key.strip()

    def _check_connection_async(self) -> None:
        def worker() -> None:
            msg = self._check_connection_now()
            self.after(0, lambda: self.api_status.set(msg))

        threading.Thread(target=worker, daemon=True).start()

    def _check_connection_now(self) -> str:
        base_url, runner_key = self._resolve_api_settings()
        if not base_url or not runner_key:
            return "API: missing SIM_* env vars"

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
                return f"API: connected ({team_count} team(s))"
        except urllib.error.HTTPError as exc:
            return f"API: HTTP {exc.code}"
        except Exception:
            return "API: unreachable"

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
