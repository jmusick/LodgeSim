#!/usr/bin/env python3
"""Merge multiple candidates JSON files into one deduplicated pool."""

from __future__ import annotations

import argparse
import json
import pathlib
from collections import defaultdict
from typing import Any


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected object JSON in {path}")
    return payload


def merge(paths: list[pathlib.Path]) -> dict[str, Any]:
    slots: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)

    for path in paths:
        payload = load_json(path)
        slot_map = payload.get("slots", {})
        if not isinstance(slot_map, dict):
            continue

        for slot, items in slot_map.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                simc = str(item.get("simc", "")).strip()
                if not simc or simc in seen[slot]:
                    continue
                seen[slot].add(simc)
                label = str(item.get("label", simc)).strip()
                slots[slot].append({"label": label, "simc": simc})

    return {"slots": dict(slots)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge candidates JSON files.")
    parser.add_argument("--input", nargs="+", required=True, help="Input candidates JSON files.")
    parser.add_argument("--output", required=True, help="Output candidates JSON file.")
    args = parser.parse_args()

    in_paths = [pathlib.Path(p).resolve() for p in args.input]
    out_path = pathlib.Path(args.output).resolve()

    merged = merge(in_paths)
    out_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")

    count = sum(len(v) for v in merged["slots"].values())
    print(f"Merged {len(in_paths)} files -> {count} candidates across {len(merged['slots'])} slots")
    print(f"Output: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
