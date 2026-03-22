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
- `candidates.example.json`: Candidate item template.
- `raiders.example.json`: Batch list of raiders to process.
- `run-nightly.ps1`: Helper that writes each run to a timestamped folder.
- `convert_raidbots_profileset.py`: Converts Raidbots profileset simc exports to candidates JSON.
- `merge_candidates.py`: Merges multiple candidates JSON files into one deduplicated pool.
- `guild_droptimizer.py`: Runs guild-wide sims from roster and outputs per-item best recipient.

## Prerequisites

- A local SimulationCraft build (`simc.exe`).
- Python 3.10+.
- A baseline character profile in SimulationCraft text format (`.simc`).

## Setup

1. Run `./update-simc.ps1` to install the newest nightly into `tools/simc/nightly/current`.
2. Copy `config.example.json` to `config.json` and edit paths.
2. Create your baseline profile file, for example `input/character.simc`.
3. Copy `candidates.example.json` to `candidates.json` and update gear options.
4. Update `config.json` to point to your `candidates.json`.
5. Optional for batch mode: copy `raiders.example.json` to `raiders.json` and set `raiders_path` in `config.json`.
6. Optional for direct Armory import: set `armory_url` in `config.json`.

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

`candidates.json` uses this shape:

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
python .\droptimizer.py --config .\config.json --armory-url "https://worldofwarcraft.blizzard.com/en-us/character/us/malganis/beastndesist/"
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
python .\guild_droptimizer.py --config .\config.beastndesist.all-raids-hc-mythic.json --guild-url "https://worldofwarcraft.blizzard.com/en-us/guild/us/illidan/hidden-lodge/" --difficulty mythic
```

Optional test run on only the first N level-90 raiders:

```powershell
python .\guild_droptimizer.py --config .\config.beastndesist.all-raids-hc-mythic.json --guild-url "https://worldofwarcraft.blizzard.com/en-us/guild/us/illidan/hidden-lodge/" --difficulty heroic --max-raiders 5
```

Roster-only dry run (fetch roster and write level-90 CSV without running sims):

```powershell
python .\guild_droptimizer.py --config .\config.beastndesist.all-raids-hc-mythic.json --guild-url "https://worldofwarcraft.blizzard.com/en-us/guild/us/illidan/hidden-lodge/" --difficulty mythic --dry-run
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

To avoid retyping env vars every session, use the launcher script:

1. Copy `.env.simrunner.local.example` to `.env.simrunner.local` and fill values.
2. Start GUI with:

```powershell
.\run-website-gui.ps1
```

The UI provides:

- Dev/Prod selection
- Start button
- API connection status
- Live console output

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

## Raidbots Loot Pool Import

Convert a Raidbots Droptimizer export (profileset simc text) into candidates JSON:

```powershell
python .\convert_raidbots_profileset.py --input .\input\beastndesist_raidbots_heroic.simc --output .\candidates.beastndesist.voidspire-heroic.json
```

Merge Heroic + Mythic pools:

```powershell
python .\merge_candidates.py --input .\candidates.beastndesist.voidspire-mythic.json .\candidates.beastndesist.voidspire-heroic.json --output .\candidates.beastndesist.voidspire-hc-mythic.json
```

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
