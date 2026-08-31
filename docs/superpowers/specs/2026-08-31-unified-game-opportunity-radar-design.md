# Unified Game Opportunity Radar Design

**Status:** Approved by user on 2026-08-31  
**Goal:** Build one local Agent Skill that scans itch.io, Steam, and Roblox, stores comparable historical snapshots, enforces real-search-demand gates, and publishes one daily ranked opportunity report. A future adapter may import the versioned report into `https://web-game-data.vercel.app/overview`, but the local radar must run independently.

## Context

The repository currently has an early `html5-game-radar` and a substantially implemented Steam radar on the `feature/steam-game-radar` branch. The existing HTML5 score incorrectly allows low competition to compensate for missing demand, and it does not preserve enough history to distinguish a sustained trend from a one-day spike. Steam and Roblox expose different platform metrics, so their raw values cannot be added directly to itch.io metrics.

The user wants one daily list across all three platforms. Games with zero search demand or only a single declining trend spike must never be labeled “worth doing.” Short term, collection and judgment run locally through a Skill/Agent. Long term, the existing web platform may consume the radar’s canonical JSON output.

## Decisions

1. Create one public orchestrator Skill named `unified-game-radar`.
2. Keep platform collection isolated behind three collectors: itch.io, Steam, and Roblox.
3. Normalize platform-local momentum into a comparable 0–30 discovery score; never compare raw platform metrics directly.
4. Store immutable observations in SQLite so rank and engagement deltas can be calculated across runs.
5. Apply a search-demand gate before action labels. The total score cannot override this gate.
6. Produce one canonical, versioned JSON report and one Markdown report per run.
7. Keep collection and scoring independent from the web platform. Future platform integration consumes canonical reports through a small adapter or API import.
8. Preserve existing Steam provider policy: automated collection uses Steam-owned endpoints; SteamDB remains manual-import-only.
9. Treat missing values as `null`, never as zero, low competition, or positive evidence.
10. Use Python 3.11+ and the standard library for the core so the local radar is easy to schedule.

## Scope

### In scope

- Daily and manual scans across itch.io, Steam, and Roblox.
- Platform-specific collection, normalization, snapshots, growth calculations, and source-health reporting.
- Cross-platform candidate identity and conservative deduplication.
- Google Trends/search-suggestion evidence supplied by the Agent through a versioned evidence bundle.
- Independent community evidence from Reddit, YouTube, X, Instagram, TikTok, or other explicitly typed sources.
- Search-result content-gap evidence supplied by the Agent.
- Hard demand states: `pass`, `early_watch`, `fail`, and `unknown`.
- One unified score, action label, JSON report, and Markdown report.
- Offline fixture tests and an opt-in live smoke test for each provider.
- A stable export contract for future `web-game-data` integration.

### Out of scope

- Direct writes to the existing web platform in the first version.
- Automatic domain registration, site creation, publishing, or backlink work.
- Automated SteamDB crawling or scraping.
- Claiming private Steam wishlist counts.
- Treating platform popularity alone as proof of Google search demand.
- Merging same-name games without strong identity evidence.

## Architecture

```text
itch collector ───┐
steam collector ──┼──> observations/snapshots ──> platform momentum
roblox collector ─┘                                  │
                                                     v
Agent evidence bundle ──> demand gate + external spread + SEO gap
                                                     │
                                                     v
                                      normalized candidates/deduplication
                                                     │
                                                     v
                                     one canonical JSON + Markdown ranking
                                                     │
                                                     v
                                  optional future web-game-data import adapter
```

The Python CLI owns deterministic collection where stable public endpoints are available, persistence, pure scoring, and report generation. The Agent owns browser-dependent discovery and semantic evidence collection. Browser-derived observations are ingested through a strict JSON contract rather than allowing scoring code to depend on a live browser.

## File Structure

```text
unified-game-radar/
├── SKILL.md
├── scripts/
│   └── game_radar.py
├── unified_game_radar/
│   ├── __init__.py
│   ├── config.py
│   ├── errors.py
│   ├── schemas.py
│   ├── storage.py
│   ├── identity.py
│   ├── normalize.py
│   ├── demand.py
│   ├── score.py
│   ├── report.py
│   ├── orchestration.py
│   └── collectors/
│       ├── __init__.py
│       ├── base.py
│       ├── itch.py
│       ├── steam.py
│       └── roblox.py
├── references/
│   ├── config.example.json
│   ├── collection-contract.md
│   ├── evidence-format.md
│   ├── scoring-rules.md
│   └── report-schema.md
└── tests/
    ├── fixtures/
    └── test_*.py
```

Only `scripts/game_radar.py` parses command-line arguments. Package modules expose focused, import-safe interfaces.

## CLI Contract

```bash
python3 unified-game-radar/scripts/game_radar.py scan \
  --config unified-game-radar/references/config.example.json

python3 unified-game-radar/scripts/game_radar.py ingest \
  --config unified-game-radar/references/config.example.json \
  --input /path/to/browser-observations.json

python3 unified-game-radar/scripts/game_radar.py enrich \
  --config unified-game-radar/references/config.example.json \
  --run-id 20260831T020000Z-a1b2c3d4 \
  --input /path/to/evidence.json

python3 unified-game-radar/scripts/game_radar.py report \
  --config unified-game-radar/references/config.example.json \
  --run-id 20260831T020000Z-a1b2c3d4
```

- `scan` runs deterministic providers and creates a preliminary report plus an Agent collection manifest.
- `ingest` validates and stores browser observations for itch.io or Roblox.
- `enrich` validates demand/community/SEO evidence and calculates final actions.
- `report` regenerates canonical output deterministically from persisted data.

Every successful command prints one compact JSON manifest line containing `run_id`, phase, report paths, warnings, source health, and outstanding Agent tasks.

## Unified Candidate Schema

All persisted records include `schema_version: 1`.

```json
{
  "schema_version": 1,
  "candidate_id": "steam:123456",
  "name": "Example Game",
  "normalized_name": "example game",
  "developer": "Example Studio",
  "platform": "steam",
  "platform_id": "123456",
  "url": "https://store.steampowered.com/app/123456/",
  "first_seen_at": "2026-08-31T02:00:00Z",
  "observed_at": "2026-08-31T02:00:00Z",
  "release_at": "2026-08-30T00:00:00Z",
  "source_rank": 12,
  "previous_rank": 35,
  "raw_metrics": {
    "current_players": 12000,
    "review_count": 250
  },
  "source_kind": "steam_official",
  "evidence_urls": []
}
```

Unknown observations are omitted or explicitly `null`; they are never serialized as fabricated zero values.

## Platform Collection and Momentum

Each collector emits raw observations and a platform-local heat score from 0–100. The normalizer converts heat to a same-run percentile and then to `platform_score` from 0–30.

### itch.io

- Monitor browser-playable newest releases and a popularity surface.
- Signals: first-seen recency, movement from newest into popular surfaces, originality/spam filtering, author credibility, and browser-playable status.
- Copied commercial games, mass reuploads, and Jam-only entries are excluded or heavily penalized.
- Reddit/community evidence is not counted here; it belongs to the independent-spread dimension.

### Steam

- Automated collection uses Steam-owned endpoints only.
- Signals: official chart/category rank and change, release recency, current-player change, recommendation/review-count change when available, and persistence across runs.
- Released and upcoming Steam records retain separate internal heat formulas but enter the same final candidate pool after normalization.
- SteamDB may be imported from a user-provided local CSV/JSON file only.

### Roblox

- Monitor visible Rising/Up-and-Coming/Charts observations through an Agent browser collection contract or stable Roblox-owned endpoint when available.
- Signals: chart rank and change, concurrent-player change, visit/favorite change when publicly available, first-seen recency, and consecutive chart appearances.
- Personalized recommendation surfaces are not treated as global rank unless the collection evidence identifies the surface and locale.

### Snapshot requirement

The default schedule is twice daily at 10:00 and 16:00 Asia/Shanghai. A first observation may establish recency and presence but cannot claim growth. Rank or metric growth requires at least two compatible observations from the same source and surface.

## Identity and Deduplication

1. Exact platform IDs are authoritative within a platform.
2. Cross-platform records merge only when normalized title plus developer, official domain, or an explicit alias mapping agrees.
3. Same-name records with different developers remain separate.
4. Every merged candidate retains all source-specific records and URLs.
5. Manual alias decisions are versioned in configuration and visible in report provenance.

## Search-Demand Evidence

The Agent supplies a versioned evidence record for each enrichment candidate:

```json
{
  "candidate_id": "steam:123456",
  "observed_at": "2026-08-31T06:00:00Z",
  "trends": {
    "timeframe": "now 7-d",
    "geo": "US",
    "daily_values": [0, 0, 8, 100, 32, 18, 22],
    "latest_is_partial": true,
    "comparison_term": "gpts",
    "comparison_average": 41
  },
  "autocomplete_queries": [],
  "related_queries": [],
  "external_evidence": [],
  "serp": {
    "query": "Example Game game",
    "relevant_nonofficial_results": null,
    "guide_results": null,
    "evidence_url": "https://www.google.com/search?q=Example+Game+game"
  }
}
```

The evidence schema distinguishes an actual count of zero from a collection failure (`null`). Evidence URLs and observation times are mandatory for positive claims.

## Demand Gate

The gate runs before action labels.

### `pass`

All of the following are required:

1. Search is nonzero on at least two completed calendar days within the selected window.
2. The latest completed day is at least 30% of the completed-window peak.
3. At least one supporting intent signal exists: relevant autocomplete, related query, durable second wave, or independently verified long-tail demand.

### `early_watch`

Used when demand is plausible but not durable, including:

- one sharp spike followed by a decline;
- only one completed nonzero day;
- the latest point is partial and durability cannot yet be evaluated;
- platform/community acceleration exists but search evidence is not mature.

An `early_watch` candidate can never receive a “worth doing” or “immediate action” label.

### `fail`

Used when Trends is zero and there are no relevant autocomplete or related queries, or when a previously isolated spike has decayed without supporting intent. A failed candidate is eliminated regardless of platform score or SEO competition.

### `unknown`

Used when evidence collection failed or is stale. It never receives a positive action.

## Unified Score

Scores are calculated only from verified, non-missing evidence.

| Dimension | Maximum |
|---|---:|
| Platform-local momentum percentile | 30 |
| Search demand strength and durability | 30 |
| Independent external spread | 20 |
| SEO content gap | 20 |
| **Total** | **100** |

Action thresholds after the hard gate:

- 80–100: `immediate_action`
- 65–79: `worth_content_mvp`
- 50–64: `watch`
- below 50: `skip`

Overrides:

- `fail` always becomes `skip`.
- `early_watch` always becomes `watch`, regardless of numeric score.
- `unknown` becomes `needs_verification`.
- A candidate with no independent evidence cannot be `immediate_action`.

External evidence must be independent of the developer when scored as organic spread. Developer announcements remain provenance but receive no organic-spread credit.

## Persistence

Use SQLite in the configured data directory, defaulting to:

```text
data/unified-game-radar/radar.sqlite3
```

Tables store runs, source health, candidates, platform observations, identity links, evidence bundles, scores, and report publications. Writes use transactions and foreign keys. Reports are rebuilt from persisted canonical records, not temporary files.

Runtime artifacts:

```text
data/unified-game-radar/raw/{run_id}/...
reports/unified-game-radar/{run_id}.preliminary.json
reports/unified-game-radar/{run_id}.preliminary.md
reports/unified-game-radar/{run_id}.final.json
reports/unified-game-radar/{run_id}.final.md
reports/unified-game-radar/latest.json
reports/unified-game-radar/latest.md
```

Raw browser/provider artifacts are retained for a configurable period and redacted before persistence. `latest.*` advances monotonically by run timestamp.

## Failure Handling

- One collector failure does not abort healthy collectors.
- Every report includes source health: `fresh`, `partial`, `stale`, `unavailable`, or `not_run`.
- Stale observations are labeled and cannot claim current growth.
- HTTP 429, CAPTCHA, authentication, and selector drift use bounded retries and explicit warning codes.
- Browser collection failures create outstanding Agent tasks rather than fabricated results.
- Demand evidence failure caps the action at `needs_verification`.
- Persistence or schema corruption is fatal and returns a nonzero exit code.
- Concurrent runs use a project-scoped lock.

## Agent Skill Workflow

The public triggers include `跑统一游戏雷达`, `监控今天的热词游戏`, and scheduled execution.

1. Run `scan` and parse its one-line manifest.
2. Complete outstanding itch.io/Roblox browser observations using the exact collection contract.
3. Run `ingest` for those observations.
4. Read the preliminary enrichment list.
5. Collect Google Trends, autocomplete, related-query, SERP, and independent community evidence for the bounded Top N.
6. Run `enrich` with the exact run ID.
7. Return one unified report. Do not push to Feishu or the web platform unless the user separately authorizes that external write.

## Web Platform Integration Boundary

Version 1 performs no network write to `web-game-data.vercel.app`. The canonical final JSON is the integration contract. A future adapter may:

- upload/import the final report;
- map candidates and evidence into platform tables;
- preserve run ID, schema version, source health, and evidence URLs;
- reject unsupported schema versions;
- remain retryable and idempotent.

The web platform should act as control plane and system of record. Volatile browser research stays in the Agent layer.

## Testing

### Unit tests

- Schema validation and `null` handling.
- Platform-local heat transforms and percentile normalization.
- Identity normalization and conservative deduplication.
- SQLite transactions, snapshot retrieval, and compatible delta selection.
- Demand-gate boundaries.
- Total-score thresholds and gate overrides.
- Deterministic report ordering.

### Provider/parser fixtures

- itch.io newest/popular fixtures, including mass reuploads and Jam games.
- Steam official endpoint fixtures for released and upcoming games.
- Roblox chart/browser observation fixtures with rank and engagement metrics.

### Regression cases

- Trends all zero plus low SERP competition must be `skip`.
- One-day peak followed by a large decline must be `early_watch`.
- Missing Trends data must be `unknown`, not zero or low competition.
- Two completed nonzero days with maintained latest interest and supporting intent may pass.
- A developer-only announcement cannot satisfy independent spread.
- A source outage cannot reuse stale observations as current growth.

### Integration and smoke tests

- Offline end-to-end run using all three platform fixtures produces one preliminary and one final report.
- Re-running the same run/import is idempotent.
- An opt-in live smoke test checks each official provider without becoming part of the default suite.

## Acceptance Criteria

1. One command/Agent workflow produces one ranked list across itch.io, Steam, and Roblox.
2. Every row shows platform provenance, evidence timestamps, demand state, component scores, total score, action, and warnings.
3. Historical observations survive process restarts and enable twice-daily growth comparisons.
4. Zero-demand candidates never receive a positive action.
5. Single-spike declining candidates never receive a positive action.
6. Missing collection data never receives positive credit.
7. A full offline test run passes without network access.
8. The final JSON is versioned and suitable for future idempotent import into the existing web platform.
