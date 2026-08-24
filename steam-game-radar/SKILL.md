---
name: steam-game-radar
description: Use when a user asks to scan Steam trends, rank released or upcoming game opportunities, import a local SteamDB export, or configure the daily Steam radar schedule.
---

# Steam Game Radar

Run every command from the target project root. This radar is separate from
HTML5/browser-playable discovery and produces released and unreleased Steam
rankings.

## Manual routes

For `跑 Steam 雷达`, run:

```bash
python3 steam-game-radar/scripts/steam_radar.py scan \
  --config steam-game-radar/references/config.example.json
```

For `导入 SteamDB 榜单并跑 Steam 雷达`, first require a human-provided
本地 CSV/JSON file and one explicit view, then run:

```bash
python3 steam-game-radar/scripts/steam_radar.py import-steamdb \
  --config steam-game-radar/references/config.example.json \
  --view wishlist_activity \
  --input /path/to/steamdb-export.csv
```

After the scan preliminary report, collect Google, YouTube, and Reddit
evidence for the configured Top 10 candidates (default). For an import
preliminary report, this same enrichment is optional. When enriching, save the
versioned evidence file and run:

```bash
python3 steam-game-radar/scripts/steam_radar.py enrich \
  --config steam-game-radar/references/config.example.json \
  --run-id 20260824T030000Z-a1b2c3d4 \
  --input /path/to/enrichment.json
```

## Schedules

Use this host-agent schedule registration; its payload owns the two-phase run:

```json
{
  "name": "Daily Steam Game Radar",
  "expression": "0 11 * * *",
  "timezone": "Asia/Shanghai",
  "payload": "At the target project root, run python3 steam-game-radar/scripts/steam_radar.py scan --config steam-game-radar/references/config.example.json; read its preliminary report; collect Google, YouTube, and Reddit evidence for preliminary Top 10; write the versioned evidence file; run python3 steam-game-radar/scripts/steam_radar.py enrich --config steam-game-radar/references/config.example.json --run-id <preliminary_run_id> --input <enrichment_file>; report both output paths and any warnings."
}
```

For a host without agent scheduling, conventional cron is scan-only:

```cron
TZ=Asia/Shanghai
0 11 * * * cd /absolute/path/to/project && python3 steam-game-radar/scripts/steam_radar.py scan --config steam-game-radar/references/config.example.json
```

This conventional cron is preliminary-only unless a separate agent performs
enrichment. This Skill never edits cron or any scheduled task.

## Policy and outputs

SteamDB is manual-import-only. Never request, never browse, never crawl, never
scrape, and never refresh steamdb.info automatically; accept only a local
CSV/JSON export explicitly supplied by a human（人工提供）.

Preliminary output starts at
`reports/steam-game-radar/{run_id}.preliminary.json`; final output starts at
`reports/steam-game-radar/{run_id}.final.json`, each paired with Markdown and
monotonic `latest.*`. Exit codes: 0 success, 2 input, 3 provider, 4 config,
5 persistence, 6 busy; unexpected failures use 1.

Load details only when needed:

- Official endpoints and fallback: `references/data-sources.md`
- Local import contract: `references/steamdb-import-format.md`
- Scoring and ordering: `references/scoring-rules.md`
- Canonical report fields: `references/report-template.md`
