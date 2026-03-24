#!/usr/bin/env python3
"""Local nightly WoW droptimizer runner for SimulationCraft."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html as _html
import itertools
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from typing import Any

# Season-specific bonus-id upgrade mapping used to normalize both equipped
# gear and candidate pools to their highest upgrade tier when requested.
BONUS_ID_MAX_UPGRADE_MAP: dict[int, int] = {
    12779: 12782,
    12785: 12790,
    12788: 12790,
    12789: 12790,
    12787: 12790,
    12793: 12798,
    12794: 12798,
    12795: 12798,
    12796: 12798,
    12801: 12804,
    12802: 12804,
    12803: 12804,
}

UPGRADE_BONUS_RANK: dict[int, int] = {
    12779: 1,
    12782: 2,
    12785: 3,
    12787: 4,
    12788: 5,
    12789: 6,
    12790: 7,
    12793: 8,
    12794: 9,
    12795: 10,
    12796: 11,
    12798: 12,
    12801: 13,
    12802: 14,
    12803: 15,
    12804: 16,
}

# Live-generated candidates can use plain ilevel-based SimC strings instead of
# bonus_id tracks. When we assume fully upgraded candidates, normalize those
# base drop ilvls to the max upgrade step available for that difficulty track.
ILEVEL_MAX_UPGRADE_MAP: dict[int, int] = {
    250: 259,
    263: 272,
    276: 285,
    282: 289,
}

ILEVEL_DIFFICULTY_LABEL: dict[int, str] = {
    250: "LFR",
    263: "Normal",
    276: "Heroic",
    282: "Mythic",
}

SLOT_KEYS = {
    "head",
    "neck",
    "shoulders",
    "back",
    "chest",
    "shirt",
    "tabard",
    "wrist",
    "hands",
    "waist",
    "legs",
    "feet",
    "finger1",
    "finger2",
    "trinket1",
    "trinket2",
    "main_hand",
    "off_hand",
}

SLOT_ALIASES = {
    "shoulder": "shoulders",
    "shoulders": "shoulders",
    "wrist": "wrist",
    "wrists": "wrist",
    "mainhand": "main_hand",
    "main_hand": "main_hand",
    "offhand": "off_hand",
    "off_hand": "off_hand",
}

PROFILE_CLASSES = {
    "warrior",
    "paladin",
    "hunter",
    "rogue",
    "priest",
    "death_knight",
    "deathknight",
    "shaman",
    "mage",
    "warlock",
    "monk",
    "demon_hunter",
    "demonhunter",
    "druid",
    "evoker",
}

PROFILE_TOKEN_ALIASES = {
    "deathknight": "death_knight",
    "demonhunter": "demon_hunter",
    "beastmastery": "beast_mastery",
}


def normalize_slot_name(slot: str) -> str:
    key = slot.strip().lower().replace("-", "_")
    return SLOT_ALIASES.get(key, key)


def normalize_profile_token(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return PROFILE_TOKEN_ALIASES.get(normalized, normalized)


def normalize_candidate_mapping_key(raw_key: str) -> str:
    value = raw_key.strip().lower().replace("/", ":")
    if ":" in value:
        class_name, spec_name = value.split(":", 1)
        class_name = normalize_profile_token(class_name)
        spec_name = normalize_profile_token(spec_name)
        if class_name and spec_name:
            return f"{class_name}:{spec_name}"
    return normalize_profile_token(value)


def extract_profile_class_spec(profile_text: str) -> tuple[str | None, str | None]:
    class_name: str | None = None
    spec_name: str | None = None

    for line in profile_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if spec_name is None and stripped.startswith("spec="):
            spec_name = normalize_profile_token(stripped.split("=", 1)[1])
            continue

        if class_name is None and "=" in stripped:
            left = normalize_profile_token(stripped.split("=", 1)[0])
            if left in PROFILE_CLASSES:
                class_name = left

        if class_name is not None and spec_name is not None:
            break

    return class_name, spec_name


@dataclass
class Config:
    simc_path: str
    base_profile_path: str
    candidates_path: str
    raiders_path: str | None
    armory_url: str | None
    output_dir: str
    mode: str
    iterations: int
    threads: int
    fight_style: str
    additional_options: list[str]
    max_scenarios: int
    staged_pruning: bool
    staged_threshold: int
    assume_fully_upgraded_equipped: bool
    assume_fully_upgraded_candidates: bool
    candidates_by_spec: dict[str, str]
    strict_spec_mapping: bool


@dataclass
class Scenario:
    name: str
    replacements: dict[str, str]


@dataclass
class SimResult:
    scenario_name: str
    dps: float
    replacements: dict[str, str]
    raw_json_path: pathlib.Path
    dps_min: float | None = None
    dps_max: float | None = None
    dps_std_dev: float | None = None


@dataclass
class RaiderSummary:
    raider_name: str
    baseline_dps: float
    best_scenario: str
    best_dps: float
    csv_path: pathlib.Path
    md_path: pathlib.Path
    html_path: pathlib.Path


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _windows_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}

    kwargs: dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    kwargs["startupinfo"] = startupinfo
    return kwargs


def _resolve_config_path(raw_value: str | None, config_dir: pathlib.Path) -> str | None:
    if raw_value is None:
        return None

    candidate = pathlib.Path(raw_value).expanduser()
    if not candidate.is_absolute():
        return str((config_dir / candidate).resolve())

    repo_name = config_dir.name.lower()
    candidate_parts = list(candidate.parts)
    normalized_parts = [part.lower() for part in candidate_parts]
    if repo_name in normalized_parts:
        repo_index = max(index for index, part in enumerate(normalized_parts) if part == repo_name)
        rebased = config_dir.joinpath(*candidate_parts[repo_index + 1 :])

        # Prefer files under the current workspace clone when present, even if
        # an older absolute clone path still exists on disk.
        if rebased.exists() or not candidate.exists():
            return str(rebased)

    if candidate.exists():
        return str(candidate)

    return str(candidate)


def load_config(path: pathlib.Path) -> Config:
    raw = load_json(path)
    if not isinstance(raw, dict):
        raise ValueError("Config JSON must be an object.")
    config_dir = path.resolve().parent
    raw_candidates_by_spec = raw.get("candidates_by_spec", {})
    candidates_by_spec: dict[str, str] = {}
    if isinstance(raw_candidates_by_spec, dict):
        for raw_key, raw_value in raw_candidates_by_spec.items():
            if raw_value is None:
                continue
            key = normalize_candidate_mapping_key(str(raw_key))
            if not key:
                continue
            resolved = _resolve_config_path(str(raw_value), config_dir)
            if resolved:
                candidates_by_spec[key] = resolved

    return Config(
        simc_path=_resolve_config_path(raw["simc_path"], config_dir) or raw["simc_path"],
        base_profile_path=_resolve_config_path(raw["base_profile_path"], config_dir) or raw["base_profile_path"],
        candidates_path=_resolve_config_path(raw["candidates_path"], config_dir) or raw["candidates_path"],
        raiders_path=_resolve_config_path(raw.get("raiders_path"), config_dir),
        armory_url=raw.get("armory_url"),
        output_dir=_resolve_config_path(raw.get("output_dir", "./results"), config_dir) or "./results",
        mode=raw.get("mode", "single_upgrades"),
        iterations=int(raw.get("iterations", 15000)),
        threads=int(raw.get("threads", 8)),
        fight_style=raw.get("fight_style", "Patchwerk"),
        additional_options=list(raw.get("additional_options", [])),
        max_scenarios=int(raw.get("max_scenarios", 500)),
        staged_pruning=bool(raw.get("staged_pruning", True)),
        staged_threshold=int(raw.get("staged_threshold", 24)),
        assume_fully_upgraded_equipped=bool(raw.get("assume_fully_upgraded_equipped", False)),
        assume_fully_upgraded_candidates=bool(raw.get("assume_fully_upgraded_candidates", False)),
        candidates_by_spec=candidates_by_spec,
        strict_spec_mapping=bool(raw.get("strict_spec_mapping", False)),
    )


def _upgrade_bonus_ids_in_simc_item(simc_item: str) -> str:
    m = re.search(r"\bbonus_id=([0-9/]+)", simc_item)
    if not m:
        return simc_item

    raw_ids = [p for p in m.group(1).split("/") if p]
    if not raw_ids:
        return simc_item

    upgraded: list[str] = []
    changed = False
    for part in raw_ids:
        try:
            bonus_id = int(part)
        except ValueError:
            upgraded.append(part)
            continue
        mapped = BONUS_ID_MAX_UPGRADE_MAP.get(bonus_id, bonus_id)
        if mapped != bonus_id:
            changed = True
        upgraded.append(str(mapped))

    if not changed:
        return simc_item

    updated_bonus = "/".join(upgraded)
    return re.sub(r"\bbonus_id=[0-9/]+", f"bonus_id={updated_bonus}", simc_item, count=1)


def _upgrade_equipped_profile(profile: str) -> str:
    out_lines: list[str] = []
    for line in profile.splitlines():
        if "=" not in line:
            out_lines.append(line)
            continue
        key = normalize_slot_name(line.split("=", 1)[0].strip())
        if key in SLOT_KEYS:
            out_lines.append(_upgrade_bonus_ids_in_simc_item(line))
        else:
            out_lines.append(line)
    return "\n".join(out_lines) + "\n"


def _item_id_from_simc(simc_item: str) -> int | None:
    m = re.search(r"\bid=(\d+)", simc_item)
    if not m:
        return None
    return int(m.group(1))


def _candidate_upgrade_score(simc_item: str) -> int:
    m = re.search(r"\bbonus_id=([0-9/]+)", simc_item)
    if not m:
        return 0
    best = 0
    for part in m.group(1).split("/"):
        try:
            bid = int(part)
        except ValueError:
            continue
        best = max(best, UPGRADE_BONUS_RANK.get(bid, 0))
    return best


def _upgrade_ilevel_candidate(simc_item: str, label: str | None) -> tuple[str, str | None]:
    # Live candidate JSON already emits difficulty-specific final ilvls.
    # Applying an additional ilevel upgrade step here inflates outputs.
    return simc_item, label


def _upgrade_and_reduce_candidates(candidates: dict[str, Any]) -> dict[str, Any]:
    slots_raw = candidates.get("slots")
    if not isinstance(slots_raw, dict):
        return candidates

    upgraded_slots: dict[str, list[dict[str, str]]] = {}
    for slot, items in slots_raw.items():
        if not isinstance(items, list):
            continue

        best_by_id: dict[int, tuple[int, dict[str, str]]] = {}
        passthrough: list[dict[str, str]] = []

        for item in items:
            if not isinstance(item, dict) or "simc" not in item:
                continue
            simc_item = str(item["simc"])
            upgraded_simc = _upgrade_bonus_ids_in_simc_item(simc_item)

            updated = dict(item)
            updated_label = str(updated.get("label")) if updated.get("label") is not None else None
            upgraded_simc, upgraded_label = _upgrade_ilevel_candidate(upgraded_simc, updated_label)
            updated["simc"] = upgraded_simc
            if upgraded_label is not None:
                updated["label"] = upgraded_label

            item_id = _item_id_from_simc(upgraded_simc)
            if item_id is None:
                passthrough.append(updated)
                continue

            # Items without bonus_id use ilevel-only SimC strings and represent
            # distinct difficulty variants (e.g. Normal/Heroic/Mythic drops). They
            # are NOT upgrade-tier variants of the same item, so skip deduplication
            # and preserve all of them.
            if not re.search(r"\bbonus_id=", upgraded_simc):
                passthrough.append(updated)
                continue

            score = _candidate_upgrade_score(upgraded_simc)
            current = best_by_id.get(item_id)
            if current is None or score > current[0]:
                best_by_id[item_id] = (score, updated)

        reduced = [entry for _, entry in best_by_id.values()]
        reduced.extend(passthrough)
        upgraded_slots[slot] = reduced

    transformed = dict(candidates)
    transformed["slots"] = upgraded_slots
    return transformed


def load_base_profile(path: pathlib.Path) -> str:
    with path.open("r", encoding="utf-8") as handle:
        return handle.read()


def profile_slots(profile: str) -> set[str]:
    found: set[str] = set()
    for line in profile.splitlines():
        if "=" not in line:
            continue
        key = normalize_slot_name(line.split("=", 1)[0].strip())
        if key in SLOT_KEYS:
            found.add(key)
    return found


def _simc_item_value(raw: str) -> str:
    """Ensure a simc item value has a leading comma when it starts with bare parameters.

    SimC item syntax: ``slot=<name>,key=val,...`` or ``slot=,key=val,...`` (no name).
    Candidates JSON stores values like ``id=12345,bonus_id=...`` without the leading
    comma, which SimC then interprets as a literal item name rather than parameters,
    silently leaving the slot empty.  This function normalises both old (no comma) and
    new (already has comma) formats.
    """
    v = raw.strip()
    if not v or v.startswith(","):
        return v
    # If the first '=' appears before the first ',', the value starts with a key=param
    # pair (e.g. "id=..."), not with an item name.  Prepend the required comma.
    first_eq = v.find("=")
    first_comma = v.find(",")
    if first_eq != -1 and (first_comma == -1 or first_eq < first_comma):
        return "," + v
    return v


def apply_replacements(profile: str, replacements: dict[str, str]) -> str:
    normalized_replacements = {normalize_slot_name(k): _simc_item_value(v) for k, v in replacements.items()}
    lines = profile.splitlines()
    replaced_keys: set[str] = set()

    for idx, line in enumerate(lines):
        if "=" not in line:
            continue
        key, _ = line.split("=", 1)
        slot = normalize_slot_name(key.strip())
        if slot in normalized_replacements:
            lines[idx] = f"{slot}={normalized_replacements[slot]}"
            replaced_keys.add(slot)

    for slot, value in normalized_replacements.items():
        if slot not in replaced_keys:
            lines.append(f"{slot}={value}")

    return "\n".join(lines) + "\n"


def generate_scenarios(candidates: dict[str, Any], mode: str, max_scenarios: int) -> list[Scenario]:
    slots_raw: dict[str, list[dict[str, str]]] = candidates.get("slots", {})
    slots: dict[str, list[dict[str, str]]] = {}
    for slot, items in slots_raw.items():
        norm_slot = normalize_slot_name(slot)
        if not isinstance(items, list):
            continue
        slots.setdefault(norm_slot, []).extend(items)

    if mode == "single_upgrades":
        scenarios: list[Scenario] = []
        for slot, items in slots.items():
            for item in items:
                label = item.get("label", item["simc"])
                replacements: dict[str, str] = {slot: item["simc"]}
                for clear_slot in item.get("clear_slots", []):
                    replacements[normalize_slot_name(clear_slot)] = ""
                scenarios.append(Scenario(name=f"{slot}: {label}", replacements=replacements))
        return scenarios[:max_scenarios]

    if mode == "cartesian":
        active_slots = [(slot, items) for slot, items in slots.items() if items]
        if not active_slots:
            return []

        all_item_lists = [items for _, items in active_slots]
        scenarios = []
        for combo in itertools.product(*all_item_lists):
            replacements: dict[str, str] = {}
            labels: list[str] = []
            for (slot, _), item in zip(active_slots, combo):
                replacements[slot] = item["simc"]
                for clear_slot in item.get("clear_slots", []):
                    replacements[normalize_slot_name(clear_slot)] = ""
                labels.append(item.get("label", slot))
            scenarios.append(Scenario(name=" + ".join(labels), replacements=replacements))
            if len(scenarios) >= max_scenarios:
                break
        return scenarios

    raise ValueError(f"Unsupported mode: {mode}. Use 'single_upgrades' or 'cartesian'.")


def build_command(config: Config, profile_path: pathlib.Path, json_path: pathlib.Path) -> list[str]:
    cmd = [
        config.simc_path,
        str(profile_path),
        f"iterations={config.iterations}",
        f"threads={config.threads}",
        f"fight_style={config.fight_style}",
        f"json2={json_path}",
        "statistics_level=1",
    ]
    cmd.extend(config.additional_options)
    return cmd


def _lookup_path(data: dict[str, Any], path: list[str]) -> float | None:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    if isinstance(cur, (int, float)):
        return float(cur)
    return None


def parse_dps_from_json(payload: dict[str, Any]) -> float | None:
    common_paths = [
        ["sim", "statistics", "raid_dps", "mean"],
        ["sim", "statistics", "dps", "mean"],
        ["statistics", "raid_dps", "mean"],
        ["statistics", "dps", "mean"],
        ["raid_dps", "mean"],
        ["dps", "mean"],
    ]
    for path in common_paths:
        value = _lookup_path(payload, path)
        if value is not None:
            return value

    players = payload.get("sim", {}).get("players", [])
    if isinstance(players, list) and players:
        first = players[0]
        for path in (
            ["collected_data", "dps", "mean"],
            ["dps", "mean"],
            ["collected_data", "raid_dps", "mean"],
        ):
            value = _lookup_path(first, path)
            if value is not None:
                return value

    return None


def parse_dps_from_stdout(stdout: str) -> float | None:
    patterns = [
        r"DPS\s*=\s*([0-9]+(?:\.[0-9]+)?)",
        r"mean\s*=\s*([0-9]+(?:\.[0-9]+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, stdout)
        if match:
            return float(match.group(1))
    return None


def parse_stats_from_json(payload: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    """Return (dps_min, dps_max, dps_std_dev) from a SimC json2 output."""
    players = payload.get("sim", {}).get("players", [])
    dps_obj: Any = None
    if isinstance(players, list) and players:
        dps_obj = players[0].get("collected_data", {}).get("dps")
    if not isinstance(dps_obj, dict):
        stats = payload.get("sim", {}).get("statistics", {})
        dps_obj = stats.get("raid_dps") or stats.get("dps")
    if isinstance(dps_obj, dict):
        lo = dps_obj.get("min")
        hi = dps_obj.get("max")
        sd = dps_obj.get("std_dev") or dps_obj.get("std_deviation")
        return (
            float(lo) if lo is not None else None,
            float(hi) if hi is not None else None,
            float(sd) if sd is not None else None,
        )
    return None, None, None


def slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", text).strip("_").lower() or "entry"


def parse_armory_url(armory_url: str) -> tuple[str, str, str]:
    parsed = urllib.parse.urlparse(armory_url)
    parts = [p for p in parsed.path.split("/") if p]

    if "character" not in parts:
        raise ValueError(
            "Armory URL format not recognized. Expected path segment 'character'."
        )

    idx = parts.index("character")
    tail = parts[idx + 1 :]
    if len(tail) < 3:
        raise ValueError(
            "Armory URL format not recognized. Expected: "
            ".../character/<region>/<realm>/<name>"
        )

    region = tail[0].lower()
    realm = tail[1].lower()
    name = urllib.parse.unquote(tail[2]).lower()
    return region, realm, name


def export_profile_from_armory(
    config: Config,
    armory_url: str,
    out_profile_path: pathlib.Path,
) -> tuple[pathlib.Path, str]:
    region, realm, name = parse_armory_url(armory_url)
    out_profile_path.parent.mkdir(parents=True, exist_ok=True)

    armory_ref = f"{region},{realm},{name}"
    cmd = [
        config.simc_path,
        f"armory={armory_ref}",
        f"save={out_profile_path}",
    ]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **_windows_subprocess_kwargs(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "SimulationCraft armory import failed.\n"
            f"Armory: {armory_ref}\n"
            f"STDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}"
        )

    if not out_profile_path.exists():
        raise RuntimeError(
            "SimulationCraft armory import completed but no profile was saved at "
            f"{out_profile_path}"
        )

    return out_profile_path, name


def run_sim(config: Config, profile_text: str, scenario: Scenario, out_dir: pathlib.Path) -> SimResult:
    slug = slugify(scenario.name)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    with tempfile.TemporaryDirectory(prefix="simc_run_") as tmp:
        tmp_path = pathlib.Path(tmp)
        simc_profile = tmp_path / "input.simc"
        json_path = out_dir / f"{ts}_{slug}.json"

        simc_profile.write_text(profile_text, encoding="utf-8")
        cmd = build_command(config, simc_profile, json_path)

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **_windows_subprocess_kwargs(),
        )

        if proc.returncode != 0:
            raise RuntimeError(
                "SimulationCraft failed for scenario "
                f"'{scenario.name}' with code {proc.returncode}.\n"
                f"STDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}"
            )

    dps: float | None = None
    dps_min: float | None = None
    dps_max: float | None = None
    dps_std_dev: float | None = None
    if json_path.exists():
        payload = load_json(json_path)
        dps = parse_dps_from_json(payload)
        dps_min, dps_max, dps_std_dev = parse_stats_from_json(payload)

    if dps is None:
        dps = parse_dps_from_stdout(proc.stdout)

    if dps is None:
        raise RuntimeError(
            f"Could not parse DPS for scenario '{scenario.name}'. "
            f"Check JSON at: {json_path} and SimulationCraft output."
        )

    return SimResult(
        scenario_name=scenario.name,
        dps=dps,
        replacements=scenario.replacements,
        raw_json_path=json_path,
        dps_min=dps_min,
        dps_max=dps_max,
        dps_std_dev=dps_std_dev,
    )


def _emit_progress(pct: int, stage: str, detail: str) -> None:
    pct = max(0, min(100, int(pct)))
    print(f"@@PROGRESS@@ pct={pct} stage={stage} detail={detail}", flush=True)


# ---------------------------------------------------------------------------
# HTML report helpers
# ---------------------------------------------------------------------------

def _simc_item_id(simc_str: str) -> str | None:
    m = re.search(r"\bid=(\d+)", simc_str)
    return m.group(1) if m else None


def _simc_bonus_colon(simc_str: str) -> str:
    m = re.search(r"bonus_id=([0-9/]+)", simc_str)
    return m.group(1).replace("/", ":") if m else ""


def _split_scenario_name(scenario_name: str) -> tuple[str, str]:
    """'chest: Item Name 272 - Raid - Boss'  -> ('chest', 'Item Name 272 - Raid - Boss')"""
    if ": " in scenario_name:
        slot, _, label = scenario_name.partition(": ")
        return slot.strip(), label.strip()
    return "", scenario_name


def _parse_item_label(label: str) -> tuple[str, str, str]:
    """'Item Name 272 - Raid - Boss'  -> (item_name, ilvl, source)"""
    m = re.match(r"^(.*?)\s+(\d{3})\s+-\s+(.+)$", label)
    if m:
        return m.group(1).strip(), m.group(2), m.group(3).strip()
    return label, "", ""


def _dist_svg(
    mean: float,
    dps_min: float | None,
    dps_max: float | None,
    dps_std_dev: float | None,
    global_min: float,
    global_max: float,
    width: int = 180,
    height: int = 20,
) -> str:
    span = max(global_max - global_min, 1.0)

    def px(v: float) -> int:
        return max(0, min(width, int((v - global_min) / span * width)))

    me = px(mean)
    svg_parts = [
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">',
        f'<rect x="0" y="8" width="{width}" height="4" fill="#2a2a3e" rx="2"/>',
    ]
    if dps_min is not None and dps_max is not None:
        lo, hi = px(dps_min), px(dps_max)
        svg_parts.append(
            f'<rect x="{lo}" y="9" width="{max(1, hi - lo)}" height="2" fill="#444" rx="1"/>'
        )
    if dps_std_dev is not None:
        slo = px(mean - dps_std_dev)
        shi = px(mean + dps_std_dev)
        svg_parts.append(
            f'<rect x="{slo}" y="6" width="{max(1, shi - slo)}" height="8" fill="#5c7cfa" rx="2" opacity="0.65"/>'
        )
    svg_parts.append(
        f'<circle cx="{me}" cy="10" r="4" fill="#c8962e" stroke="#000" stroke-width="1"/>'
    )
    svg_parts.append("</svg>")
    return "".join(svg_parts)


_HTML_CSS = """\
:root{--bg:#0e0e1a;--card:#1a1a2e;--hdr:#12121f;--gold:#c8962e;--gold-lt:#f0c060;
--txt:#d4d4d4;--dim:#888;--pos:#4caf92;--neu:#ffa726;--neg:#ef5350;
--bdr:#282840;--row-alt:#1c1c2c;--row-hov:#22223a;--font:'Segoe UI',system-ui,sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:var(--font);font-size:14px;line-height:1.4}
header{background:var(--hdr);border-bottom:2px solid var(--gold);padding:18px 28px}
.hdr-top{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
h1{font-size:26px;font-weight:700;color:var(--gold-lt);letter-spacing:2px;text-transform:uppercase;
text-shadow:0 0 18px rgba(200,150,46,.4)}
.sim-meta{font-size:12px;color:var(--dim)}
.stat-row{display:flex;gap:12px;margin-top:12px;flex-wrap:wrap}
.stat-card{background:var(--card);border:1px solid var(--bdr);border-radius:6px;padding:8px 16px;text-align:center;min-width:110px}
.stat-value{font-size:19px;font-weight:700;color:var(--gold-lt)}
.stat-value.pos{color:var(--pos)}
.stat-label{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-top:2px}
main{padding:20px 28px}
.controls{display:flex;gap:10px;margin-bottom:14px;align-items:center;flex-wrap:wrap}
.controls input{background:var(--card);border:1px solid var(--bdr);border-radius:4px;
color:var(--txt);padding:6px 12px;font-size:13px;width:260px;outline:none}
.controls input:focus{border-color:var(--gold)}
.info{font-size:12px;color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:13px}
thead tr{background:var(--hdr);border-bottom:2px solid var(--gold)}
th{padding:9px 10px;text-align:left;font-weight:600;text-transform:uppercase;
font-size:11px;letter-spacing:.7px;color:var(--gold);white-space:nowrap}
th.sortable{cursor:pointer}
th.sortable:hover{color:var(--gold-lt)}
tbody tr{border-bottom:1px solid var(--bdr);transition:background .1s}
tbody tr:nth-child(even){background:var(--row-alt)}
tbody tr:hover{background:var(--row-hov)}
td{padding:8px 10px;vertical-align:middle}
td.rank{color:var(--dim);font-size:11px;width:32px;text-align:right;padding-right:4px}
td.item a{color:inherit;text-decoration:none}
td.item a:hover{color:var(--gold-lt)}
td.ilvl{color:var(--dim);text-align:center;width:44px;font-size:12px}
td.slot{color:var(--dim);white-space:nowrap;width:84px;font-size:12px}
td.source{color:var(--dim);font-size:11px;max-width:210px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
td.dps{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap;
color:#90caf9;width:84px}
td.delta{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap;font-weight:600;width:78px}
td.pct{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap;width:66px}
.r-pos strong td.delta,.r-pos td.delta,.r-pos td.pct{color:var(--pos)}
.r-neu td.delta,.r-neu td.pct{color:var(--neu)}
.r-neg td.delta,.r-neg td.pct{color:var(--neg)}
td.bar-cell{width:110px;padding-right:6px}
.bar-track{background:#1e1e30;border-radius:3px;height:7px;overflow:hidden}
.bar-fill{height:100%;border-radius:3px;background:linear-gradient(90deg,#1976d2,#4caf92)}
.bar-neg{background:var(--neg)}
td.dist{width:184px}
tr.baseline-row{border-top:2px solid #5c7cfa!important;border-bottom:2px solid #5c7cfa!important}
tr.baseline-row td{color:#90caf9}
tr.baseline-row td.item{font-weight:600}
tr.hidden{display:none}
"""

_HTML_JS = """\
let _sortCol=5,_sortDir=-1;
function sortTable(col){
  if(_sortCol===col){_sortDir*=-1}else{_sortCol=col;_sortDir=-1}
  const tb=document.getElementById('rtb');
  const rows=[...tb.querySelectorAll('tr[data-dps]')];
  const base=tb.querySelector('.baseline-row');
  rows.sort((a,b)=>(_sortDir*(parseFloat(a.dataset['c'+col]||0)-parseFloat(b.dataset['c'+col]||0))));
  rows.forEach(r=>tb.appendChild(r));
  if(base)tb.appendChild(base);
  rows.forEach((r,i)=>r.querySelector('.rank').textContent=i+1);
  document.querySelectorAll('th.sortable').forEach((th,i)=>{
    const base=th.dataset.label;
    th.textContent=base+(th.dataset.col==col?((_sortDir===-1)?'\u00a0\u25bc':'\u00a0\u25b2'):'');
  });
}
function filterTable(){
  const q=document.getElementById('fi').value.toLowerCase();
  let vis=0;
  document.querySelectorAll('#rtb tr[data-dps]').forEach(r=>{
    const t=(r.querySelector('.item')||{}).textContent+' '
           +(r.querySelector('.slot')||{}).textContent+' '
           +(r.querySelector('.source')||{}).textContent;
    const show=!q||t.toLowerCase().includes(q);
    r.classList.toggle('hidden',!show);
    if(show)vis++;
  });
  document.getElementById('fc').textContent=vis+' results';
}
window.addEventListener('DOMContentLoaded',()=>sortTable(5));
"""


def write_html_report(
    results: list[SimResult],
    baseline: SimResult,
    out_dir: pathlib.Path,
    label: str = "",
    fight_style: str = "",
) -> pathlib.Path:
    html_path = out_dir / "droptimizer_report.html"
    baseline_dps = baseline.dps
    ranked = sorted(results, key=lambda r: r.dps, reverse=True)

    # Compute global DPS range for normalizing all distribution bars.
    all_dps: list[float] = [baseline_dps]
    for r in ranked:
        all_dps.append(r.dps)
        if r.dps_min is not None:
            all_dps.append(r.dps_min)
        if r.dps_max is not None:
            all_dps.append(r.dps_max)
    if baseline.dps_min is not None:
        all_dps.append(baseline.dps_min)
    if baseline.dps_max is not None:
        all_dps.append(baseline.dps_max)
    g_min = min(all_dps) * 0.995
    g_max = max(all_dps) * 1.005

    max_positive_delta = max((r.dps - baseline_dps for r in ranked if r.dps > baseline_dps), default=1.0)
    top = ranked[0] if ranked else None
    top_gain = (top.dps - baseline_dps) if top else 0.0
    top_pct = (top_gain / baseline_dps * 100.0) if baseline_dps and top else 0.0
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    def row_html(idx: int, r: SimResult) -> str:
        delta = r.dps - baseline_dps
        pct = (delta / baseline_dps * 100.0) if baseline_dps else 0.0
        slot, lbl = _split_scenario_name(r.scenario_name)
        item_name, ilvl, source = _parse_item_label(lbl)

        item_id = _simc_item_id(r.replacements.get(slot, ""))
        bonus = _simc_bonus_colon(r.replacements.get(slot, ""))
        if item_id:
            wh_url = f"https://www.wowhead.com/item={item_id}"
            if bonus:
                wh_url += f"?bonus={bonus}"
            item_link = f'<a href="{wh_url}" data-wh-rename-link="false">{_html.escape(item_name)}</a>'
        else:
            item_link = _html.escape(item_name)

        if delta >= max_positive_delta * 0.4:
            row_cls = "r-pos"
        elif delta > 0:
            row_cls = "r-pos"
        elif delta >= -200:
            row_cls = "r-neu"
        else:
            row_cls = "r-neg"

        bar_w = int(max(0.0, delta / max_positive_delta) * 100) if max_positive_delta > 0 else 0
        bar_cls = "bar-neg" if delta < 0 else ""
        sign = "+" if delta >= 0 else ""

        dist = _dist_svg(r.dps, r.dps_min, r.dps_max, r.dps_std_dev, g_min, g_max)

        return (
            f'<tr class="{row_cls}" data-dps="{r.dps:.2f}"'
            f' data-c5="{r.dps:.2f}" data-c6="{delta:.2f}" data-c7="{pct:.4f}"'
            f' data-c0="{idx}">'
            f'<td class="rank">{idx}</td>'
            f'<td class="item">{item_link}</td>'
            f'<td class="ilvl">{_html.escape(ilvl)}</td>'
            f'<td class="slot">{_html.escape(slot.replace("_"," ").title())}</td>'
            f'<td class="source" title="{_html.escape(source)}">{_html.escape(source)}</td>'
            f'<td class="dps">{r.dps:,.1f}</td>'
            f'<td class="delta">{sign}{delta:,.1f}</td>'
            f'<td class="pct">{sign}{pct:.2f}%</td>'
            f'<td class="bar-cell"><div class="bar-track"><div class="bar-fill {bar_cls}" style="width:{bar_w}%"></div></div></td>'
            f'<td class="dist">{dist}</td>'
            f"</tr>"
        )

    rows = "\n".join(row_html(i, r) for i, r in enumerate(ranked, 1))

    base_dist = _dist_svg(baseline.dps, baseline.dps_min, baseline.dps_max, baseline.dps_std_dev, g_min, g_max)
    baseline_row = (
        f'<tr class="baseline-row">'
        f'<td class="rank">—</td>'
        f'<td class="item" colspan="4"><strong>Equipped (Baseline)</strong></td>'
        f'<td class="dps">{baseline_dps:,.1f}</td>'
        f'<td class="delta">—</td><td class="pct">—</td>'
        f'<td class="bar-cell"></td>'
        f'<td class="dist">{base_dist}</td>'
        f"</tr>"
    )

    title_esc = _html.escape(f"Droptimizer — {label}" if label else "Droptimizer")
    label_esc = _html.escape(label.upper() if label else "LOCAL SIM")

    content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{title_esc}</title>
<script>const whTooltips={{colorLinks:true,iconizeLinks:true,renameLinks:false}};</script>
<script src="https://wow.zamimg.com/js/tooltips.js"></script>
<style>{_HTML_CSS}</style>
</head>
<body>
<header>
  <div class="hdr-top">
    <h1>{label_esc}</h1>
    <span class="sim-meta">{_html.escape(fight_style)} &bull; {_html.escape(ts)} &bull; Local SimulationCraft</span>
  </div>
  <div class="stat-row">
    <div class="stat-card"><div class="stat-value">{baseline_dps:,.0f}</div><div class="stat-label">Baseline DPS</div></div>
    <div class="stat-card"><div class="stat-value pos">+{top_gain:,.0f}</div><div class="stat-label">Best Upgrade</div></div>
    <div class="stat-card"><div class="stat-value pos">+{top_pct:.1f}%</div><div class="stat-label">Best % Gain</div></div>
    <div class="stat-card"><div class="stat-value">{len(ranked)}</div><div class="stat-label">Scenarios</div></div>
  </div>
</header>
<main>
  <div class="controls">
    <input id="fi" type="text" placeholder="Filter by name, slot, source…" oninput="filterTable()">
    <span class="info" id="fc">{len(ranked)} results</span>
  </div>
  <table>
    <thead>
      <tr>
        <th class="sortable" data-col="0" data-label="#" onclick="sortTable(0)">#</th>
        <th>Item</th>
        <th>iLvl</th>
        <th>Slot</th>
        <th>Source</th>
        <th class="sortable" data-col="5" data-label="DPS" onclick="sortTable(5)">DPS ▼</th>
        <th class="sortable" data-col="6" data-label="Delta" onclick="sortTable(6)">Delta ▼</th>
        <th class="sortable" data-col="7" data-label="% Gain" onclick="sortTable(7)">% Gain</th>
        <th colspan="2">Distribution (±1σ)</th>
      </tr>
    </thead>
    <tbody id="rtb">
{rows}
{baseline_row}
    </tbody>
  </table>
</main>
<script>{_HTML_JS}</script>
</body>
</html>"""

    html_path.write_text(content, encoding="utf-8")
    return html_path


def write_reports(
    results: list[SimResult],
    baseline: "SimResult | float",
    out_dir: pathlib.Path,
    label: str = "",
    fight_style: str = "",
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    # Accept either a SimResult (preferred) or a bare float for back-compat.
    if isinstance(baseline, (int, float)):
        baseline_dps = float(baseline)
        baseline_result: SimResult | None = None
    else:
        baseline_dps = baseline.dps
        baseline_result = baseline

    ranked = sorted(results, key=lambda r: r.dps, reverse=True)
    csv_path = out_dir / "droptimizer_results.csv"
    md_path = out_dir / "droptimizer_results.md"

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["rank", "scenario", "dps", "delta", "pct_gain", "replacements", "json_path"])
        for idx, row in enumerate(ranked, start=1):
            delta = row.dps - baseline_dps
            pct = (delta / baseline_dps * 100.0) if baseline_dps else 0.0
            writer.writerow(
                [
                    idx,
                    row.scenario_name,
                    f"{row.dps:.2f}",
                    f"{delta:.2f}",
                    f"{pct:.4f}",
                    json.dumps(row.replacements, separators=(",", ":")),
                    str(row.raw_json_path),
                ]
            )

    with md_path.open("w", encoding="utf-8") as handle:
        handle.write("# Local Droptimizer Results\n\n")
        handle.write(f"Baseline DPS: **{baseline_dps:.2f}**\n\n")
        handle.write("| Rank | Scenario | DPS | Delta | % Gain |\n")
        handle.write("|---:|---|---:|---:|---:|\n")
        for idx, row in enumerate(ranked, start=1):
            delta = row.dps - baseline_dps
            pct = (delta / baseline_dps * 100.0) if baseline_dps else 0.0
            handle.write(
                f"| {idx} | {row.scenario_name} | {row.dps:.2f} | {delta:.2f} | {pct:.4f}% |\n"
            )

    if baseline_result is not None:
        html_path = write_html_report(
            results, baseline_result, out_dir, label=label, fight_style=fight_style
        )
    else:
        # Synthesize a minimal SimResult for the baseline so the HTML still works.
        dummy = SimResult(
            scenario_name="baseline",
            dps=baseline_dps,
            replacements={},
            raw_json_path=out_dir,
        )
        html_path = write_html_report(results, dummy, out_dir, label=label, fight_style=fight_style)

    return csv_path, md_path, html_path


def load_raiders(path: pathlib.Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise ValueError("Raiders JSON must be an object with a 'raiders' array.")
    raiders = payload.get("raiders")
    if not isinstance(raiders, list) or not raiders:
        raise ValueError("Raiders JSON must contain a non-empty 'raiders' array.")
    return raiders


def run_droptimizer_for_profile(
    config: Config,
    profile_path: pathlib.Path,
    candidates_path: pathlib.Path,
    out_dir: pathlib.Path,
    label: str,
) -> RaiderSummary:
    out_dir.mkdir(parents=True, exist_ok=True)
    base_profile = load_base_profile(profile_path)
    if config.assume_fully_upgraded_equipped:
        base_profile = _upgrade_equipped_profile(base_profile)

    candidates = load_json(candidates_path)
    if not isinstance(candidates, dict):
        raise ValueError(f"Candidates JSON for '{label}' must be an object.")
    if config.assume_fully_upgraded_candidates:
        candidates = _upgrade_and_reduce_candidates(candidates)

    existing_slots = profile_slots(base_profile)
    candidate_slots = [normalize_slot_name(slot) for slot in candidates.get("slots", {}).keys()]
    missing = [slot for slot in candidate_slots if slot not in existing_slots]
    if missing:
        print(
            f"[{label}] Warning: these slots were not found in base profile and will be appended:",
            ", ".join(sorted(missing)),
            file=sys.stderr,
        )

    scenarios = generate_scenarios(candidates, config.mode, config.max_scenarios)
    if not scenarios:
        raise RuntimeError(f"[{label}] No scenarios generated. Check candidates JSON.")

    print(f"[{label}] Running baseline + {len(scenarios)} scenario(s)...")
    _emit_progress(2, "Baseline", f"{label} baseline")
    print(f"[{label}] [baseline] Starting baseline simulation...")
    baseline = run_sim(config, base_profile, Scenario(name="baseline", replacements={}), out_dir)
    print(f"[{label}] [baseline] Complete -> {baseline.dps:.2f} DPS")

    staged_enabled = (
        config.staged_pruning
        and config.mode == "single_upgrades"
        and len(scenarios) >= config.staged_threshold
    )

    results: list[SimResult] = []
    if not staged_enabled:
        for idx, scenario in enumerate(scenarios, start=1):
            _emit_progress(2 + int((idx / len(scenarios)) * 94), "Scenarios", f"{label} {idx}/{len(scenarios)}")
            print(f"[{label}] [{idx}/{len(scenarios)}] Starting {scenario.name}")
            patched_profile = apply_replacements(base_profile, scenario.replacements)
            result = run_sim(config, patched_profile, scenario, out_dir)
            results.append(result)
            print(f"[{label}] [{idx}/{len(scenarios)}] {scenario.name} -> {result.dps:.2f} DPS")
    else:
        stages = [
            ("Low", 0.20, 0.50, 20),
            ("Medium", 0.50, 0.35, 8),
            ("High", 1.00, 1.00, 1),
        ]
        remaining = scenarios
        print(f"[{label}] Staged pruning enabled for {len(scenarios)} scenarios.")
        for s_idx, (stage_name, iter_scale, keep_ratio, min_keep) in enumerate(stages, start=1):
            stage_results: list[SimResult] = []
            stage_total = len(remaining)
            print(f"[{label}] Stage {s_idx}/{len(stages)} ({stage_name}) running {stage_total} scenarios...")
            for idx, scenario in enumerate(remaining, start=1):
                span_start = 2 + (s_idx - 1) * 30
                pct = span_start + int((idx / stage_total) * 28)
                _emit_progress(pct, f"{stage_name}", f"{label} {idx}/{stage_total}")
                print(f"[{label}] [{stage_name} {idx}/{stage_total}] Starting {scenario.name}")
                patched_profile = apply_replacements(base_profile, scenario.replacements)
                stage_config = Config(
                    simc_path=config.simc_path,
                    base_profile_path=config.base_profile_path,
                    candidates_path=config.candidates_path,
                    raiders_path=config.raiders_path,
                    armory_url=config.armory_url,
                    output_dir=config.output_dir,
                    mode=config.mode,
                    iterations=max(100, int(config.iterations * iter_scale)),
                    threads=config.threads,
                    fight_style=config.fight_style,
                    additional_options=config.additional_options,
                    max_scenarios=config.max_scenarios,
                    staged_pruning=config.staged_pruning,
                    staged_threshold=config.staged_threshold,
                    assume_fully_upgraded_equipped=config.assume_fully_upgraded_equipped,
                    assume_fully_upgraded_candidates=config.assume_fully_upgraded_candidates,
                    candidates_by_spec=config.candidates_by_spec,
                    strict_spec_mapping=config.strict_spec_mapping,
                )
                result = run_sim(stage_config, patched_profile, scenario, out_dir)
                stage_results.append(result)
                print(f"[{label}] [{stage_name} {idx}/{stage_total}] {scenario.name} -> {result.dps:.2f} DPS")

            if s_idx == len(stages):
                results = sorted(stage_results, key=lambda r: r.dps, reverse=True)
                break

            keep_n = max(min_keep, int(len(stage_results) * keep_ratio))
            keep_n = min(keep_n, len(stage_results))
            kept_names = {
                r.scenario_name for r in sorted(stage_results, key=lambda r: r.dps, reverse=True)[:keep_n]
            }
            remaining = [s for s in remaining if s.name in kept_names]
            print(f"[{label}] Stage {stage_name} kept {len(remaining)}/{len(stage_results)} scenarios.")

    csv_path, md_path, html_path = write_reports(
        results, baseline, out_dir, label=label, fight_style=config.fight_style
    )
    best = max(results, key=lambda row: row.dps)

    print(f"[{label}] Done. Baseline: {baseline.dps:.2f}. Top: {best.scenario_name} ({best.dps:.2f}).")
    print(f"[{label}] HTML report: {html_path}")

    return RaiderSummary(
        raider_name=label,
        baseline_dps=baseline.dps,
        best_scenario=best.scenario_name,
        best_dps=best.dps,
        csv_path=csv_path,
        md_path=md_path,
        html_path=html_path,
    )


def write_batch_summary(summaries: list[RaiderSummary], out_dir: pathlib.Path) -> pathlib.Path:
    path = out_dir / "batch_summary.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["raider", "baseline_dps", "top_scenario", "top_dps", "gain", "csv_path", "md_path", "html_path"])
        for row in summaries:
            gain = row.best_dps - row.baseline_dps
            writer.writerow(
                [
                    row.raider_name,
                    f"{row.baseline_dps:.2f}",
                    row.best_scenario,
                    f"{row.best_dps:.2f}",
                    f"{gain:.2f}",
                    str(row.csv_path),
                    str(row.md_path),
                    str(row.html_path),
                ]
            )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local WoW droptimizer simulations with SimulationCraft.")
    parser.add_argument("--config", default="config.json", help="Path to config JSON file.")
    parser.add_argument("--raiders", default=None, help="Optional path to raiders JSON list for batch mode.")
    parser.add_argument(
        "--armory-url",
        default=None,
        help="Optional Blizzard Armory character URL to import profile from before simming.",
    )
    parser.add_argument(
        "--assume-fully-upgraded",
        action="store_true",
        help=(
            "Assume equipped gear and candidates are fully upgraded. "
            "Normalizes known upgrade bonus_ids to max and keeps best candidate tier per item id."
        ),
    )
    args = parser.parse_args()

    config_path = pathlib.Path(args.config).resolve()
    config = load_config(config_path)
    if args.assume_fully_upgraded:
        config.assume_fully_upgraded_equipped = True
        config.assume_fully_upgraded_candidates = True

    out_dir = pathlib.Path(config.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    raiders_arg = args.raiders or config.raiders_path
    if raiders_arg:
        raiders_path = pathlib.Path(raiders_arg).resolve()
        raiders = load_raiders(raiders_path)
        summaries: list[RaiderSummary] = []

        for idx, raider in enumerate(raiders, start=1):
            if not isinstance(raider, dict):
                raise ValueError(f"Raider entry #{idx} must be an object.")
            name = str(raider.get("name", f"raider_{idx}"))
            profile_raw = raider.get("profile_path")
            armory_raw = raider.get("armory_url")

            if profile_raw:
                profile_path = pathlib.Path(str(profile_raw)).resolve()
            elif armory_raw:
                imported_path = out_dir / "imported_profiles" / f"{slugify(name)}.simc"
                profile_path, imported_name = export_profile_from_armory(
                    config=config,
                    armory_url=str(armory_raw),
                    out_profile_path=imported_path,
                )
                if "name" not in raider:
                    name = imported_name
            else:
                raise ValueError(
                    f"Raider '{name}' is missing required field 'profile_path' or 'armory_url'."
                )

            candidates_path = pathlib.Path(
                str(raider.get("candidates_path", config.candidates_path))
            ).resolve()
            raider_out = out_dir / slugify(name)

            summaries.append(
                run_droptimizer_for_profile(config, profile_path, candidates_path, raider_out, name)
            )

        summary_path = write_batch_summary(summaries, out_dir)
        print("\nBatch complete.")
        print(f"Raiders processed: {len(summaries)}")
        print(f"Summary CSV: {summary_path}")
        return 0

    base_profile_path = pathlib.Path(config.base_profile_path).resolve()
    candidates_path = pathlib.Path(config.candidates_path).resolve()

    armory_url = args.armory_url or config.armory_url
    if armory_url:
        imported_path = out_dir / "imported_profiles" / "single_armory.simc"
        base_profile_path, imported_name = export_profile_from_armory(
            config=config,
            armory_url=armory_url,
            out_profile_path=imported_path,
        )
        single_label = imported_name
    else:
        single_label = "single"

    summary = run_droptimizer_for_profile(
        config=config,
        profile_path=base_profile_path,
        candidates_path=candidates_path,
        out_dir=out_dir,
        label=single_label,
    )

    print("\nDone.")
    print(f"Baseline DPS: {summary.baseline_dps:.2f}")
    print(
        f"Top scenario: {summary.best_scenario} "
        f"({summary.best_dps:.2f}, +{summary.best_dps - summary.baseline_dps:.2f})"
    )
    print(f"CSV report: {summary.csv_path}")
    print(f"Markdown report: {summary.md_path}")
    print(f"HTML report: {summary.html_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
