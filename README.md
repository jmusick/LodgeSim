# Local WoW Nightly Droptimizer

This project runs SimulationCraft locally in a Droptimizer-style flow:
1. Sim your baseline character profile.
2. Sim each candidate item scenario.
3. Rank upgrades by DPS gain.
4. Output CSV and Markdown reports.

## Files

- `droptimizer.py`: Main CLI program.
- `update-simc.ps1`: Downloads and installs the latest SimulationCraft nightly.
- `register-update-task.ps1`: Creates a Windows Task Scheduler job for auto-updates.
- `config.example.json`: Config template.
- `raiders.example.json`: Batch list of raiders to process.
- `run-nightly.ps1`: Helper that writes each run to a timestamped folder.
- `generate_live_candidates.py`: Builds a spec-scoped candidates file directly from Raidbots live static data, without manual Raidbots export files.
- `guild_droptimizer.py`: Runs guild-wide sims from roster and outputs per-item best recipient.

## Prerequisites

- A local SimulationCraft build (`simc.exe`).
- Python 3.10+.
- A baseline character profile in SimulationCraft text format (`.simc`).

## Setup

1. Run `./update-simc.ps1` to install the newest nightly into `tools/simc/nightly/current`.
2. Copy `config.example.json` to `config.json` and edit paths.
3. Create your baseline profile file, for example `input/character.simc`.
4. Generate live raid loot pools:

```powershell
python .\generate_live_candidates.py --all-specs --pool-name all-raids-normal-hc-mythic.live --output-dir .\generated\live-candidates --register-config .\config.json --strict-mapping --default-spec hunter:survival
```

5. Optional for batch mode: copy `raiders.example.json` to `raiders.json` and set `raiders_path` in `config.json`.
6. Optional for direct Armory import: set `armory_url` in `config.json`.

## Naming Convention

Reusable loot pools should be named by class first, then spec:

- `generated/live-candidates/candidates.hunter-survival.all-raids-normal-hc-mythic.live.json`
- `config.hunter-survival.all-raids-hc-mythic.json`

Use config mapping keys in `class:spec` form:

- `hunter:survival`
- `paladin:holy`
- `warrior:fury`

The website runner imports each raider's profile, reads `class` and `spec` from the SimC text, and selects the matching candidates file from `candidates_by_spec` when present. If the exact spec mapping is missing or its generated file is absent, the runner now generates that spec pool on demand from Raidbots live data and persists the mapping back to the config.

## Update SimulationCraft

Manual update:

```powershell
.\update-simc.ps1
```

Force reinstall of the latest file:

```powershell
.\update-simc.ps1 -Force
```

## Candidate Format

Generated candidate files use this shape:

```json
{
  "slots": {
    "head": [
      { "label": "My Item", "simc": "id=12345,bonus_id=67890" }
    ],
    "trinket1": [
      { "label": "Trinket A", "simc": "id=11111" }
    ]
  }
}
```

- `single_upgrades` mode: tests each listed item independently.
- `cartesian` mode: tests all combinations across listed slots (can explode quickly).

## Per-Spec Candidate Mapping

Configs can define spec-specific loot pools:

```json
{
  "candidates_path": "C:/Projects/WoWSim/generated/live-candidates/candidates.hunter-survival.all-raids-normal-hc-mythic.live.json",
  "strict_spec_mapping": true,
  "candidates_by_spec": {
    "hunter:survival": "C:/Projects/WoWSim/generated/live-candidates/candidates.hunter-survival.all-raids-normal-hc-mythic.live.json",
    "paladin:holy": "C:/Projects/WoWSim/generated/live-candidates/candidates.paladin-holy.all-raids-normal-hc-mythic.live.json"
  }
}
```

With `strict_spec_mapping=true`, the website runner fails fast if a raider's class/spec has no configured loot pool. That avoids silently simming the wrong item pool.

## Run Manually

```powershell
python .\droptimizer.py --config .\config.json
```

Batch mode with raiders list:

```powershell
python .\droptimizer.py --config .\config.json --raiders .\raiders.json
```

Single run directly from Armory URL (imports profile automatically):

```powershell
python .\droptimizer.py --config .\config.json --armory-url "https://worldofwarcraft.blizzard.com/en-us/character/us/realm/character-name/"
```

If `raiders_path` is set in `config.json`, batch mode runs automatically without `--raiders`.
If `armory_url` is set in `config.json`, single mode imports from Armory automatically.

### Raiders File Format

```json
{
  "raiders": [
    {
      "name": "WarriorMain",
      "profile_path": "C:/Projects/WoWSim/input/warrior_main.simc"
    },
    {
      "name": "PriestAlt",
      "profile_path": "C:/Projects/WoWSim/input/priest_alt.simc",
      "candidates_path": "C:/Projects/WoWSim/candidates-priest.json"
    }
  ]
}
```

- `name`: label used in console output and folder naming.
- `profile_path`: required SimulationCraft profile path for that raider.
- `armory_url`: alternative to `profile_path`; imports that character profile via SimulationCraft.
- `candidates_path`: optional per-raider item list override.

Batch output writes each raider under `results/<raider-name>/` plus `results/batch_summary.csv`.

## Guild-Wide Item Assignment

Use this when you want to simulate an entire guild roster and answer:
"For each item, which raider gets the biggest upgrade?"

Guild runs are now single-difficulty only by design. You must choose either
`heroic` or `mythic` per run.

### 1) Run guild automation

```powershell
python .\guild_droptimizer.py --config .\config.hunter-survival.all-raids-hc-mythic.json --guild-url "https://worldofwarcraft.blizzard.com/en-us/guild/us/illidan/hidden-lodge/" --difficulty mythic
```

Optional test run on only the first N level-90 raiders:

```powershell
python .\guild_droptimizer.py --config .\config.hunter-survival.all-raids-hc-mythic.json --guild-url "https://worldofwarcraft.blizzard.com/en-us/guild/us/illidan/hidden-lodge/" --difficulty heroic --max-raiders 5
```

Roster-only dry run (fetch roster and write level-90 CSV without running sims):

```powershell
python .\guild_droptimizer.py --config .\config.hunter-survival.all-raids-hc-mythic.json --guild-url "https://worldofwarcraft.blizzard.com/en-us/guild/us/illidan/hidden-lodge/" --difficulty mythic --dry-run
```

### Output

Outputs are written under your configured output dir in a `guild_<guild-slug>/` folder:

- `guild_item_winners.csv`: item-centric table with the best recipient per item.
- `guild_item_winners.md`: markdown version of the same report.
- Per-raider subfolders with full droptimizer outputs (CSV/Markdown/HTML).

By default, guild winner reports always include all items, even if the best
available result is a small downgrade. Use `--positive-only` if you only want
strict upgrades.

## Website Sim Runner (Dev vs Prod)

Use `website_sim_runner.py` to pull team targets from HiddenLodgeWebsite and
push run lifecycle + results back.

Set environment-specific values once:

```powershell
$env:SIM_SITE_BASE_URL_DEV = "http://127.0.0.1:4321"
$env:SIM_RUNNER_KEY_DEV = "dev-runner-key"
$env:SIM_SITE_BASE_URL_PROD = "https://hiddenlodge.example.com"
$env:SIM_RUNNER_KEY_PROD = "prod-runner-key"
```

Run against dev (default):

```powershell
python .\website_sim_runner.py --environment dev --config .\config.guild.json
```

Run against prod:

```powershell
python .\website_sim_runner.py --environment prod --config .\config.guild.json
```

One-off override with explicit values (ignores env var defaults):

```powershell
python .\website_sim_runner.py --environment prod --site-base-url "https://hiddenlodge.example.com" --runner-key "..."
```

### Desktop Launcher UI

For a simple desktop client, run:

```powershell
python .\website_sim_runner_gui.py
```

Or launch the pre-built EXE directly:

```
dist\WoWSim Website Runner Patched.exe
```

To avoid retyping env vars every session, use the launcher script:

1. Copy `.env.simrunner.local.example` to `.env.simrunner.local` and fill values.
2. Start GUI with:

```powershell
.\run-website-gui.ps1
```

The UI provides:

- Dev/Prod selection
- Start button (gated while SimC auto-update is in progress)
- API connection status
- Live console output
- SimC Auto-Update status indicator with dot state (checking / ok / error)
- **Check Now** button to manually re-trigger the SimC nightly update at any time

On launch the GUI automatically runs `update-simc.ps1` in the background to keep SimulationCraft up to date. The Start button stays disabled until the update completes. Set `WOWSIM_AUTO_UPDATE_SIMC_ON_LAUNCH=0` to disable this behaviour.

## Web App UI

Launch a local web UI to start runs and monitor progress live:

1. Install dependency:

```powershell
pip install -r .\requirements-webapp.txt
```

2. Start UI:

```powershell
python .\webapp.py
```

3. Open browser:

`http://127.0.0.1:5050`

The UI lets you:

- Choose config, guild URL, difficulty, level, max raiders.
- Trigger runs in the background.
- Watch live progress and logs.

## Live Loot Pool Generation

Generate and register a single spec pool from Raidbots live data:

```powershell
python .\generate_live_candidates.py --spec-key hunter:survival --pool-name all-raids-normal-hc-mythic.live --register-config .\config.guild.json --strict-mapping
```

Generate and register live candidates for every supported spec in one pass:

```powershell
python .\generate_live_candidates.py --all-specs --pool-name all-raids-normal-hc-mythic.live --output-dir .\generated\live-candidates --register-config .\config.guild.json --strict-mapping --default-spec hunter:survival
```

The website runner also uses the same generator on demand when a requested spec has no generated file yet.

This fetches Raidbots static data from the live manifest and builds a candidates file using raid item metadata, source encounters, class restrictions, and spec restrictions.

Current caveats of the live-data generator:

- It generates candidates using `id=...,ilevel=...` rather than Raidbots-export bonus strings.
- Tier set pieces are included, and WoWSim applies a local override so current-season catalyst-backed tier pieces retain their original raid token boss sources.
- Since the live data does not provide per-item enchant/gem decisions, the generated candidates are intentionally less opinionated.

## Getting a Profile for a Spec You Do Not Play

You do not need your own character for every spec.

Any representative character of the correct class/spec is enough to generate that spec's Raidbots loot pool:

- A guild member of that spec
- A public Armory character for that spec
- A SimC addon export shared by someone who plays it
- A manually prepared SimC profile, if needed

What matters is the class/spec, not the character's name. Raidbots uses that profile to decide which raid drops belong in the pool for that spec.

or with timestamped output folders:

```powershell
.\run-nightly.ps1 -ConfigPath .\config.json
```

The nightly wrapper updates SimulationCraft first. Skip that only if needed:

```powershell
.\run-nightly.ps1 -ConfigPath .\config.json -SkipSimcUpdate
```

## Schedule Nightly on Windows

Run this once in PowerShell (adjust account/time/path):

```powershell
schtasks /Create /TN "WoW-Droptimizer-Nightly" /SC DAILY /ST 02:00 /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Projects\WoWSim\run-nightly.ps1 -ConfigPath C:\Projects\WoWSim\config.json" /F
```

Optional separate updater task (recommended 15-30 minutes before sim):

```powershell
schtasks /Create /TN "WoW-SimC-Update" /SC DAILY /ST 01:30 /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Projects\WoWSim\update-simc.ps1" /F
```

or with helper script:

```powershell
.\register-update-task.ps1 -Time 01:30
```

To test the task immediately:

```powershell
schtasks /Run /TN "WoW-Droptimizer-Nightly"
```

## Notes

- Start with 10k-20k iterations for nightly runs, increase for final gear decisions.
- Keep `max_scenarios` conservative to keep runtime manageable.
- If SimulationCraft JSON changes in a future build, adjust parsing logic in `droptimizer.py`.
