---
name: steam-game-radar
description: Use when a user explicitly asks for the legacy Steam-only radar or a human-supplied local SteamDB import.
---

# Steam Game Radar Compatibility Route

The scheduled standalone Steam workflow is deprecated. Current monitoring is manual-only and delegates ranking to `unified-game-radar`; it does not publish a separate daily list.

From the repository root, start a Steam-only unified run:

```bash
python3 unified-game-radar/scripts/game_radar.py scan \
  --config unified-game-radar/references/config.example.json \
  --platform steam
```

Parse the one-line manifest and keep its exact `run_id`. Steam normally has no browser ingest task. Follow `unified-game-radar/SKILL.md` to collect one evidence object for every enrichment candidate, then run the unified `enrich` and `report` commands. This compatibility route never advances the daily report.

SteamDB remains a separate legacy manual import only when the user explicitly supplies a local CSV/JSON export and requests that import. Never browse, crawl, refresh, or automate steamdb.info. It is not a source in the unified scheduled workflow. See the [legacy import contract](references/steamdb-import-format.md) only for that explicit manual route.
