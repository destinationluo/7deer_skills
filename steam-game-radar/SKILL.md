---
name: steam-game-radar
description: Use when a user asks to scan Steam trends, rank released or upcoming game opportunities, import a local SteamDB export, or configure the daily Steam radar schedule.
---

# Steam Game Radar

Run every command from the target project root. This radar is separate from
HTML5/browser-playable discovery and produces released and unreleased Steam
rankings.

## Path modes

Always run from the target project root. Relative `data_dir` and `report_dir`
values resolve from that target project root.

**Repository checkout:** the three commands under Manual routes use the
`steam-game-radar/...` prefix.

**Installed in the target project:** use these `.agent/skills` paths:

```bash
python3 .agent/skills/steam-game-radar/scripts/steam_radar.py scan \
  --config .agent/skills/steam-game-radar/references/config.example.json

python3 .agent/skills/steam-game-radar/scripts/steam_radar.py import-steamdb \
  --config .agent/skills/steam-game-radar/references/config.example.json \
  --view wishlist_activity \
  --input /path/to/steamdb-export.csv

python3 .agent/skills/steam-game-radar/scripts/steam_radar.py enrich \
  --config .agent/skills/steam-game-radar/references/config.example.json \
  --run-id 20260824T030000Z-a1b2c3d4 \
  --input /path/to/enrichment.json
```

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

After the scan preliminary report, parse its manifest and collect Google,
YouTube, and Reddit evidence for every listed enrichment AppID (one total
Top 10 maximum by default). For an import preliminary report, this same
enrichment is optional. When enriching, save the versioned evidence file and
run:

```bash
python3 steam-game-radar/scripts/steam_radar.py enrich \
  --config steam-game-radar/references/config.example.json \
  --run-id 20260824T030000Z-a1b2c3d4 \
  --input /path/to/enrichment.json
```

## Schedules

The following are scheduler-neutral parameters to map into the host Agent
scheduler UI or API; they are not a universal registration JSON.

| Parameter | Value |
|---|---|
| Name | Daily Steam Game Radar |
| Cron expression | `0 11 * * *` |
| Timezone | `Asia/Shanghai` |
| Payload | Execute the Agent payload below from the target project root |

### Manifest contract

Each successful command writes exactly one compact JSON manifest line to
stdout. A preliminary example is:

```jsonl
{"enrichment_candidate_appids":[123456],"phase":"preliminary","report_json":"/absolute/reports/steam-game-radar/20260824T030000Z-a1b2c3d4.preliminary.json","report_markdown":"/absolute/reports/steam-game-radar/20260824T030000Z-a1b2c3d4.preliminary.md","run_id":"20260824T030000Z-a1b2c3d4","schema_version":1,"warnings":[]}
```

### Agent payload

1. Run `scan` with the active path mode, then capture and parse its exact
   one-line JSON manifest.
2. From that manifest, read `run_id`, `report_json`, `report_markdown`, and
   `warnings`.
3. The `enrichment_candidate_appids` manifest field is authoritative:
   research every AppID in `enrichment_candidate_appids`, then collect Google,
   YouTube, and Reddit evidence. It is one total Top N across released and
   unreleased eligible reported candidates, selected by canonical
   `candidate_sort_key`; it is not N per pool and is already limited by
   configured `enrichment_top_n`.
4. Using that exact `run_id`, write `enrichment.json` according to
   `references/scoring-rules.md`.
5. With the active path mode, run `enrich` using the captured `run_id` and the
   evidence file.
6. Immediately capture and consume the final one-line manifest, including its
   report paths and warnings.

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
monotonic `latest.*`.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | Success, including stale fallback no older than 72 hours |
| 1 | Unexpected failure; traceback is emitted |
| 2 | Input or schema validation failure |
| 3 | Provider failure with no usable fallback |
| 4 | Configuration failure |
| 5 | Snapshot or report persistence failure |
| 6 | Another run holds the project lock |

## References

Load details only when needed:

- Official endpoints and fallback: `references/data-sources.md`
- Local import contract: `references/steamdb-import-format.md`
- Scoring and ordering: `references/scoring-rules.md`
- Canonical report fields: `references/report-template.md`
