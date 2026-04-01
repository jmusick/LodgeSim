from __future__ import annotations

import pathlib
import sys

DEFAULT_APP_VERSION = "0.0.0-dev"


def _candidate_roots() -> list[pathlib.Path]:
    roots: list[pathlib.Path] = []

    # When frozen, prefer the directory containing the launched executable.
    if getattr(sys, "frozen", False):
        try:
            roots.append(pathlib.Path(sys.executable).resolve().parent)
        except Exception:
            pass

    try:
        roots.append(pathlib.Path(__file__).resolve().parent)
    except Exception:
        pass

    roots.append(pathlib.Path.cwd())

    seen: set[pathlib.Path] = set()
    unique: list[pathlib.Path] = []
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        unique.append(root)
    return unique


def get_app_version(default: str = DEFAULT_APP_VERSION) -> str:
    for root in _candidate_roots():
        version_file = root / "version.txt"
        if not version_file.exists():
            continue
        try:
            value = version_file.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if value:
            return value
    return default


def get_runner_version_tag(single_target: bool = False) -> str:
    suffix = "-single-target" if single_target else ""
    return f"wowsim-website-runner-v{get_app_version()}{suffix}"
