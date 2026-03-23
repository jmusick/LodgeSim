#!/usr/bin/env python3
"""Generate spec-scoped raid candidates directly from Raidbots static data."""

from __future__ import annotations

import argparse
import functools
import json
import pathlib
import urllib.request
from collections import defaultdict
from typing import Any

from droptimizer import normalize_candidate_mapping_key

RAIDBOTS_BASE = "https://www.raidbots.com/static/data/live"
USER_AGENT = "Mozilla/5.0"
DEFAULT_POOL_NAME = "all-raids-normal-hc-mythic.live"
DEFAULT_OUTPUT_SUBDIR = pathlib.Path("generated") / "live-candidates"
TIER_SOURCE_OVERRIDES_PATH = pathlib.Path(__file__).with_name("tier_source_overrides.json")

RAID_DIFFICULTY_ILVLS = {
    "lfr": 250,
    "normal": 263,
    "heroic": 276,
    "mythic": 282,
}

DEFAULT_RAID_IDS = [1307, 1308, 1314]
CATALYST_INSTANCE_ID = -87

CLASS_NAME_TO_ID = {
    "warrior": 1,
    "paladin": 2,
    "hunter": 3,
    "rogue": 4,
    "priest": 5,
    "death_knight": 6,
    "shaman": 7,
    "mage": 8,
    "warlock": 9,
    "monk": 10,
    "druid": 11,
    "demon_hunter": 12,
    "evoker": 13,
}

SPEC_NAME_TO_ID = {
    "warrior:arms": 71,
    "warrior:fury": 72,
    "warrior:protection": 73,
    "paladin:holy": 65,
    "paladin:protection": 66,
    "paladin:retribution": 70,
    "hunter:beast_mastery": 253,
    "hunter:beastmastery": 253,
    "hunter:marksmanship": 254,
    "hunter:survival": 255,
    "rogue:assassination": 259,
    "rogue:outlaw": 260,
    "rogue:subtlety": 261,
    "priest:discipline": 256,
    "priest:holy": 257,
    "priest:shadow": 258,
    "death_knight:blood": 250,
    "death_knight:frost": 251,
    "death_knight:unholy": 252,
    "deathknight:blood": 250,
    "deathknight:frost": 251,
    "deathknight:unholy": 252,
    "shaman:elemental": 262,
    "shaman:enhancement": 263,
    "shaman:restoration": 264,
    "mage:arcane": 62,
    "mage:fire": 63,
    "mage:frost": 64,
    "warlock:affliction": 265,
    "warlock:demonology": 266,
    "warlock:destruction": 267,
    "monk:brewmaster": 268,
    "monk:mistweaver": 270,
    "monk:windwalker": 269,
    "druid:balance": 102,
    "druid:feral": 103,
    "druid:guardian": 104,
    "druid:restoration": 105,
    "demon_hunter:havoc": 577,
    "demon_hunter:vengeance": 581,
    "demonhunter:havoc": 577,
    "demonhunter:vengeance": 581,
    "evoker:devastation": 1467,
    "evoker:preservation": 1468,
    "evoker:augmentation": 1473,
}

CLASS_MAX_ARMOR_SUBCLASS = {
    "warrior": 4,
    "paladin": 4,
    "hunter": 3,
    "rogue": 2,
    "priest": 1,
    "death_knight": 4,
    "deathknight": 4,
    "shaman": 3,
    "mage": 1,
    "warlock": 1,
    "monk": 2,
    "druid": 2,
    "demon_hunter": 2,
    "demonhunter": 2,
    "evoker": 3,
}

CLASS_ALLOWED_WEAPON_SUBCLASSES = {
    "warrior": {0, 1, 4, 5, 6, 7, 8, 10, 13, 15},
    "paladin": {0, 1, 4, 5, 6, 7, 8},
    "hunter": {0, 1, 2, 3, 6, 10, 18},
    "rogue": {0, 4, 7, 13, 15},
    "priest": {4, 7, 10, 13, 15, 19},
    "death_knight": {0, 1, 4, 5, 6, 7, 8},
    "deathknight": {0, 1, 4, 5, 6, 7, 8},
    "shaman": {0, 1, 4, 5, 6, 7, 10, 13, 15},
    "mage": {7, 10, 13, 15, 19},
    "warlock": {7, 10, 13, 15, 19},
    "monk": {0, 1, 4, 6, 7, 10, 13},
    "druid": {0, 4, 6, 10, 13, 15},
    "demon_hunter": {0, 7, 13, 15},
    "demonhunter": {0, 7, 13, 15},
    "evoker": {0, 4, 6, 7, 10, 13, 15},
}

INVENTORY_TYPE_TO_SLOTS = {
    1: ["head"],
    2: ["neck"],
    3: ["shoulders"],
    5: ["chest"],
    6: ["waist"],
    7: ["legs"],
    8: ["feet"],
    9: ["wrist"],
    10: ["hands"],
    11: ["finger1", "finger2"],
    12: ["trinket1", "trinket2"],
    13: ["main_hand", "off_hand"],
    14: ["off_hand"],
    15: ["main_hand"],
    16: ["back"],
    17: ["main_hand"],
    20: ["chest"],
    21: ["main_hand"],
    22: ["off_hand"],
    23: ["off_hand"],
    26: ["main_hand"],
}


def canonical_spec_keys() -> list[str]:
    seen: set[str] = set()
    keys: list[str] = []
    for raw_key in SPEC_NAME_TO_ID:
        normalized = normalize_candidate_mapping_key(raw_key)
        if ":" not in normalized or normalized in seen:
            continue
        seen.add(normalized)
        keys.append(normalized)
    return sorted(keys)


def fetch_json(name: str) -> Any:
    req = urllib.request.Request(f"{RAIDBOTS_BASE}/{name}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


@functools.lru_cache(maxsize=1)
def load_tier_source_overrides() -> dict[int, str]:
    if not TIER_SOURCE_OVERRIDES_PATH.exists():
        return {}

    raw = json.loads(TIER_SOURCE_OVERRIDES_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"Tier source overrides must be a JSON object: {TIER_SOURCE_OVERRIDES_PATH}")

    overrides: dict[int, str] = {}
    for item_id, source_label in raw.items():
        if isinstance(source_label, str):
            overrides[int(item_id)] = source_label
    return overrides


def spec_slug(spec_key: str) -> str:
    normalized = normalize_candidate_mapping_key(spec_key)
    if ":" in normalized:
        class_name, spec_name = normalized.split(":", 1)
        return f"{class_name}-{spec_name}"
    return normalized


def class_and_spec_ids(spec_key: str) -> tuple[str, int, int]:
    normalized = normalize_candidate_mapping_key(spec_key)
    if ":" not in normalized:
        raise ValueError("spec-key must be in class:spec form, e.g. hunter:survival")
    class_name, _ = normalized.split(":", 1)
    class_id = CLASS_NAME_TO_ID.get(class_name)
    spec_id = SPEC_NAME_TO_ID.get(normalized)
    if class_id is None or spec_id is None:
        raise ValueError(f"Unsupported spec key: {spec_key}")
    return class_name, class_id, spec_id


def is_armor_allowed(item: dict[str, Any], class_name: str) -> bool:
    if item.get("itemClass") != 4:
        return True
    inv_type = int(item.get("inventoryType") or 0)
    if inv_type not in {1, 3, 5, 6, 7, 8, 9, 10, 16, 20}:
        return True
    subclass = int(item.get("itemSubClass") or 0)
    max_subclass = CLASS_MAX_ARMOR_SUBCLASS.get(class_name)
    if max_subclass is None:
        return True
    return subclass == 0 or subclass <= max_subclass


def is_weapon_allowed(item: dict[str, Any], class_name: str) -> bool:
    if item.get("itemClass") != 2:
        return True
    subclass = int(item.get("itemSubClass") or -1)
    allowed = CLASS_ALLOWED_WEAPON_SUBCLASSES.get(class_name)
    if allowed is None:
        return True
    return subclass in allowed


def is_item_eligible(item: dict[str, Any], class_name: str, class_id: int, spec_id: int) -> bool:
    allowable_classes = item.get("allowableClasses")
    if isinstance(allowable_classes, list) and allowable_classes and class_id not in allowable_classes:
        return False

    specs = item.get("specs")
    if isinstance(specs, list) and specs and spec_id not in specs:
        return False

    if not is_armor_allowed(item, class_name):
        return False
    if not is_weapon_allowed(item, class_name):
        return False

    return True


def resolve_source_label(item: dict[str, Any], instance_names: dict[int, str], encounter_names: dict[int, str], selected_instance_ids: set[int]) -> str:
    for source in item.get("sources", []):
        if source.get("instanceId") in selected_instance_ids:
            instance_name = instance_names.get(int(source["instanceId"]), f"Instance {source['instanceId']}")
            encounter_name = encounter_names.get(int(source["encounterId"]), f"Encounter {source['encounterId']}")
            return f"{instance_name} - {encounter_name}"

    item_id = int(item.get("id") or 0)
    tier_source_overrides = load_tier_source_overrides()
    for source in item.get("sources", []):
        if int(source.get("instanceId") or 0) == CATALYST_INSTANCE_ID:
            if item_id in tier_source_overrides:
                return tier_source_overrides[item_id]
            return "Catalyst - Midnight Season 1"

    return "Unknown Source"


def should_include_item(
    item: dict[str, Any],
    selected_instance_ids: set[int],
    include_catalyst_tier: bool,
    instance_names: dict[int, str] | None = None,
) -> bool:
    sources = item.get("sources") or []
    if any(int(source.get("instanceId") or 0) in selected_instance_ids for source in sources):
        return True
    if include_catalyst_tier and item.get("itemSetId") and any(
        int(source.get("instanceId") or 0) == CATALYST_INSTANCE_ID for source in sources
    ):
        # When filtering to specific instances, only include catalyst tier pieces
        # whose home raid is one of the selected instances.
        if instance_names is not None:
            tier_overrides = load_tier_source_overrides()
            item_id = int(item.get("id") or 0)
            if item_id in tier_overrides:
                override_label = tier_overrides[item_id]
                selected_names = {instance_names.get(iid, "") for iid in selected_instance_ids}
                return any(
                    override_label.startswith(name + " -")
                    for name in selected_names
                    if name
                )
        return True  # no instance_names provided (all-raids mode), include all catalyst tiers
    return False


def emit_item_entries(item: dict[str, Any], ilvl: int, source_label: str) -> list[tuple[str, dict[str, Any]]]:
    inventory_type = int(item.get("inventoryType") or 0)
    slots = INVENTORY_TYPE_TO_SLOTS.get(inventory_type, [])
    if not slots:
        return []

    label = f"{item['name']} {ilvl} - {source_label}"
    simc = f"id={int(item['id'])},ilevel={ilvl}"
    clear_slots = ["off_hand"] if inventory_type == 17 else []

    entries: list[tuple[str, dict[str, Any]]] = []
    for slot in slots:
        entry: dict[str, Any] = {"label": label, "simc": simc}
        if clear_slots:
            entry["clear_slots"] = clear_slots
        entries.append((slot, entry))
    return entries


def default_output_path(spec_key: str, pool_name: str, output_dir: pathlib.Path | None = None) -> pathlib.Path:
    slug = spec_slug(spec_key)
    filename = f"candidates.{slug}.{pool_name}.json"
    if output_dir is None:
        return pathlib.Path(filename).resolve()
    return (output_dir / filename).resolve()


def build_candidates(
    items: list[dict[str, Any]],
    instance_names: dict[int, str],
    encounter_names: dict[int, str],
    class_name: str,
    class_id: int,
    spec_id: int,
    difficulties: list[str],
    selected_instance_ids: set[int],
    include_catalyst_tier: bool,
) -> dict[str, Any]:
    slots: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)

    for item in items:
        if not should_include_item(item, selected_instance_ids, include_catalyst_tier, instance_names):
            continue
        if not is_item_eligible(item, class_name, class_id, spec_id):
            continue

        source_label = resolve_source_label(item, instance_names, encounter_names, selected_instance_ids)
        for difficulty in difficulties:
            ilvl = RAID_DIFFICULTY_ILVLS[difficulty]
            for slot, entry in emit_item_entries(item, ilvl, source_label):
                dedupe_key = json.dumps({"slot": slot, "simc": entry["simc"]}, sort_keys=True)
                if dedupe_key in seen[slot]:
                    continue
                seen[slot].add(dedupe_key)
                slots[slot].append(entry)

    return {"slots": dict(slots)}


@functools.lru_cache(maxsize=1)
def load_raidbots_context() -> tuple[list[dict[str, Any]], dict[int, str], dict[int, str]]:
    items = fetch_json("encounter-items.json")
    instances = fetch_json("instances.json")

    if not isinstance(items, list) or not isinstance(instances, list):
        raise RuntimeError("Unexpected Raidbots data format.")

    instance_names: dict[int, str] = {}
    encounter_names: dict[int, str] = {}
    for instance in instances:
        instance_id = int(instance.get("id") or 0)
        instance_names[instance_id] = str(instance.get("name") or instance_id)
        for encounter in instance.get("encounters", []) or []:
            encounter_names[int(encounter.get("id") or 0)] = str(encounter.get("name") or encounter.get("id"))

    return items, instance_names, encounter_names


def _load_config_json(config_path: pathlib.Path) -> dict[str, Any]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Config must be a JSON object: {config_path}")
    return raw


def _resolve_output_dir(config_path: pathlib.Path, output_dir: pathlib.Path | None = None) -> pathlib.Path:
    if output_dir is None:
        return (config_path.parent / DEFAULT_OUTPUT_SUBDIR).resolve()
    if output_dir.is_absolute():
        return output_dir.resolve()
    return (config_path.parent / output_dir).resolve()


def _candidate_file_is_stale(output_path: pathlib.Path) -> bool:
    if not output_path.exists():
        return True

    try:
        output_mtime = output_path.stat().st_mtime
    except OSError:
        return True

    dependency_paths = [
        pathlib.Path(__file__),
        TIER_SOURCE_OVERRIDES_PATH,
    ]
    for dependency_path in dependency_paths:
        if not dependency_path.exists():
            continue
        try:
            if dependency_path.stat().st_mtime > output_mtime:
                return True
        except OSError:
            return True

    return False


def ensure_generated_candidate_file(
    config_path: pathlib.Path,
    spec_key: str,
    pool_name: str = DEFAULT_POOL_NAME,
    output_dir: pathlib.Path | None = None,
    strict: bool = True,
    set_default: bool = False,
    refresh: bool = False,
    difficulties: list[str] | None = None,
    selected_instance_ids: set[int] | None = None,
    include_catalyst_tier: bool = True,
) -> pathlib.Path:
    config_path = config_path.resolve()
    normalized_spec = normalize_candidate_mapping_key(spec_key)
    raw = _load_config_json(config_path)
    mappings = raw.get("candidates_by_spec") if isinstance(raw.get("candidates_by_spec"), dict) else {}

    existing_path_raw = mappings.get(normalized_spec)
    resolved_output_dir = _resolve_output_dir(config_path, output_dir)
    uses_explicit_output_dir = output_dir is not None
    output_path = default_output_path(
        normalized_spec,
        pool_name,
        output_dir=resolved_output_dir,
    ) if uses_explicit_output_dir else (
        pathlib.Path(existing_path_raw).resolve() if existing_path_raw else default_output_path(
            normalized_spec,
            pool_name,
            output_dir=resolved_output_dir,
        )
    )

    if output_path.exists() and not refresh and not _candidate_file_is_stale(output_path):
        if not existing_path_raw and not uses_explicit_output_dir:
            register_mappings(config_path, {normalized_spec: output_path}, strict, default_spec=normalized_spec if set_default else "")
        return output_path

    effective_difficulties = difficulties or ["normal", "heroic", "mythic"]
    effective_instance_ids = selected_instance_ids or set(DEFAULT_RAID_IDS)
    items, instance_names, encounter_names = load_raidbots_context()
    payload = generate_candidates_payload(
        spec_key=normalized_spec,
        difficulties=effective_difficulties,
        selected_instance_ids=effective_instance_ids,
        include_catalyst_tier=include_catalyst_tier,
        items=items,
        instance_names=instance_names,
        encounter_names=encounter_names,
    )
    write_candidates_file(output_path, payload)
    if not uses_explicit_output_dir:
        register_mappings(config_path, {normalized_spec: output_path}, strict, default_spec=normalized_spec if set_default else "")
    return output_path


def generate_candidates_payload(
    spec_key: str,
    difficulties: list[str],
    selected_instance_ids: set[int],
    include_catalyst_tier: bool,
    items: list[dict[str, Any]],
    instance_names: dict[int, str],
    encounter_names: dict[int, str],
) -> dict[str, Any]:
    class_name, class_id, spec_id = class_and_spec_ids(spec_key)
    return build_candidates(
        items=items,
        instance_names=instance_names,
        encounter_names=encounter_names,
        class_name=class_name,
        class_id=class_id,
        spec_id=spec_id,
        difficulties=difficulties,
        selected_instance_ids=selected_instance_ids,
        include_catalyst_tier=include_catalyst_tier,
    )


def write_candidates_file(output_path: pathlib.Path, payload: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def register_mappings(
    config_path: pathlib.Path,
    mappings_to_add: dict[str, pathlib.Path],
    strict: bool,
    default_spec: str = "",
) -> None:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Config must be a JSON object: {config_path}")

    mappings = raw.get("candidates_by_spec")
    if not isinstance(mappings, dict):
        mappings = {}
        raw["candidates_by_spec"] = mappings

    normalized_paths = {
        normalize_candidate_mapping_key(spec_key): str(candidates_path.resolve())
        for spec_key, candidates_path in mappings_to_add.items()
    }
    mappings.update(normalized_paths)

    default_key = normalize_candidate_mapping_key(default_spec) if default_spec else ""
    if default_key:
        default_path = normalized_paths.get(default_key) or mappings.get(default_key)
        if default_path:
            raw["candidates_path"] = default_path

    if strict:
        raw["strict_spec_mapping"] = True

    config_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate spec candidates directly from Raidbots live data.")
    parser.add_argument("--spec-key", default="", help="Spec key, e.g. hunter:survival")
    parser.add_argument(
        "--all-specs",
        action="store_true",
        help="Generate candidates for every supported spec key instead of a single spec.",
    )
    parser.add_argument("--pool-name", required=True, help="Pool suffix, e.g. all-raids-normal-hc-mythic")
    parser.add_argument(
        "--difficulty",
        nargs="+",
        default=["normal", "heroic", "mythic"],
        choices=sorted(RAID_DIFFICULTY_ILVLS),
        help="Difficulties to include",
    )
    parser.add_argument(
        "--instance-id",
        action="append",
        type=int,
        default=[],
        help="Specific raid instance ids to include. Defaults to Midnight Season 1 raids.",
    )
    parser.add_argument("--output", default="", help="Optional output path")
    parser.add_argument(
        "--output-dir",
        default="",
        help="Optional directory for generated files. When omitted, files are written beside the script.",
    )
    parser.add_argument("--register-config", default="", help="Optional config JSON to update")
    parser.add_argument("--strict-mapping", action="store_true", help="Enable strict mapping when registering")
    parser.add_argument(
        "--default-spec",
        default="",
        help="Optional spec key whose generated file should become config.candidates_path when registering.",
    )
    parser.add_argument(
        "--exclude-catalyst-tier",
        action="store_true",
        help="Exclude catalyst-only tier items when Raidbots does not expose raid-boss token sources directly.",
    )
    args = parser.parse_args()

    if args.all_specs and args.spec_key:
        raise ValueError("Use either --spec-key or --all-specs, not both.")
    if not args.all_specs and not args.spec_key:
        raise ValueError("Provide --spec-key for a single spec or --all-specs to generate every supported spec.")
    if args.output and args.all_specs:
        raise ValueError("--output may only be used with a single --spec-key run.")
    if args.default_spec and not args.register_config:
        raise ValueError("--default-spec requires --register-config.")

    selected_instance_ids = set(args.instance_id or DEFAULT_RAID_IDS)

    output_dir = pathlib.Path(args.output_dir).resolve() if args.output_dir else None
    items, instance_names, encounter_names = load_raidbots_context()

    spec_keys = canonical_spec_keys() if args.all_specs else [normalize_candidate_mapping_key(args.spec_key)]
    generated_paths: dict[str, pathlib.Path] = {}

    for spec_key in spec_keys:
        payload = generate_candidates_payload(
            spec_key=spec_key,
            difficulties=args.difficulty,
            selected_instance_ids=selected_instance_ids,
            include_catalyst_tier=not args.exclude_catalyst_tier,
            items=items,
            instance_names=instance_names,
            encounter_names=encounter_names,
        )
        output_path = pathlib.Path(args.output).resolve() if args.output else default_output_path(
            spec_key,
            args.pool_name,
            output_dir=output_dir,
        )
        write_candidates_file(output_path, payload)
        generated_paths[spec_key] = output_path

        count = sum(len(values) for values in payload["slots"].values())
        print(f"[{spec_key}] Wrote {count} candidates across {len(payload['slots'])} slots")
        print(f"[{spec_key}] Output: {output_path}")

    if args.register_config:
        config_path = pathlib.Path(args.register_config).resolve()
        register_mappings(config_path, generated_paths, args.strict_mapping, default_spec=args.default_spec)
        print(f"Updated config mapping: {config_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
