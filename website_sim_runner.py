#!/usr/bin/env python3
"""Pull sim targets from HiddenLodgeWebsite, run WoWSim, and post results back."""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import pathlib
import shutil
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from droptimizer import (
    Config,
    export_profile_from_armory,
    extract_profile_class_spec,
    load_config,
    run_droptimizer_for_profile,
    slugify,
)
from generate_live_candidates import DEFAULT_RAID_IDS, ensure_generated_candidate_file
from guild_droptimizer import collect_winners_from_raider_csv, merge_item_winners


# Mapping from UI raid slug → Raidbots instance IDs
RAID_INSTANCE_IDS: dict[str, list[int]] = {
    "voidspire": [1307],
    "dreamrift": [1314],
    "queldanas": [1308],
    "all": DEFAULT_RAID_IDS,
}

RAID_DISPLAY_NAMES: dict[str, str] = {
    "voidspire": "The Voidspire",
    "dreamrift": "The Dreamrift",
    "queldanas": "March on Quel'Danas",
    "all": "All Raids",
}


@dataclass
class TargetRaider:
    blizzard_char_id: int
    name: str
    realm_slug: str
    region: str
    level: int
    guild_rank: int | None
    priority: int | None


@dataclass
class TargetTeam:
    team_id: int
    team_name: str
    raid_mode: str
    difficulty: str
    max_raiders: int | None
    parallel_raiders: int | None
    positive_only: bool | None
    raiders: list[TargetRaider]


@dataclass
class TargetsResponse:
    roster_revision: str
    generated_at_utc: str
    teams: list[TargetTeam]


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def _enable_line_buffering() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    except Exception:
        pass


SIM_API_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) WoWSimRunner/1.0"


def _open_url_with_proxy_fallback(req: urllib.request.Request, timeout: float):
    parsed = urllib.parse.urlparse(req.full_url)
    host = (parsed.hostname or "").strip().lower().strip("[]")
    prefer_direct = False
    if host in {"localhost", "127.0.0.1", "::1"}:
        prefer_direct = True
    elif host and "." not in host:
        prefer_direct = True
    else:
        try:
            addr = ipaddress.ip_address(host)
            prefer_direct = addr.is_private or addr.is_loopback or addr.is_link_local
        except ValueError:
            prefer_direct = False

    direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    if prefer_direct:
        try:
            return direct_opener.open(req, timeout=timeout)
        except Exception:
            pass

    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 407} and not prefer_direct:
            return direct_opener.open(req, timeout=timeout)
        raise
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        # WinError 10061 here is commonly caused by a dead local proxy.
        if getattr(reason, "winerror", None) != 10061 and not prefer_direct:
            raise

        return direct_opener.open(req, timeout=timeout)


def _request_json(
    method: str,
    url: str,
    runner_key: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "X-Sim-Runner-Key": runner_key,
        "User-Agent": SIM_API_USER_AGENT,
    }
    data: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _open_url_with_proxy_fallback(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if not raw.strip():
                return {}
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
            raise RuntimeError(f"Expected JSON object from {url}, got {type(parsed).__name__}.")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {method} {url}: {body}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(
            f"Could not connect to {url}. Reason: {reason}. "
            "If you are running dev mode, start HiddenLodgeWebsite with `npm run dev` "
            "and confirm SIM_SITE_BASE_URL_DEV points to that server."
        ) from exc


def _build_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def _resolve_runtime_settings(args: argparse.Namespace) -> tuple[str, str]:
    env_name = args.environment.upper()
    base_url = (
        args.site_base_url
        or os.getenv(f"SIM_SITE_BASE_URL_{env_name}")
        or os.getenv("SIM_SITE_BASE_URL")
    )
    runner_key = (
        args.runner_key
        or os.getenv(f"SIM_RUNNER_KEY_{env_name}")
        or os.getenv("SIM_RUNNER_KEY")
    )

    missing: list[str] = []
    if not base_url:
        missing.append("site base URL")
    if not runner_key:
        missing.append("runner key")
    if missing:
        raise ValueError(
            "Missing "
            + " and ".join(missing)
            + ". Provide --site-base-url/--runner-key or set "
            + f"SIM_SITE_BASE_URL_{env_name}/SIM_RUNNER_KEY_{env_name}."
        )

    return str(base_url).strip(), str(runner_key).strip()


def _parse_targets(payload: dict[str, Any]) -> TargetsResponse:
    roster_revision = str(payload.get("roster_revision", "")).strip()
    generated_at_utc = str(payload.get("generated_at_utc", "")).strip()
    teams_raw = payload.get("teams")
    if not roster_revision or not isinstance(teams_raw, list):
        raise RuntimeError("Invalid targets payload: missing roster_revision or teams array.")

    teams: list[TargetTeam] = []
    for team in teams_raw:
        if not isinstance(team, dict):
            continue
        raiders_raw = team.get("raiders")
        if not isinstance(raiders_raw, list):
            continue

        raiders: list[TargetRaider] = []
        for row in raiders_raw:
            if not isinstance(row, dict):
                continue
            try:
                raiders.append(
                    TargetRaider(
                        blizzard_char_id=int(row["blizzard_char_id"]),
                        name=str(row["name"]),
                        realm_slug=str(row["realm_slug"]),
                        region=str(row.get("region", "us") or "us").lower(),
                        level=int(row.get("level", 0) or 0),
                        guild_rank=int(row["guild_rank"]) if row.get("guild_rank") is not None else None,
                        priority=int(row["priority"]) if row.get("priority") is not None else None,
                    )
                )
            except Exception:
                continue

        if not raiders:
            continue

        try:
            teams.append(
                TargetTeam(
                    team_id=int(team["team_id"]),
                    team_name=str(team["team_name"]),
                    raid_mode=str(team.get("raid_mode", "flex")),
                    difficulty=str(team.get("difficulty", "heroic")),
                    max_raiders=int(team["max_raiders"]) if team.get("max_raiders") is not None else None,
                    parallel_raiders=int(team["parallel_raiders"]) if team.get("parallel_raiders") is not None else None,
                    positive_only=bool(team["positive_only"]) if team.get("positive_only") is not None else None,
                    raiders=raiders,
                )
            )
        except Exception:
            continue

    return TargetsResponse(roster_revision=roster_revision, generated_at_utc=generated_at_utc, teams=teams)


def _armory_url(raider: TargetRaider) -> str:
    name_slug = urllib.parse.quote(raider.name.lower())
    return (
        f"https://worldofwarcraft.blizzard.com/en-us/character/"
        f"{raider.region}/{raider.realm_slug}/{name_slug}"
    )


def _normalize_character_filter(value: str) -> str:
    return value.strip().lower()


def _resolve_candidates_for_profile(
    config: Config,
    config_path: pathlib.Path,
    profile_path: pathlib.Path,
    selected_instance_ids: set[int] | None = None,
    difficulties: list[str] | None = None,
    filtered_output_dir: pathlib.Path | None = None,
) -> pathlib.Path:
    default_path = pathlib.Path(config.candidates_path).resolve()
    profile_text = profile_path.read_text(encoding="utf-8")
    class_name, spec_name = extract_profile_class_spec(profile_text)
    if class_name and spec_name:
        exact_key = f"{class_name}:{spec_name}"

        if selected_instance_ids is None and difficulties is None:
            # Unfiltered: use the shared cached candidate file.
            match = config.candidates_by_spec.get(exact_key)
            if match and pathlib.Path(match).exists():
                return pathlib.Path(match).resolve()
            generated_path = ensure_generated_candidate_file(
                config_path=config_path,
                spec_key=exact_key,
                strict=config.strict_spec_mapping,
            )
            config.candidates_by_spec[exact_key] = str(generated_path)
            return generated_path.resolve()
        else:
            # Filtered: write to a per-job directory so we never overwrite the
            # shared unfiltered file.  No caching — regenerate fresh each run.
            generated_path = ensure_generated_candidate_file(
                config_path=config_path,
                spec_key=exact_key,
                strict=config.strict_spec_mapping,
                output_dir=filtered_output_dir,
                selected_instance_ids=selected_instance_ids,
                difficulties=difficulties,
                refresh=True,
            )
            return generated_path.resolve()

    if class_name:
        class_match = config.candidates_by_spec.get(class_name)
        if class_match:
            return pathlib.Path(class_match).resolve()

    if config.strict_spec_mapping:
        requested = f"{class_name or 'unknown'}:{spec_name or 'unknown'}"
        known = ", ".join(sorted(config.candidates_by_spec)) or "none"
        raise RuntimeError(
            "No candidates mapping found for "
            f"{requested}. Available mappings: {known}."
        )

    return default_path


def _call_start(base_url: str, runner_key: str, payload: dict[str, Any]) -> None:
    _request_json("POST", _build_url(base_url, "/api/sim/runs/start"), runner_key, payload)


def _call_heartbeat(base_url: str, runner_key: str, run_id: str, site_team_id: int) -> None:
    _request_json(
        "POST",
        _build_url(base_url, "/api/sim/runs/heartbeat"),
        runner_key,
        {"run_id": run_id, "site_team_id": site_team_id},
    )


def _call_finish(
    base_url: str,
    runner_key: str,
    run_id: str,
    site_team_id: int,
    successful: bool,
    error_message: str | None = None,
) -> None:
    status = "finished" if successful else "failed"
    _request_json(
        "POST",
        _build_url(base_url, "/api/sim/runs/finish"),
        runner_key,
        {
            "run_id": run_id,
            "site_team_id": site_team_id,
            "finished_at_utc": utc_now(),
            "successful": successful,
            "status": status,
            "error_message": (error_message[:1000] if error_message else None),
        },
    )


def _call_results(base_url: str, runner_key: str, payload: dict[str, Any]) -> None:
    run_id = payload.get("run_id", "unknown")
    site_team_id = payload.get("site_team_id", "unknown")
    raider_count = len(payload.get("raider_summaries", []))
    print(
        f"[results] POSTing results for run {run_id} (team={site_team_id}, raiders={raider_count})"
    )
    try:
        response = _request_json("POST", _build_url(base_url, "/api/sim/results"), runner_key, payload)
        inserted_raider_summaries = response.get("inserted", {}).get("raider_summaries", 0)
        inserted_item_winners = response.get("inserted", {}).get("item_winners", 0)
        duplicate = response.get("duplicate", False)
        print(
            f"[results] run {run_id}: success=True, duplicate={duplicate}, "
            f"inserted_raider_summaries={inserted_raider_summaries}, "
            f"inserted_item_winners={inserted_item_winners}"
        )
    except Exception as exc:
        print(f"[results] run {run_id}: FAILED — {exc}")
        raise


def run_team(
    config: Config,
    config_path: pathlib.Path,
    base_url: str,
    runner_key: str,
    roster_revision: str,
    team: TargetTeam,
    output_root: pathlib.Path,
    max_raiders_override: int,
    positive_only_override: bool | None,
    sim_raid: str = "all",
    sim_difficulty: str = "all",
) -> None:
    run_id = str(uuid.uuid4())
    started_at = utc_now()
    team_slug = slugify(team.team_name) or f"team-{team.team_id}"
    out_dir = output_root / f"team_{team.team_id}_{team_slug}_{team.difficulty}_{run_id[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve raid/difficulty filter
    selected_instance_ids: set[int] | None = None
    effective_difficulties: list[str] | None = None
    if sim_raid and sim_raid != "all":
        instance_list = RAID_INSTANCE_IDS.get(sim_raid, DEFAULT_RAID_IDS)
        selected_instance_ids = set(instance_list)
    if sim_difficulty and sim_difficulty != "all":
        effective_difficulties = [sim_difficulty]

    sim_raid_label = RAID_DISPLAY_NAMES.get(sim_raid, "All Raids") if sim_raid else "All Raids"

    print(
        f"[team {team.team_id}] Starting run {run_id} "
        f"({team.team_name} / {team.difficulty} / raid={sim_raid_label} / difficulty_filter={sim_difficulty})"
    )

    _call_start(
        base_url,
        runner_key,
        {
            "run_id": run_id,
            "site_team_id": team.team_id,
            "roster_revision": roster_revision,
            "difficulty": team.difficulty,
            "started_at_utc": started_at,
            "simc_version": None,
            "runner_version": "wowsim-website-runner-v1",
        },
    )

    try:
        raiders = sorted(
            team.raiders,
            key=lambda r: (
                r.priority if r.priority is not None else 999,
                r.guild_rank if r.guild_rank is not None else 999,
                r.name.lower(),
            ),
        )

        team_limit = team.max_raiders or 0
        if max_raiders_override > 0:
            team_limit = max_raiders_override
        if team_limit > 0:
            raiders = raiders[:team_limit]

        if not raiders:
            raise RuntimeError(f"Team {team.team_id} has no raiders after filtering.")

        raider_id_by_label: dict[str, int] = {}
        all_winner_maps = []
        raider_summaries = []

        for idx, raider in enumerate(raiders, start=1):
            label = f"{raider.name}-{raider.realm_slug}"
            raider_id_by_label[label] = raider.blizzard_char_id

            print(f"[team {team.team_id}] [{idx}/{len(raiders)}] Simming {label}")
            profile_path = out_dir / "imported_profiles" / f"{slugify(label)}.simc"
            export_profile_from_armory(config, _armory_url(raider), profile_path)
            filtered_candidates_path = _resolve_candidates_for_profile(
                config,
                config_path,
                profile_path,
                selected_instance_ids=selected_instance_ids,
                difficulties=effective_difficulties,
                filtered_output_dir=out_dir / "filtered-candidates" if (selected_instance_ids or effective_difficulties) else None,
            )
            print(f"[team {team.team_id}] [{idx}/{len(raiders)}] Candidates: {filtered_candidates_path.name}")

            raider_out = out_dir / slugify(label)
            summary = run_droptimizer_for_profile(
                config=config,
                profile_path=profile_path,
                candidates_path=filtered_candidates_path,
                out_dir=raider_out,
                label=label,
            )

            all_winner_maps.append(collect_winners_from_raider_csv(summary.csv_path, label))
            raider_summaries.append(
                {
                    "blizzard_char_id": raider.blizzard_char_id,
                    "baseline_dps": summary.baseline_dps,
                    "top_scenario": summary.best_scenario,
                    "top_dps": summary.best_dps,
                    "gain_dps": summary.best_dps - summary.baseline_dps,
                }
            )
            _call_heartbeat(base_url, runner_key, run_id, team.team_id)

        winners = merge_item_winners(all_winner_maps)
        positive_only = (
            positive_only_override
            if positive_only_override is not None
            else (team.positive_only if team.positive_only is not None else False)
        )
        if positive_only:
            winners = [row for row in winners if row.delta > 0]

        item_winners = []
        for winner in winners:
            best_char_id = raider_id_by_label.get(winner.raider_name)
            if best_char_id is None:
                continue
            item_winners.append(
                {
                    "slot": winner.slot,
                    "item_id": int(winner.item_id) if winner.item_id else None,
                    "item_label": winner.item_label,
                    "ilvl": float(winner.ilvl) if winner.ilvl else None,
                    "source": winner.source or None,
                    "best_blizzard_char_id": best_char_id,
                    "delta_dps": winner.delta,
                    "pct_gain": winner.pct_gain,
                    "simc": winner.simc,
                }
            )

        _call_results(
            base_url,
            runner_key,
            {
                "run_id": run_id,
                "roster_revision": roster_revision,
                "started_at_utc": started_at,
                "finished_at_utc": utc_now(),
                "site_team_id": team.team_id,
                "difficulty": team.difficulty,
                "sim_raid_label": sim_raid_label,
                "sim_difficulty": sim_difficulty if sim_difficulty != "all" else None,
                "simc_version": None,
                "runner_version": "wowsim-website-runner-v1",
                "raider_summaries": raider_summaries,
                "item_winners": item_winners,
            },
        )

        _call_finish(base_url, runner_key, run_id, team.team_id, successful=True)
        print(
            f"[team {team.team_id}] Completed run {run_id}: "
            f"{len(raider_summaries)} raiders, {len(item_winners)} winners"
        )
    except Exception as exc:
        _call_finish(base_url, runner_key, run_id, team.team_id, successful=False, error_message=str(exc))
        raise


def run_addon_profile(
    config: Config,
    config_path: pathlib.Path,
    base_url: str,
    runner_key: str,
    roster_revision: str,
    team: TargetTeam,
    raider: TargetRaider,
    addon_profile_path: pathlib.Path,
    output_root: pathlib.Path,
    sim_raid: str = "all",
    sim_difficulty: str = "all",
) -> None:
    run_id = str(uuid.uuid4())
    started_at = utc_now()
    team_slug = slugify(team.team_name) or f"team-{team.team_id}"
    out_dir = output_root / f"team_{team.team_id}_{team_slug}_{team.difficulty}_{run_id[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve raid/difficulty filter
    selected_instance_ids: set[int] | None = None
    effective_difficulties: list[str] | None = None
    if sim_raid and sim_raid != "all":
        instance_list = RAID_INSTANCE_IDS.get(sim_raid, DEFAULT_RAID_IDS)
        selected_instance_ids = set(instance_list)
    if sim_difficulty and sim_difficulty != "all":
        effective_difficulties = [sim_difficulty]

    sim_raid_label = RAID_DISPLAY_NAMES.get(sim_raid, "All Raids") if sim_raid else "All Raids"
    label = f"{raider.name}-{raider.realm_slug}"

    print(
        f"[team {team.team_id}] Starting addon run {run_id} "
        f"({team.team_name} / {team.difficulty} / raid={sim_raid_label} / difficulty_filter={sim_difficulty})"
    )

    _call_start(
        base_url,
        runner_key,
        {
            "run_id": run_id,
            "site_team_id": team.team_id,
            "roster_revision": roster_revision,
            "difficulty": team.difficulty,
            "started_at_utc": started_at,
            "simc_version": None,
            "runner_version": "wowsim-website-runner-v1",
        },
    )

    try:
        print(f"[team {team.team_id}] Simming addon profile for {label}")

        profile_path = out_dir / "imported_profiles" / f"{slugify(label)}.simc"
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(addon_profile_path, profile_path)

        filtered_candidates_path = _resolve_candidates_for_profile(
            config,
            config_path,
            profile_path,
            selected_instance_ids=selected_instance_ids,
            difficulties=effective_difficulties,
            filtered_output_dir=out_dir / "filtered-candidates" if (selected_instance_ids or effective_difficulties) else None,
        )
        print(f"[team {team.team_id}] Candidates: {filtered_candidates_path.name}")

        raider_out = out_dir / slugify(label)
        summary = run_droptimizer_for_profile(
            config=config,
            profile_path=profile_path,
            candidates_path=filtered_candidates_path,
            out_dir=raider_out,
            label=label,
        )

        _call_heartbeat(base_url, runner_key, run_id, team.team_id)

        winners = merge_item_winners([collect_winners_from_raider_csv(summary.csv_path, label)])

        item_winners = []
        for winner in winners:
            item_winners.append(
                {
                    "slot": winner.slot,
                    "item_id": int(winner.item_id) if winner.item_id else None,
                    "item_label": winner.item_label,
                    "ilvl": float(winner.ilvl) if winner.ilvl else None,
                    "source": winner.source or None,
                    "best_blizzard_char_id": raider.blizzard_char_id,
                    "delta_dps": winner.delta,
                    "pct_gain": winner.pct_gain,
                    "simc": winner.simc,
                }
            )

        raider_summaries = [
            {
                "blizzard_char_id": raider.blizzard_char_id,
                "baseline_dps": summary.baseline_dps,
                "top_scenario": summary.best_scenario,
                "top_dps": summary.best_dps,
                "gain_dps": summary.best_dps - summary.baseline_dps,
            }
        ]

        _call_results(
            base_url,
            runner_key,
            {
                "run_id": run_id,
                "roster_revision": roster_revision,
                "started_at_utc": started_at,
                "finished_at_utc": utc_now(),
                "site_team_id": team.team_id,
                "difficulty": team.difficulty,
                "sim_raid_label": sim_raid_label,
                "sim_difficulty": sim_difficulty if sim_difficulty != "all" else None,
                "simc_version": None,
                "runner_version": "wowsim-website-runner-v1",
                "raider_summaries": raider_summaries,
                "item_winners": item_winners,
            },
        )

        _call_finish(base_url, runner_key, run_id, team.team_id, successful=True)
        print(
            f"[team {team.team_id}] Completed addon run {run_id}: "
            f"1 raider, {len(item_winners)} winners"
        )
    except Exception as exc:
        _call_finish(base_url, runner_key, run_id, team.team_id, successful=False, error_message=str(exc))
        raise


def _single_target_config(config: Config) -> Config:
    return Config(
        simc_path=config.simc_path,
        base_profile_path=config.base_profile_path,
        candidates_path=config.candidates_path,
        raiders_path=config.raiders_path,
        armory_url=config.armory_url,
        output_dir=config.output_dir,
        mode=config.mode,
        iterations=config.iterations,
        threads=config.threads,
        fight_style=config.fight_style,
        additional_options=config.additional_options,
        max_scenarios=1,
        staged_pruning=False,
        staged_threshold=config.staged_threshold,
        # Keep single-target aligned with current character state (Raidbots Quick Sim parity).
        assume_fully_upgraded_equipped=False,
        assume_fully_upgraded_candidates=False,
        candidates_by_spec=config.candidates_by_spec,
        strict_spec_mapping=config.strict_spec_mapping,
    )


def run_single_target_profile(
    config: Config,
    config_path: pathlib.Path,
    base_url: str,
    runner_key: str,
    roster_revision: str,
    team: TargetTeam,
    raider: TargetRaider,
    output_root: pathlib.Path,
) -> None:
    run_id = str(uuid.uuid4())
    started_at = utc_now()
    team_slug = slugify(team.team_name) or f"team-{team.team_id}"
    out_dir = output_root / f"team_{team.team_id}_{team_slug}_{team.difficulty}_{run_id[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)

    label = f"{raider.name}-{raider.realm_slug}"

    print(
        f"[team {team.team_id}] Starting single-target run {run_id} "
        f"({team.team_name} / {team.difficulty} / {label})"
    )

    _call_start(
        base_url,
        runner_key,
        {
            "run_id": run_id,
            "site_team_id": team.team_id,
            "roster_revision": roster_revision,
            "difficulty": team.difficulty,
            "started_at_utc": started_at,
            "simc_version": None,
            "runner_version": "wowsim-website-runner-v1-single-target",
        },
    )

    try:
        profile_path = out_dir / "imported_profiles" / f"{slugify(label)}.simc"
        export_profile_from_armory(config, _armory_url(raider), profile_path)

        candidates_path = _resolve_candidates_for_profile(
            config,
            config_path,
            profile_path,
        )

        summary = run_droptimizer_for_profile(
            config=_single_target_config(config),
            profile_path=profile_path,
            candidates_path=candidates_path,
            out_dir=out_dir / slugify(label),
            label=label,
        )

        _call_heartbeat(base_url, runner_key, run_id, team.team_id)

        print(f"[{label}] Posting results to website...")
        _call_results(
            base_url,
            runner_key,
            {
                "run_id": run_id,
                "roster_revision": roster_revision,
                "started_at_utc": started_at,
                "finished_at_utc": utc_now(),
                "site_team_id": team.team_id,
                "difficulty": team.difficulty,
                "sim_raid_label": "Single Target",
                "sim_difficulty": None,
                "simc_version": None,
                "runner_version": "wowsim-website-runner-v1-single-target",
                "raider_summaries": [
                    {
                        "blizzard_char_id": raider.blizzard_char_id,
                        "baseline_dps": summary.baseline_dps,
                        "top_scenario": summary.best_scenario,
                        "top_dps": summary.best_dps,
                        "gain_dps": summary.best_dps - summary.baseline_dps,
                    }
                ],
                "item_winners": [],
            },
        )
        print(f"[{label}] Results posted successfully.")

        _call_finish(base_url, runner_key, run_id, team.team_id, successful=True)
        print(
            f"[team {team.team_id}] Completed single-target run {run_id}: "
            f"baseline={summary.baseline_dps:.2f}"
        )
    except Exception as exc:
        _call_finish(base_url, runner_key, run_id, team.team_id, successful=False, error_message=str(exc))
        raise


def main() -> int:
    _enable_line_buffering()
    parser = argparse.ArgumentParser(description="Website-integrated WoWSim runner")
    parser.add_argument("--config", default="config.guild.json", help="Path to WoWSim config JSON")
    parser.add_argument(
        "--environment",
        choices=["dev", "prod"],
        default="dev",
        help="Runtime target. Defaults to dev.",
    )
    parser.add_argument(
        "--site-base-url",
        default="",
        help="HiddenLodgeWebsite base URL. Overrides env vars when provided.",
    )
    parser.add_argument(
        "--runner-key",
        default="",
        help="Value for X-Sim-Runner-Key. Overrides env vars when provided.",
    )
    parser.add_argument(
        "--team-id",
        action="append",
        type=int,
        default=[],
        help="Optional team_id filter. Can be passed multiple times.",
    )
    parser.add_argument(
        "--max-raiders",
        type=int,
        default=0,
        help="Override per-team max raiders. 0 keeps API/team default.",
    )
    parser.add_argument(
        "--positive-only",
        action="store_true",
        help="Force posting only winners with positive delta.",
    )
    parser.add_argument(
        "--full-slot-results",
        action="store_true",
        help="Disable staged pruning so uploaded results include all simulated slot items.",
    )
    parser.add_argument(
        "--character-name",
        default="",
        help="Optional case-insensitive character name filter applied across all teams.",
    )
    parser.add_argument(
        "--allow-bulk-run",
        action="store_true",
        help="Allow manual bulk target pull/runs without a specific --team-id and --character-name.",
    )
    parser.add_argument(
        "--sim-raid",
        default="all",
        choices=["all", "voidspire", "dreamrift", "queldanas"],
        help="Restrict candidate pool to a single raid. Default: all raids.",
    )
    parser.add_argument(
        "--sim-difficulty",
        default="all",
        choices=["all", "normal", "heroic", "mythic"],
        help="Restrict candidate pool to a single difficulty. Default: all.",
    )
    parser.add_argument(
        "--mode",
        default="site",
        choices=["site", "addon", "single_target"],
        help="Sim input mode: site (website data), addon (SimC export), or single_target. Default: site.",
    )
    parser.add_argument(
        "--addon-profile",
        default="",
        help="Path to SimC addon export file. Required when mode=addon.",
    )
    args = parser.parse_args()

    mode = args.mode.strip().lower()
    addon_profile_path = args.addon_profile.strip()
    
    requested_character = args.character_name.strip()
    requested_teams = bool(args.team_id)
    if mode == "addon":
        if not addon_profile_path:
            print("ERROR: --addon-profile is required when --mode=addon")
            return 2
        if not pathlib.Path(addon_profile_path).exists():
            print(f"ERROR: Addon profile file not found: {addon_profile_path}")
            return 2
        if not requested_character or not requested_teams:
            print("ERROR: Addon mode requires both --team-id and --character-name.")
            return 2
    elif not args.allow_bulk_run and (not requested_character or not requested_teams):
        print(
            "ERROR: Bulk/manual runs are disabled by default. "
            "Website-triggered mode requires both --team-id and --character-name."
        )
        print("If you intentionally want a bulk run, pass --allow-bulk-run.")
        return 2

    base_url, runner_key = _resolve_runtime_settings(args)
    print(f"Environment: {args.environment}")
    print(f"Site base URL: {base_url}")

    try:
        config_path = pathlib.Path(args.config).resolve()
        config = load_config(config_path)
        if args.full_slot_results:
            config.staged_pruning = False
            print("Full slot results mode active: staged pruning disabled.")
        output_root = pathlib.Path(config.output_dir).resolve() / "website-runs"
        output_root.mkdir(parents=True, exist_ok=True)

        targets_payload = _request_json(
            "GET",
            _build_url(base_url, "/api/sim/targets"),
            runner_key,
        )
        targets = _parse_targets(targets_payload)

        teams = targets.teams
        if args.team_id:
            allowed = set(args.team_id)
            teams = [team for team in teams if team.team_id in allowed]

        if requested_character:
            requested_name = _normalize_character_filter(requested_character)
            filtered_teams: list[TargetTeam] = []
            for team in teams:
                matching_raiders = [
                    raider for raider in team.raiders if _normalize_character_filter(raider.name) == requested_name
                ]
                if not matching_raiders:
                    continue

                filtered_teams.append(
                    TargetTeam(
                        team_id=team.team_id,
                        team_name=team.team_name,
                        raid_mode=team.raid_mode,
                        difficulty=team.difficulty,
                        max_raiders=team.max_raiders,
                        parallel_raiders=team.parallel_raiders,
                        positive_only=team.positive_only,
                        raiders=matching_raiders,
                    )
                )

            teams = filtered_teams
            print(f"Character filter active: {requested_character}")

        if not teams:
            print("No teams to run.")
            return 0

        if mode == "addon":
            requested_name = _normalize_character_filter(requested_character)
            selected_team = teams[0]
            selected_raider: TargetRaider | None = None
            for raider in selected_team.raiders:
                if _normalize_character_filter(raider.name) == requested_name:
                    selected_raider = raider
                    break

            if selected_raider is None:
                raise RuntimeError(
                    f"Requested addon character '{requested_character}' not found in team {selected_team.team_id}."
                )

            run_addon_profile(
                config=config,
                config_path=config_path,
                base_url=base_url,
                runner_key=runner_key,
                roster_revision=targets.roster_revision,
                team=selected_team,
                raider=selected_raider,
                addon_profile_path=pathlib.Path(addon_profile_path).resolve(),
                output_root=output_root,
                sim_raid=args.sim_raid,
                sim_difficulty=args.sim_difficulty,
            )
            print("Addon run completed.")
            return 0

        if mode == "single_target":
            requested_name = _normalize_character_filter(requested_character)
            selected_team = teams[0]
            selected_raider: TargetRaider | None = None
            for raider in selected_team.raiders:
                if _normalize_character_filter(raider.name) == requested_name:
                    selected_raider = raider
                    break

            if selected_raider is None:
                raise RuntimeError(
                    f"Requested single-target character '{requested_character}' not found in team {selected_team.team_id}."
                )

            run_single_target_profile(
                config=config,
                config_path=config_path,
                base_url=base_url,
                runner_key=runner_key,
                roster_revision=targets.roster_revision,
                team=selected_team,
                raider=selected_raider,
                output_root=output_root,
            )
            print("Single-target run completed.")
            return 0

        for team in teams:
            run_team(
                config=config,
                config_path=config_path,
                base_url=base_url,
                runner_key=runner_key,
                roster_revision=targets.roster_revision,
                team=team,
                output_root=output_root,
                max_raiders_override=args.max_raiders,
                positive_only_override=True if args.positive_only else None,
                sim_raid=args.sim_raid,
                sim_difficulty=args.sim_difficulty,
            )

        print("All requested teams completed.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
