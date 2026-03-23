#!/usr/bin/env python3
"""Guild-wide droptimizer runner and loot-recipient aggregator.

Workflow:
1) Read guild roster from Blizzard profile API.
2) Filter to level-90 characters.
3) Run local droptimizer for each character via armory import.
4) Produce a report mapping each item to the guild member with the biggest upgrade.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from droptimizer import (
    Config,
    export_profile_from_armory,
    load_config,
    normalize_slot_name,
    run_droptimizer_for_profile,
    slugify,
)


@dataclass
class GuildMember:
    name: str
    realm_slug: str
    level: int
    armory_url: str
    guild_rank: int | None = None


@dataclass
class ItemWinner:
    slot: str
    simc: str
    item_label: str
    item_id: str
    source: str
    ilvl: str
    raider_name: str
    delta: float
    pct_gain: float


# Persisted query hash used by Blizzard's guild page for GetGuildRoster.
GUILD_ROSTER_QUERY_HASH = "a2d35574703e4a0658e9a93a0f514fd22ab6bd2d961cfba41d87b126df82d281"
DIFFICULTY_BONUS_ID = {
    "heroic": "4799",
    "mythic": "4800",
}


def parse_guild_url(url: str) -> tuple[str, str, str]:
    parsed = urllib.parse.urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]

    if "guild" not in parts:
        raise ValueError("Guild URL format not recognized. Expected '/.../guild/<region>/<realm>/<guild>'.")

    idx = parts.index("guild")
    tail = parts[idx + 1 :]
    if len(tail) < 3:
        raise ValueError("Guild URL format not recognized. Expected '/.../guild/<region>/<realm>/<guild>'.")

    region = tail[0].lower()
    realm_slug = tail[1].lower()
    guild_slug = tail[2].lower()
    return region, realm_slug, guild_slug


def fetch_guild_roster(region: str, realm_slug: str, guild_slug: str, locale: str) -> list[GuildMember]:
    locale_path = locale.lower().replace("_", "-")
    gql_url = f"https://worldofwarcraft.blizzard.com/{locale_path}/graphql"
    gql_payload = {
        "operationName": "GetGuildRoster",
        "variables": {
            "guild": {
                "nameSlug": guild_slug,
                "realmSlug": realm_slug,
                "region": region,
            }
        },
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": GUILD_ROSTER_QUERY_HASH,
            }
        },
    }

    req = urllib.request.Request(
        gql_url,
        data=json.dumps(gql_payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": locale_path,
        },
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    payload = json.loads(raw)

    if payload.get("errors"):
        raise RuntimeError(f"Guild roster query failed: {payload['errors']}")

    guild_obj = payload.get("data", {}).get("Guild")
    if not isinstance(guild_obj, dict):
        raise RuntimeError("Guild roster query returned no guild payload.")

    members = guild_obj.get("roster", [])
    result: list[GuildMember] = []
    for entry in members:
        if not isinstance(entry, dict):
            continue
        char = entry.get("character")
        if not isinstance(char, dict):
            continue

        name = str(char.get("name", "")).strip()
        level = int(char.get("level", 0) or 0)

        realm = char.get("realm", {})
        realm_for_char = str(realm.get("slug", realm_slug)).strip().lower() or realm_slug

        if not name:
            continue

        name_slug = urllib.parse.quote(name.lower())
        armory_url = (
            f"https://worldofwarcraft.blizzard.com/en-us/character/{region}/{realm_for_char}/{name_slug}"
        )

        result.append(
            GuildMember(
                name=name,
                realm_slug=realm_for_char,
                level=level,
                armory_url=armory_url,
                guild_rank=_extract_guild_rank(entry),
            )
        )

    return result


def _extract_guild_rank(entry: dict[str, Any]) -> int | None:
    """Best-effort extraction of a numeric guild rank from Blizzard roster payload."""
    # Common structures seen in roster payloads are either direct numeric values
    # or nested objects that contain a rank-like integer field.
    raw = entry.get("rank")

    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)

    if isinstance(raw, dict):
        for key in ("rankId", "rank", "id", "value", "position", "index"):
            val = raw.get(key)
            if isinstance(val, int):
                return val
            if isinstance(val, str) and val.isdigit():
                return int(val)

    # Some payload variants may expose rank fields at the top entry level.
    for key in ("guildRank", "rankIndex", "rankPosition"):
        val = entry.get(key)
        if isinstance(val, int):
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)

    return None


def parse_item_from_scenario(scenario_name: str) -> tuple[str, str, str, str]:
    """Return (slot, label, ilvl, source) from scenario name."""
    if ": " in scenario_name:
        slot, _, label = scenario_name.partition(": ")
    else:
        slot, label = "", scenario_name

    ilvl = ""
    source = ""
    m = re.match(r"^(.*?)\s+(\d{3})\s+-\s+(.+)$", label)
    if m:
        label = m.group(1).strip()
        ilvl = m.group(2)
        source = m.group(3).strip()

    return normalize_slot_name(slot.strip()), label.strip(), ilvl, source


def emit_progress(pct: int, stage: str, detail: str) -> None:
    pct = max(0, min(100, int(pct)))
    print(f"@@PROGRESS@@ pct={pct} stage={stage} detail={detail}")


def parse_item_id(simc: str) -> str:
    m = re.search(r"\bid=(\d+)", simc)
    return m.group(1) if m else ""


def parse_bonus_ids(simc: str) -> set[str]:
    m = re.search(r"\bbonus_id=([0-9/]+)", simc)
    if not m:
        return set()
    return {p.strip() for p in m.group(1).split("/") if p.strip()}


def filter_candidates_by_difficulty(
    candidates_path: pathlib.Path,
    difficulty: str,
    out_dir: pathlib.Path,
) -> tuple[pathlib.Path, int, int]:
    payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    slots = payload.get("slots", {})
    if not isinstance(slots, dict):
        raise ValueError(f"Candidates JSON has invalid 'slots' object: {candidates_path}")

    required_bonus = DIFFICULTY_BONUS_ID[difficulty]
    filtered_slots: dict[str, list[dict[str, Any]]] = {}
    before = 0
    after = 0

    for slot, items in slots.items():
        norm_slot = normalize_slot_name(str(slot))
        if not isinstance(items, list):
            continue
        kept: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            simc = str(item.get("simc", "")).strip()
            if not simc:
                continue
            before += 1
            bonus_ids = parse_bonus_ids(simc)
            if required_bonus in bonus_ids:
                entry: dict[str, Any] = {
                    "label": str(item.get("label", simc)),
                    "simc": simc,
                }

                # Preserve slot-clearing metadata (e.g. 2H weapons clearing off_hand)
                # so downstream scenario generation matches Raidbots behavior.
                raw_clear_slots = item.get("clear_slots")
                if isinstance(raw_clear_slots, list):
                    normalized_clear_slots = [
                        normalize_slot_name(str(slot_name).strip())
                        for slot_name in raw_clear_slots
                        if str(slot_name).strip()
                    ]
                    if normalized_clear_slots:
                        entry["clear_slots"] = normalized_clear_slots

                kept.append(entry)
                after += 1
        if kept:
            filtered_slots.setdefault(norm_slot, []).extend(kept)

    filtered_path = out_dir / f"candidates_filtered_{difficulty}.json"
    filtered_path.write_text(json.dumps({"slots": filtered_slots}, indent=2), encoding="utf-8")
    return filtered_path, before, after


def collect_winners_from_raider_csv(csv_path: pathlib.Path, raider_name: str) -> dict[tuple[str, str], ItemWinner]:
    winners: dict[tuple[str, str], ItemWinner] = {}

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            scenario = str(row.get("scenario", "")).strip()
            if not scenario:
                continue

            try:
                delta = float(row.get("delta", "0") or 0)
            except ValueError:
                delta = 0.0

            try:
                pct_gain = float(row.get("pct_gain", "0") or 0)
            except ValueError:
                pct_gain = 0.0

            replacements_raw = str(row.get("replacements", "{}")).strip() or "{}"
            try:
                replacements = json.loads(replacements_raw)
            except json.JSONDecodeError:
                replacements = {}

            if not isinstance(replacements, dict) or not replacements:
                continue

            slot = normalize_slot_name(next(iter(replacements.keys())))
            simc = str(replacements.get(slot, "")).strip()
            if not simc:
                # Fall back to the original key if normalization changed the lookup key.
                original_slot = next(iter(replacements.keys()))
                simc = str(replacements.get(original_slot, "")).strip()
            if not simc:
                continue

            _, parsed_label, ilvl, source = parse_item_from_scenario(scenario)
            item_id = parse_item_id(simc)

            key = (slot, simc)
            existing = winners.get(key)
            if existing is None or delta > existing.delta:
                winners[key] = ItemWinner(
                    slot=slot,
                    simc=simc,
                    item_label=parsed_label,
                    item_id=item_id,
                    source=source,
                    ilvl=ilvl,
                    raider_name=raider_name,
                    delta=delta,
                    pct_gain=pct_gain,
                )

    return winners


def merge_item_winners(all_winners: list[dict[tuple[str, str], ItemWinner]]) -> list[ItemWinner]:
    merged: dict[tuple[str, str], ItemWinner] = {}
    for winner_map in all_winners:
        for key, cand in winner_map.items():
            cur = merged.get(key)
            if cur is None or cand.delta > cur.delta:
                merged[key] = cand

    ranked = sorted(merged.values(), key=lambda x: x.delta, reverse=True)
    return ranked


def write_item_winner_reports(winners: list[ItemWinner], out_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    csv_path = out_dir / "guild_item_winners.csv"
    md_path = out_dir / "guild_item_winners.md"

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "rank",
                "item_label",
                "item_id",
                "ilvl",
                "slot",
                "source",
                "best_raider",
                "delta_dps",
                "pct_gain",
                "simc",
            ]
        )
        for idx, row in enumerate(winners, start=1):
            writer.writerow(
                [
                    idx,
                    row.item_label,
                    row.item_id,
                    row.ilvl,
                    row.slot,
                    row.source,
                    row.raider_name,
                    f"{row.delta:.2f}",
                    f"{row.pct_gain:.4f}",
                    row.simc,
                ]
            )

    with md_path.open("w", encoding="utf-8") as handle:
        handle.write("# Guild Item Winners\n\n")
        handle.write("Per-item best recipient by simulated DPS gain.\n\n")
        handle.write("| Rank | Item | iLvl | Slot | Source | Best Raider | Delta DPS | % Gain |\n")
        handle.write("|---:|---|---:|---|---|---|---:|---:|\n")
        for idx, row in enumerate(winners, start=1):
            handle.write(
                "| "
                f"{idx} | {row.item_label} | {row.ilvl} | {row.slot} | {row.source} | "
                f"{row.raider_name} | {row.delta:.2f} | {row.pct_gain:.4f}% |\n"
            )

    return csv_path, md_path


def run_member_sim(
    config: Config,
    member: GuildMember,
    out_dir: pathlib.Path,
    filtered_candidates_path: pathlib.Path,
    idx: int,
    total: int,
) -> dict[tuple[str, str], ItemWinner]:
    label = f"{member.name}-{member.realm_slug}"
    print(f"[{idx}/{total}] Importing + simming {label}")

    profile_path = out_dir / "imported_profiles" / f"{slugify(label)}.simc"
    export_profile_from_armory(config, member.armory_url, profile_path)

    raider_out = out_dir / slugify(label)
    summary = run_droptimizer_for_profile(
        config=config,
        profile_path=profile_path,
        candidates_path=filtered_candidates_path,
        out_dir=raider_out,
        label=label,
    )

    return collect_winners_from_raider_csv(summary.csv_path, label)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run guild-wide droptimizer sims and compute best recipient per item."
    )
    parser.add_argument(
        "--config",
        default="config.hunter-survival.all-raids-hc-mythic.json",
        help="Droptimizer config JSON.",
    )
    parser.add_argument(
        "--guild-url",
        required=True,
        help="Guild URL, e.g. https://worldofwarcraft.blizzard.com/en-us/guild/us/illidan/hidden-lodge/",
    )
    parser.add_argument(
        "--difficulty",
        required=True,
        choices=["heroic", "mythic"],
        help="Raid difficulty to simulate. Mixed difficulty runs are intentionally blocked.",
    )
    parser.add_argument("--level", type=int, default=90, help="Minimum exact level to include (default: 90).")
    parser.add_argument("--locale", default="en-us", help="WoW site locale path (default: en-us).")
    parser.add_argument(
        "--max-raiders",
        type=int,
        default=0,
        help="Optional limit for testing. 0 means no limit.",
    )
    parser.add_argument(
        "--parallel-raiders",
        type=int,
        default=1,
        help="How many raiders to process concurrently. Default: 1 (sequential).",
    )
    parser.add_argument(
        "--positive-only",
        action="store_true",
        help="Only include items with positive best gain. Default includes all items.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch roster and write level filter outputs only; do not run sims.",
    )
    args = parser.parse_args()

    if args.parallel_raiders < 1:
        raise ValueError("--parallel-raiders must be >= 1")

    config_path = pathlib.Path(args.config).resolve()
    config: Config = load_config(config_path)

    region, realm_slug, guild_slug = parse_guild_url(args.guild_url)

    print(f"Fetching roster for guild '{guild_slug}' on {realm_slug} ({region})...")
    emit_progress(2, "Roster", f"Fetching {guild_slug}")
    roster = fetch_guild_roster(region, realm_slug, guild_slug, args.locale)

    level_filtered = [m for m in roster if m.level == args.level]
    level_filtered.sort(key=lambda m: (m.guild_rank if m.guild_rank is not None else 999, m.name.lower()))
    if args.max_raiders and args.max_raiders > 0:
        level_filtered = level_filtered[: args.max_raiders]

    if not level_filtered:
        raise RuntimeError(f"No level {args.level} characters found for guild roster.")

    out_dir = pathlib.Path(config.output_dir).resolve() / f"guild_{slugify(guild_slug)}_{args.difficulty}"
    out_dir.mkdir(parents=True, exist_ok=True)

    candidates_path = pathlib.Path(config.candidates_path).resolve()
    filtered_candidates_path, candidates_before, candidates_after = filter_candidates_by_difficulty(
        candidates_path=candidates_path,
        difficulty=args.difficulty,
        out_dir=out_dir,
    )
    if candidates_after == 0:
        raise RuntimeError(
            f"No {args.difficulty} candidates were found in {candidates_path}. "
            "Use a candidates file that contains that raid difficulty."
        )

    print(f"Roster members: {len(roster)} total, {len(level_filtered)} at level {args.level}.")
    emit_progress(8, "Roster", f"Eligible raiders: {len(level_filtered)}")
    print(
        f"Candidates: {candidates_after}/{candidates_before} kept for {args.difficulty} "
        f"({filtered_candidates_path})"
    )
    emit_progress(12, "Candidates", f"{candidates_after}/{candidates_before} kept")

    roster_path = out_dir / f"guild_roster_level_{args.level}.csv"
    with roster_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "realm_slug", "level", "guild_rank", "armory_url"])
        for m in level_filtered:
            writer.writerow([m.name, m.realm_slug, m.level, m.guild_rank if m.guild_rank is not None else "", m.armory_url])
    print(f"Level {args.level} roster CSV: {roster_path}")

    if args.dry_run:
        emit_progress(100, "Complete", "Dry run complete")
        print("Dry run complete. No sims were executed.")
        return 0

    all_winner_maps: list[dict[tuple[str, str], ItemWinner]] = []
    total = len(level_filtered)
    workers = min(args.parallel_raiders, total)
    print(f"Parallel raiders: {workers}")

    if workers == 1:
        for idx, member in enumerate(level_filtered, start=1):
            winners = run_member_sim(
                config=config,
                member=member,
                out_dir=out_dir,
                filtered_candidates_path=filtered_candidates_path,
                idx=idx,
                total=total,
            )
            all_winner_maps.append(winners)
            pct = 12 + int((idx / total) * 84)
            emit_progress(pct, "Raiders", f"Completed {idx}/{total}")
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    run_member_sim,
                    config,
                    member,
                    out_dir,
                    filtered_candidates_path,
                    idx,
                    total,
                )
                for idx, member in enumerate(level_filtered, start=1)
            ]
            done = 0
            for fut in as_completed(futures):
                all_winner_maps.append(fut.result())
                done += 1
                pct = 12 + int((done / total) * 84)
                emit_progress(pct, "Raiders", f"Completed {done}/{total}")

    merged = merge_item_winners(all_winner_maps)
    if args.positive_only:
        merged = [x for x in merged if x.delta > 0]

    csv_path, md_path = write_item_winner_reports(merged, out_dir)

    print("\nGuild run complete.")
    print(f"Item winners: {len(merged)}")
    print(f"CSV: {csv_path}")
    print(f"Markdown: {md_path}")
    emit_progress(100, "Complete", "Guild run complete")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
