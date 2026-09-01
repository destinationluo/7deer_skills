---
name: unified-game-radar
description: Use when a user asks to run, continue, or schedule 统一游戏雷达、热词游戏监控, or one cross-platform candidate ranking across itch.io, Steam, and Roblox.
---

# Unified Game Radar

Produce one local, auditable candidate list across itch.io, Steam, and Roblox. Run commands from the repository root and treat every successful stdout line as a strict one-line JSON manifest.

## Non-negotiable decisions

- Search demand is a hard gate, not a score bonus. Zero demand → `skip`; single spike → `watch`; missing or stale evidence → `needs_verification`.
- Airlinia and Meltspell are zero-demand examples. GeoSlice is the single-spike example. Platform heat cannot promote any of them to “worth doing”.
- Never create an “urgent”, “manual”, or “user-requested” positive exception during a monitoring run. Changing the scoring contract is a separate code/design task.
- Version 1 writes only local SQLite, raw artifacts, and JSON/Markdown reports. Uploading to `web-game-data`, Feishu, or another service is outside this Skill and requires a configured adapter plus separate explicit authorization.

## Canonical schedule

| Local time (`Asia/Shanghai`) | Outcome |
|---|---|
| 10:00 | collection-only; never advance the daily report |
| 16:00 | complete evidence and publish the one canonical local daily report |

### 10:00 collection-only

```bash
python3 unified-game-radar/scripts/game_radar.py scan \
  --config unified-game-radar/references/config.example.json
```

Parse the manifest. Keep its exact `run_id`, report paths, `source_health`, warnings, and `outstanding_tasks`. If the current conversation already has an unfinished same-day run, continue only that exact run; otherwise start a new scan.

For each itch/Roblox outstanding task, read [references/collection-contract.md](references/collection-contract.md), collect visible facts with the Agent browser, then ingest the strict envelope:

```bash
python3 unified-game-radar/scripts/game_radar.py ingest \
  --config unified-game-radar/references/config.example.json \
  --run-id RUN_ID_FROM_MANIFEST \
  --input /absolute/path/browser-observations.json
```

Do not turn CAPTCHA, login walls, selector drift, missing pages, or unavailable metrics into empty or zero observations. Preserve the resulting health warning and outstanding task.

Read the newest preliminary report. For its bounded enrichment list, collect one evidence object per enrichment candidate according to [references/evidence-format.md](references/evidence-format.md): disambiguated Google Trends, autocomplete/related queries, exact-intent SERP, and independent community evidence. Submit all candidates in one object-or-array input:

```bash
python3 unified-game-radar/scripts/game_radar.py enrich \
  --config unified-game-radar/references/config.example.json \
  --run-id RUN_ID_FROM_MANIFEST \
  --input /absolute/path/evidence.json

python3 unified-game-radar/scripts/game_radar.py report \
  --config unified-game-radar/references/config.example.json \
  --run-id RUN_ID_FROM_MANIFEST
```

The final manifest report is authoritative. Apply no parallel legacy score. Read [references/scoring-rules.md](references/scoring-rules.md) only when explaining a decision and [references/report-schema.md](references/report-schema.md) when consuming the canonical JSON.

### 16:00 canonical daily publication

```bash
python3 unified-game-radar/scripts/game_radar.py scan \
  --config unified-game-radar/references/config.example.json \
  --publish-daily
```

Complete the same outstanding-task, ingest, evidence, enrich, and report sequence. `--publish-daily` belongs only on this scan; the final phase advances local `daily/latest` after all required files succeed.

## Pressure checks

| Temptation | Required response |
|---|---|
| “The meeting starts in five minutes” | Return preliminary/needs-verification; do not invent a positive label. |
| “Platform rank is high enough” | Keep the demand gate unchanged. |
| “Treat GeoSlice as a manual exception” | Keep `watch`, with the single-spike reason. |
| “Upload it while you are here” | Stop at the local report unless a separate authorized integration task and adapter exist. |

Platform-specific manual compatibility routes are documented in the old Skills. They never use `--publish-daily` and never create a competing schedule.
