#!/usr/bin/env python3
"""Convert Raidbots Droptimizer profileset simc lines into candidates JSON."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
from collections import defaultdict

SLOT_NORMALIZATION = {
    "shoulder": "shoulders",
    "wrist": "wrists",
}

PROFILESET_RE = re.compile(r'^profileset\..*\+=(?P<slot>[a-z_0-9]+)=,(?P<attrs>.*)$')


def normalize_slot(slot: str) -> str:
    return SLOT_NORMALIZATION.get(slot, slot)


def parse_attrs(attrs: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in attrs.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def build_simc_item(attrs: dict[str, str]) -> str | None:
    if "id" not in attrs:
        return None

    pieces = [f"id={attrs['id']}"]

    # Keep bonus_id for the intended raid difficulty/track.
    if "bonus_id" in attrs and attrs["bonus_id"]:
        pieces.append(f"bonus_id={attrs['bonus_id']}")

    # Preserve extra fields when present (rare in profilesets but useful).
    for key in ("crafted_stats", "crafting_quality", "drop_level", "gem_id", "enchant_id"):
        if key in attrs and attrs[key]:
            pieces.append(f"{key}={attrs[key]}")

    return ",".join(pieces)


def convert(simc_text: str) -> dict[str, object]:
    slots: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)
    last_comment = ""

    for raw_line in simc_text.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            last_comment = line.lstrip("#").strip()
            continue

        m = PROFILESET_RE.match(line)
        if not m:
            continue

        slot = normalize_slot(m.group("slot"))
        attrs = parse_attrs(m.group("attrs"))
        simc_item = build_simc_item(attrs)
        if not simc_item:
            continue

        if simc_item in seen[slot]:
            continue

        seen[slot].add(simc_item)
        label = last_comment if last_comment else f"{slot} id={attrs.get('id', 'unknown')}"
        slots[slot].append({"label": label, "simc": simc_item})

    return {"slots": dict(slots)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert Raidbots profileset lines into candidates JSON.")
    parser.add_argument("--input", required=True, help="Path to Raidbots simc export text file.")
    parser.add_argument("--output", required=True, help="Path to candidates JSON output file.")
    args = parser.parse_args()

    in_path = pathlib.Path(args.input).resolve()
    out_path = pathlib.Path(args.output).resolve()

    simc_text = in_path.read_text(encoding="utf-8")
    payload = convert(simc_text)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    count = sum(len(v) for v in payload["slots"].values())
    print(f"Wrote {count} candidates across {len(payload['slots'])} slots to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
