---
name: html5-game-radar
description: Use when a user explicitly asks for the legacy HTML5 or itch.io-only game radar compatibility route.
---

# HTML5 Game Radar Compatibility Route

This standalone workflow is deprecated. It is manual-only and delegates current monitoring to `unified-game-radar`; it has no schedule, no 0–20 legacy score, and no automatic Feishu delivery.

From the repository root, start an itch-only unified run:

```bash
python3 unified-game-radar/scripts/game_radar.py scan \
  --config unified-game-radar/references/config.example.json \
  --platform itch
```

Parse the one-line manifest, keep the exact `run_id`, and follow `unified-game-radar/SKILL.md` for the itch browser envelope, `ingest`, evidence, `enrich`, and `report` commands. This compatibility route never passes a daily publication flag and cannot create a competing daily report.

Historical HTML5 methodology remains in `references/seo_arbitrage_logic.md` and `references/platform_signals.md`; it is context only and must not replace the unified demand gate.
