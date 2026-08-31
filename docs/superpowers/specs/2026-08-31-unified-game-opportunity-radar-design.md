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
11. Reuse the committed Steam provider, HTTP, schema, trend, snapshot, and locking behavior from clean commit `cbdb1bb`; do not copy the dirty Steam worktree.
12. Local SQLite is authoritative in version 1. The web platform is a downstream read-only consumer until a separate migration design changes ownership.

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
  --run-id 20260831T020000Z-a1b2c3d4 \
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
- `ingest` validates and stores browser observations for itch.io or Roblox and requires the exact originating run ID.
- `enrich` validates demand/community/SEO evidence and calculates final actions.
- `report` regenerates canonical output deterministically from persisted data.

Every successful command prints one compact JSON manifest line containing `run_id`, phase, report paths, warnings, source health, and outstanding Agent tasks.

## Identity and Observation Schemas

All persisted records include `schema_version: 1`.

`GameIdentity` is the stable aggregate that receives demand evidence and final
actions. Its `opportunity_id` is a persisted UUID and never changes when a new
platform record is linked.

```json
{
  "schema_version": 1,
  "opportunity_id": "0f840f6f-5c62-4ca6-9d53-e0be9ab2740b",
  "name": "Example Game",
  "normalized_name": "example game",
  "developer": "Example Studio",
  "official_domain": "example.com",
  "platform_records": [
    {
      "platform": "steam",
      "platform_id": "123456",
      "url": "https://store.steampowered.com/app/123456/"
    }
  ]
}
```

Every collected metric belongs to a `PlatformObservation` rather than directly
to the aggregate identity:

```json
{
  "schema_version": 1,
  "observation_id": "steam:123456:most-played:20260831T020000Z",
  "run_id": "20260831T020000Z-a1b2c3d4",
  "platform": "steam",
  "platform_id": "123456",
  "url": "https://store.steampowered.com/app/123456/",
  "observed_at": "2026-08-31T02:00:00Z",
  "release_at": "2026-08-30T00:00:00Z",
  "provider": "steam_official",
  "surface": "most_played",
  "geo": "US",
  "locale": "en",
  "query_parameters": {},
  "metric_definition_version": 1,
  "source_rank": 12,
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

Each collector emits raw observations and a deterministic platform-local heat
score from 0–100. Candidates below an absolute heat floor of 30 are excluded
from enrichment. Remaining candidates form a separate cohort per platform and
compatible discovery surface.

For a cohort of at least five candidates, sort heat descending, assign tied
items their average one-based rank, and calculate:

```text
percentile = (cohort_size - average_rank) / (cohort_size - 1)
platform_score = min(30 * heat / 100, 30 * percentile)
```

For cohorts smaller than five, `platform_score = min(15, 30 * heat / 100)`.
If all candidates tie, every candidate receives `min(15, 30 * heat / 100)`.
Persist scores rounded to one decimal. This prevents a weak one-item cohort
from manufacturing a 30-point platform signal.

### itch.io

- Monitor browser-playable newest releases and a popularity surface.
- Signals: first-seen recency, movement from newest into popular surfaces, originality/spam filtering, author credibility, and browser-playable status.
- Copied commercial games, mass reuploads, and Jam-only entries are excluded or heavily penalized.
- Reddit/community evidence is not counted here; it belongs to the independent-spread dimension.

Exact heat components:

- First-seen age: 25 points for ≤24h, 15 for ≤3d, 5 for ≤7d, otherwise 0.
- Popular-surface rank: 35 points for top 10, 25 for top 25, 15 for top 50, otherwise 0.
- Compatible popular-rank improvement: 20 points for ≥20 places, 10 for 5–19, 5 for 1–4, otherwise 0.
- Originality confidence: 10 for verified original, 5 for unknown, 0 for a known reupload; known reuploads are excluded.
- Browser-playable verification: 5 when verified, otherwise 0.
- Author history: 5 when a non-spam author has at least two prior original releases, otherwise 0.

### Steam

- Automated collection uses Steam-owned endpoints only.
- Signals: official chart/category rank and change, release recency, current-player change, recommendation/review-count change when available, and persistence across runs.
- Released and upcoming Steam records retain separate internal heat formulas but enter the same final candidate pool after normalization.
- SteamDB may be imported from a user-provided local CSV/JSON file only.

Released-game heat components:

- Official chart/category rank: 25 points for top 10, 20 for top 25, 15 for top 50, 8 for top 100, otherwise 0.
- Compatible rank improvement: 25 points for ≥20 places, 18 for 10–19, 10 for 5–9, 5 for 1–4, otherwise 0.
- One-day current-player growth: 25 points for ≥100%, 20 for ≥50%, 12 for ≥20%, 5 for >0%, otherwise 0.
- Current-player scale: 15 points for ≥10,000, 10 for ≥1,000, 5 for ≥100, otherwise 0.
- Release recency: 10 points for ≤7d, 5 for ≤30d, otherwise 0.

Upcoming-game heat components:

- Coming-soon rank: 30 points for top 10, 24 for top 25, 15 for top 50, otherwise 0.
- Compatible rank improvement: 30 points for ≥20 places, 20 for 10–19, 10 for 1–9, otherwise 0.
- Verified follower/wishlist growth from an allowed manual import: 20 points for ≥50%, 15 for ≥20%, 8 for >0%, otherwise 0.
- Release proximity: 10 points for ≤7d, 7 for ≤30d, 3 when later, otherwise 0 when unknown.
- Presence on at least two Steam-owned discovery surfaces in the same run: 10 points, otherwise 0.

### Roblox

- Monitor visible Rising/Up-and-Coming/Charts observations through an Agent browser collection contract or stable Roblox-owned endpoint when available.
- Signals: chart rank and change, concurrent-player change, visit/favorite change when publicly available, first-seen recency, and consecutive chart appearances.
- Personalized recommendation surfaces are not treated as global rank unless the collection evidence identifies the surface and locale.

Exact heat components:

- Rising/Up-and-Coming/Charts rank: 30 points for top 10, 24 for top 25, 15 for top 50, otherwise 0.
- Compatible one-day rank improvement: 30 points for ≥20 places, 20 for 10–19, 10 for 1–9, otherwise 0.
- One-day concurrent-player growth: 25 points for ≥100%, 20 for ≥50%, 12 for ≥20%, 5 for >0%, otherwise 0.
- Concurrent-player scale: 10 points for ≥10,000, 7 for ≥1,000, 3 for ≥100, otherwise 0.
- Consecutive compatible chart appearances: 5 points for at least three observations, 3 for two, otherwise 0.

All heat formulas use available verified fields without reweighting. Missing
components contribute zero. One platform record observed on multiple compatible
surfaces uses its highest heat for normalization while retaining every surface
observation as evidence.

### Snapshot requirement

The default schedule collects twice daily at 10:00 and 16:00 Asia/Shanghai. A
first observation may establish recency and presence but cannot claim growth.
Compatible deltas require the same provider, surface, geo, locale, normalized
query parameters, and metric-definition version.

- A one-day delta uses the closest observation to 24 hours ago within ±6 hours.
- A seven-day delta uses the closest observation to 168 hours ago within ±24 hours.
- A same-day 10:00-to-16:00 change is labeled `intraday` and never fills a 1d or 7d growth field.

## Identity and Deduplication

1. Exact platform IDs are authoritative within a platform and map to one persisted `opportunity_id`.
2. Cross-platform records merge only when normalized title plus developer, official domain, or an explicit alias mapping agrees.
3. Same-name records with different developers remain separate.
4. Every merged candidate retains all source-specific records and URLs.
5. Manual alias decisions are versioned in configuration and visible in report provenance.
6. Deduplication finishes before the Agent enrichment manifest is generated; evidence always targets `opportunity_id` while preserving its source platform IDs.

## Search-Demand Evidence

The Agent supplies a versioned evidence record for each enrichment candidate:

```json
{
  "schema_version": 1,
  "run_id": "20260831T020000Z-a1b2c3d4",
  "opportunity_id": "0f840f6f-5c62-4ca6-9d53-e0be9ab2740b",
  "observed_at": "2026-08-31T06:00:00Z",
  "trends": {
    "query": "Example Game game",
    "query_type": "search_term",
    "timeframe": "now 7-d",
    "geo": "US",
    "category": 0,
    "property": "web",
    "timezone": "America/Los_Angeles",
    "points": [
      {"date": "2026-08-28", "value": 100, "complete": true},
      {"date": "2026-08-29", "value": 32, "complete": true},
      {"date": "2026-08-30", "value": 18, "complete": true},
      {"date": "2026-08-31", "value": 22, "complete": false}
    ],
    "comparison_term": "gpts",
    "comparison_average": 41,
    "evidence_url": "https://trends.google.com/trends/explore?...",
    "raw_artifact": "data/unified-game-radar/raw/.../trends.json"
  },
  "autocomplete_queries": [
    {"query": "example game codes", "observed_at": "2026-08-31T06:00:00Z", "source_url": "https://www.google.com/..."}
  ],
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

The evidence schema distinguishes an actual count of zero from a collection
failure (`null`). Evidence URLs and observation times are mandatory for every
positive claim. An ambiguous bare name is tested with an exact game-intent
modifier such as `[name] game`; if the SERP or related queries still resolve to
another product, person, or software project, demand is `unknown` until a
disambiguated query can be verified.

Hourly Trends exports are aggregated into a mean for each calendar day in the
declared Trends timezone. The current calendar day is marked incomplete and is
excluded from demand classification.

## Demand Gate

The gate runs before action labels in this exact order:

```text
unknown → fail → single_spike/early_watch → pass
```

Only completed points participate in peak, persistence, and latest-day tests.
Evidence older than 24 hours at publication is `unknown`.

### `pass`

All of the following are required after the earlier classifications do not
match:

1. Search is nonzero on at least two completed calendar days within the selected window.
2. The latest completed day is at least 30% of the completed-window peak.
3. At least one relevant autocomplete or related query exists, or a verified second wave occurs after the peak.
4. The query is unambiguously associated with the intended game.

### `early_watch`

Used when demand is plausible but not durable, including:

- one sharp spike followed by a decline;
- only one completed nonzero day;
- the latest point is partial and durability cannot yet be evaluated;
- platform/community acceleration exists but search evidence is not mature.

An exact `single_spike` detector classifies `early_watch` when all are true:

1. The completed peak is positive and at least twice the second-highest completed value.
2. At least two completed points occur after the peak.
3. Every completed post-peak point is below 40% of the peak.
4. No later local maximum reaches 50% of the peak.

An `early_watch` candidate can never receive a “worth doing” or “immediate action” label.

### `fail`

Used when all completed Trends points are zero and there are no relevant
autocomplete or related queries, or when the latest completed point is zero
after a decayed spike and no supporting intent remains. A failed candidate is
eliminated regardless of platform score or SEO competition.

### `unknown`

Used when evidence collection failed or is stale. It never receives a positive action.

## Unified Score

Scores are calculated only from verified, non-missing evidence. Missing
dimensions contribute zero and are never reweighted. A positive action also
requires known, fresh search-demand and SERP evidence.

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

### Exact demand score (0–30)

- Persistence: `min(8, 2 × completed_nonzero_days)`.
- Latest retention: `8 × min(1, latest_completed / completed_peak)`.
- Second wave: 6 points when a later completed local maximum is at least 50% of peak; 3 points when it is 30–49%; otherwise 0.
- Relevant autocomplete: 4 points for at least two distinct suggestions, 2 for one, otherwise 0.
- Relevant related queries: 4 points for at least two distinct queries, 2 for one, otherwise 0.
- Cap at 30 and round to one decimal.

### Exact external-spread score (0–20)

Each evidence row contains `source`, `url`, `published_at`, `observed_at`,
`author_relation` (`independent`, `developer`, or `unknown`), `engagement_count`,
and `evidence_kind`. Only independent rows published within seven days score.

- Source diversity: 4 points per distinct source domain, capped at 8.
- Evidence count: 2 points per qualifying row, capped at 4.
- Highest verified engagement: 8 points for ≥10,000; 6 for ≥1,000; 4 for ≥100; 2 for ≥20; 1 below 20; 0 when absent.
- Recency: 4 points when the newest row is ≤2 days old, 2 when ≤7 days, otherwise 0.
- Cap at 20.

### Exact SEO-gap score (0–20)

SERP counts include only results relevant to the exact game-intent query.

- Guide results: 10 points for 0, 7 for 1–2, 3 for 3–5, otherwise 0.
- Relevant nonofficial results: 6 points for 0, 4 for 1–3, 2 for 4–10, otherwise 0.
- Missing intent types among `guide`, `codes`, `answers`, and `wiki`: 1 point each, capped at 4.
- A `null` count makes SEO evidence unknown, contributes zero, and blocks a positive action.

### Stable ordering

Sort by action priority, total score descending, demand score descending,
platform score descending, normalized name ascending, then `opportunity_id`.

## Persistence

Use SQLite in the configured data directory, defaulting to:

```text
data/unified-game-radar/radar.sqlite3
```

Tables store runs, source health, candidates, platform observations, identity links, evidence bundles, scores, and report publications. Writes use transactions and foreign keys. Reports are rebuilt from persisted canonical records, not temporary files.

Version 1 does not import legacy Steam JSON snapshots. It starts a new unified
baseline while leaving legacy artifacts read-only. Reports label candidates
`baselining` until compatible unified observations exist.

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

Raw browser/provider artifacts are retained for a configurable period and
redacted before persistence. Run-scoped artifacts are immutable; daily
`latest.*` advances only under the publication rules below and never moves to
an older scheduled date.

### Ingest idempotency

Browser observations use a versioned envelope with `run_id`, collector,
surface, geo, locale, metric-definition version, and immutable
`observation_id`. The CLI rejects a run mismatch. Repeating an identical
observation is a no-op; reusing an observation ID with a different payload is
a conflict. One evidence record is accepted per opportunity per run; identical
retries are no-ops and changed payloads are rejected so corrections require a
new run.

### Publication semantics

- The scheduled 10:00 run is collection-only and never advances daily `latest`.
- The scheduled 16:00 run collects, enriches, and publishes
  `daily/YYYY-MM-DD.{json,md}` plus daily `latest.{json,md}`.
- Manual runs produce run-scoped reports but do not advance daily `latest`
  unless `--publish-daily` is explicitly passed.
- Preliminary and final run artifacts remain available for audit, but “one
  daily report” refers only to the 16:00 canonical publication.

## Failure Handling

- One collector failure does not abort healthy collectors.
- Every report includes source health: `fresh`, `partial`, `stale`, `unavailable`, or `not_run`.
- Stale observations are labeled and cannot claim current growth.
- HTTP 429, CAPTCHA, authentication, and selector drift use bounded retries and explicit warning codes.
- Browser collection failures create outstanding Agent tasks rather than fabricated results.
- Demand evidence failure caps the action at `needs_verification`.
- Persistence or schema corruption is fatal and returns a nonzero exit code.
- Concurrent runs use a project-scoped lock.

Source observations from the active run are `fresh` when no more than six
hours old. Same-run capability gaps are `partial`. A provider fallback no more
than 72 hours old is `stale`; older or missing data is `unavailable`. Stale
data may appear for context but cannot contribute a current-growth score or a
positive action.

## Agent Skill Workflow

The public triggers include `跑统一游戏雷达`, `监控今天的热词游戏`, and scheduled execution.

1. Run `scan` and parse its one-line manifest.
2. Complete outstanding itch.io/Roblox browser observations using the exact collection contract.
3. Run `ingest` for those observations.
4. Read the preliminary enrichment list.
5. Collect Google Trends, autocomplete, related-query, SERP, and independent community evidence for the bounded Top N.
6. Run `enrich` with the exact run ID.
7. Return one unified report. Do not push to Feishu or the web platform unless the user separately authorizes that external write.

The old `html5-game-radar` and `steam-game-radar` schedules are deprecated when
the unified schedule is enabled. Their manual triggers remain compatibility
wrappers that invoke the unified CLI with a platform filter; they must not
publish a competing daily report. The old HTML5 score is no longer used.

## Web Platform Integration Boundary

Version 1 performs no network write to `web-game-data.vercel.app`. The canonical final JSON is the integration contract. A future adapter may:

- upload/import the final report;
- map candidates and evidence into platform tables;
- preserve run ID, schema version, source health, and evidence URLs;
- reject unsupported schema versions;
- remain retryable and idempotent.

Local SQLite is the version-1 system of record. The web platform is a
downstream, idempotent read model and never writes scores or evidence back into
the local radar. A later migration may promote the platform to system of
record only through a separately approved conflict and migration design.

## Testing

### Unit tests

- Schema validation and `null` handling.
- Platform-local heat transforms and percentile normalization.
- One-item, tied, all-low, and smaller-than-five normalization cohorts.
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
- Observation-ID conflict and run-ID mismatch are rejected.
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
