# Steam Game Radar Design

**Status:** Approved by user on 2026-08-24  
**Goal:** Add a standalone Steam trend radar that supports scheduled and manual scans, ranks released and unreleased games separately, and uses manual SteamDB imports as a compliant fallback and enrichment source.

## Context and Constraints

The repository already contains `html5-game-radar`, which discovers browser-playable games. Steam games have different metrics and content opportunities, so they must not share an input pool or leaderboard with HTML5 games.

SteamDB exposes useful trend, follower-growth, wishlist-activity, chart, and release views. SteamDB also states that it has no public API and prohibits automated scraping and crawling. Automated collection must therefore use Steam-owned endpoints. SteamDB data may enter the system only through a local file explicitly provided by a person.

Sources:

- SteamDB FAQ: <https://steamdb.info/faq/>
- Steamworks Web API overview: <https://partner.steamgames.com/doc/webapi_overview>
- Steamworks Web API reference: <https://partner.steamgames.com/doc/webapi>

## Decisions

1. Create `steam-game-radar`; leave `html5-game-radar` behavior unchanged.
2. Support daily scheduled scans and the manual phrase `跑 Steam 雷达`.
3. Produce separate released and unreleased rankings.
4. Use Steam-owned endpoints for automated collection.
5. Support local CSV and JSON SteamDB imports as fallback and enrichment.
6. Never make an automated request to SteamDB, including through redirects.
7. Save timestamped snapshots so one-day and seven-day changes can be calculated locally.
8. Use a two-phase report: Steam-only preliminary ranking, followed by optional SEO/community enrichment and final actions.

## Scope

### In Scope

- Released games with increasing player activity.
- Unreleased games gaining visibility or approaching release.
- Official Steam collection, manual SteamDB import, normalization, snapshots, trends, scores, and reports.
- Google, YouTube, and Reddit evidence supplied through a versioned enrichment file.
- Markdown and JSON reports.
- Manual and scheduled invocation instructions.
- Offline tests using fixtures.

### Out of Scope

- Automated SteamDB scraping, crawling, browser extraction, or auto-refreshing.
- Domain registration, site generation, deployment, or backlink submission.
- Automatic watchlist mutation.
- Purchasing or integrating a commercial data provider.
- Claiming precise wishlist counts when no selected provider supplies them.
- Combining Steam and HTML5 candidates into one score.

## File Structure

```text
steam-game-radar/
├── SKILL.md
├── scripts/
│   └── steam_radar.py
├── steam_game_radar/
│   ├── __init__.py
│   ├── errors.py
│   ├── config.py
│   ├── artifacts.py
│   ├── http_client.py
│   ├── official_provider.py
│   ├── steamdb_import.py
│   ├── run_lock.py
│   ├── snapshot.py
│   ├── merge.py
│   ├── trend.py
│   ├── score.py
│   ├── enrichment.py
│   ├── report.py
│   └── schemas.py
├── references/
│   ├── config.example.json
│   ├── data-sources.md
│   ├── scoring-rules.md
│   ├── steamdb-import-format.md
│   └── report-template.md
└── tests/
    ├── fixtures/
    ├── test_config.py
    ├── test_schemas.py
    ├── test_artifacts.py
    ├── test_http_client.py
    ├── test_official_parsers.py
    ├── test_official_collection.py
    ├── test_steamdb_import.py
    ├── test_run_lock.py
    ├── test_snapshot.py
    ├── test_merge.py
    ├── test_trend.py
    ├── test_enrichment.py
    ├── test_score.py
    ├── test_report.py
    ├── test_cli.py
    ├── test_skill_docs.py
    └── test_live_official_provider.py
```

Only `scripts/steam_radar.py` is executable. Package modules expose small, pure interfaces and do not parse command-line arguments.

The repository root `README.md` will list the new skill and explain its separation from the HTML5 radar.

## Runtime Contract

### Commands

```bash
python3 steam-game-radar/scripts/steam_radar.py scan \
  --config steam-game-radar/references/config.example.json

python3 steam-game-radar/scripts/steam_radar.py import-steamdb \
  --config steam-game-radar/references/config.example.json \
  --view wishlist_activity \
  --input /path/to/steamdb-export.csv

python3 steam-game-radar/scripts/steam_radar.py enrich \
  --config steam-game-radar/references/config.example.json \
  --run-id 20260824T030000Z-a1b2c3d4 \
  --input /path/to/enrichment.json
```

`scan` and `import-steamdb` create a preliminary JSON/Markdown report. `enrich` validates evidence for an existing run and creates a final JSON/Markdown report. All three commands use the same orchestrator and configuration.

The supported runtime is Python 3.11 or newer with only the standard library. The process working directory is always the target project root. The offline test command is:

```bash
python3 -m unittest discover -s steam-game-radar/tests -p 'test_*.py'
```

### Exit Codes

| Code | Meaning |
|---:|---|
| 0 | Success, including a documented stale-snapshot fallback no older than 72 hours |
| 2 | Input or schema validation failure |
| 3 | Provider failure with no usable fallback |
| 4 | Configuration failure |
| 5 | Snapshot or report persistence failure |
| 6 | Another radar run holds the project run lock |

### Configuration Schema

`config.example.json` uses these fields and defaults:

```json
{
  "schema_version": 1,
  "country": "US",
  "language": "english",
  "timezone": "Asia/Shanghai",
  "schedule": "0 11 * * *",
  "released_candidate_limit": 50,
  "unreleased_candidate_limit": 50,
  "preliminary_top_n": 20,
  "enrichment_top_n": 10,
  "final_top_n": 10,
  "request_timeout_seconds": 15,
  "max_retries": 3,
  "minimum_request_interval_seconds": 1.0,
  "raw_retention_days": 14,
  "raw_max_bytes_per_provider": 5242880,
  "stale_warning_hours": 36,
  "stale_fallback_limit_hours": 72,
  "data_dir": "data/steam-game-radar",
  "report_dir": "reports/steam-game-radar"
}
```

Relative paths resolve from the target project root, not from the skill directory.

### Manual Trigger Mapping

- `跑 Steam 雷达` maps to `scan`.
- `导入 SteamDB 榜单并跑 Steam 雷达` requires a local file and view, then maps to `import-steamdb`.
- After a preliminary report, the Agent enriches the top configured candidates and runs `enrich`.

### Scheduled Registration

`SKILL.md` contains two equivalent examples:

1. A host-agent cron registration at `0 11 * * *` in `Asia/Shanghai` whose payload runs `scan`, collects enrichment for the preliminary Top 10, then runs `enrich`.
2. A conventional cron entry invoking the same `scan` command for environments without agent scheduling. Conventional cron produces the preliminary report only unless a separate agent performs enrichment.

Scheduling is configuration; no scoring module registers or edits cron itself.

## Official Provider Matrix

The initial implementation uses only these Steam-owned HTTPS hosts and endpoints:

| Capability | Endpoint | Auth | Pagination / Bound | Normalized Fields |
|---|---|---|---|---|
| Most-played candidate discovery and rank | `https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/` | None | Provider response, capped locally at released candidate limit | `appid`, `rank`, `previous_rank`, `peak_players` when present |
| Current players for a known AppID | `https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={appid}` | None | One AppID per request; at most released candidate limit | `current_players` |
| Store category candidates | `https://store.steampowered.com/api/featuredcategories?cc={country}&l={language}` | None | Non-paginated; consume `top_sellers`, `new_releases`, and `coming_soon`, capped per pool | `appid`, category rank, name, price metadata |
| Release metadata | `https://store.steampowered.com/api/appdetails?appids={appid}&cc={country}&l={language}` | None | One AppID per request; at most released + unreleased candidate limits | name, release status/date, genres, recommendations when present |

The Steam Store JSON endpoints are Steam-owned public endpoints but are not guaranteed by the documented Steamworks API contract. The provider records its capability result for every run. A missing or changed capability causes a warning and fallback; it does not silently change scoring semantics.

The initial implementation does not require a Steam Web API key. Optional authenticated or SteamKit providers are future extensions and are not part of this plan.

### Candidate Aggregation

Released discovery sources are processed in this deterministic priority order: most-played chart, top sellers, then new releases. Within a source, candidates sort by source rank ascending and AppID ascending. The first occurrence of an AppID establishes discovery priority; later occurrences add source observations but do not move it earlier. The ordered union is capped at `released_candidate_limit` before per-AppID requests.

Unreleased discovery uses `coming_soon`, sorted by category rank and AppID, then capped at `unreleased_candidate_limit`.

App details are fetched for every capped candidate. Only records whose app-details type is exactly `game` are retained. DLC, demos, software, videos, tools, and unknown types are excluded with stable warning codes. App details also decide the final pool: a confirmed released game appears only in the released pool, while a confirmed unreleased game appears only in the unreleased pool. If an AppID was discovered in both pools, this release-state decision still produces one record. App-details name and release metadata win for identity fields; source-specific ranks remain separate metric observations.

Current-player requests run only for retained released base games, in the deterministic discovery order, and stop at the released limit.

### Request Policy

- Allowed HTTPS hosts: `api.steampowered.com` and `store.steampowered.com` only.
- Redirect following is disabled. A redirect is an error, even if its target appears allowed.
- Requests are limited to one per configured interval, default one second.
- Each request uses the configured timeout and at most three exponential-backoff attempts.
- Per-AppID metadata and player requests stop at configured candidate limits.
- The user agent identifies `7deer-steam-game-radar/1` and a documentation URL, with no credentials.

## Data Schemas

All persisted JSON objects include `schema_version: 1`.

### Metric Observation

Provenance is stored per value, not per record:

```json
{
  "value": 12000,
  "source_id": "steam_current_players",
  "source_kind": "steam_official",
  "observed_at": "2026-08-24T03:00:00Z"
}
```

### Normalized Game Record

```json
{
  "schema_version": 1,
  "appid": 123456,
  "name": "Example Game",
  "release_status": "released",
  "store_url": "https://store.steampowered.com/app/123456/",
  "metrics": {
    "release_date": {
      "value": "2026-08-20",
      "source_id": "steam_appdetails",
      "source_kind": "steam_official",
      "observed_at": "2026-08-24T03:00:00Z"
    },
    "current_players": {
      "value": 12000,
      "source_id": "steam_current_players",
      "source_kind": "steam_official",
      "observed_at": "2026-08-24T03:00:00Z"
    }
  },
  "source_extra": {}
}
```

Required fields are `schema_version`, `appid`, `name`, `release_status`, `store_url`, and `metrics`. Allowed release states are `released`, `unreleased`, and `unknown`. Unknown values are omitted; they are never stored as zero or an empty date.

### Analyzed Candidate

```json
{
  "schema_version": 1,
  "appid": 123456,
  "deltas": {
    "current_players_1d_percent": 24.5,
    "rank_7d_change": 18
  },
  "metric_scores": {
    "player_growth": 74.0,
    "current_player_scale": 82.0
  },
  "steam_heat_score": 78.2,
  "seo_opportunity_score": null,
  "final_score": null,
  "action": "needs_seo_enrichment",
  "confidence": "C",
  "warnings": []
}
```

## SteamDB Manual Import Contract

### Invocation and Shapes

- CSV requires explicit `--view`.
- JSON may be a row array or `{ "schema_version": 1, "view": "...", "rows": [...] }`.
- If JSON includes a view, it must match an explicitly supplied `--view`.

Allowed views:

- `trending_games`
- `wishlist_activity`
- `trending_followers`
- `recent_releases`

Every row must contain either numeric `appid` or a `url` containing `/app/{appid}`. Name-only matching is not allowed.

### Canonical Fields and Aliases

| Canonical Field | Accepted Headers |
|---|---|
| `appid` | `appid`, `app_id`, `steam_appid` |
| `url` | `url`, `steamdb_url`, `app_url` |
| `name` | `name`, `game`, `title` |
| `rank` | `rank`, `#` |
| `current_players` | `players_now`, `current`, `online` |
| `peak_players` | `peak`, `24h_peak` |
| `followers` | `followers`, `follows` |
| `follower_gain_7d` | `7d_gain`, `followers_7d_gain` |
| `wishlist_gain_7d` | `wishlist_7d_gain`, `wishlists_7d_gain` |
| `rating_percent` | `rating`, `rating_percent` |
| `release_date` | `release`, `release_date` |

View requirements:

- `trending_games`: AppID/URL, name, and either rank or current players.
- `wishlist_activity`: AppID/URL, name, and either wishlist gain or follower gain.
- `trending_followers`: AppID/URL, name, follower gain, and followers when available.
- `recent_releases`: AppID/URL, name, release date, and either current or peak players.

Numbers accept commas, a leading plus sign, and `K`/`M` suffixes. An em dash or empty value means missing. Percentages accept `91.2` or `91.2%`. Dates accept ISO `YYYY-MM-DD` or `DD Mon YYYY`; SteamDB dates are interpreted as UTC. Duplicate AppIDs within one import are rejected as ambiguous rather than merged.

Unknown columns are preserved under `source_extra`. Invalid rows are reported individually. Valid rows continue through the pipeline.

Imported values use the run start as their observation time because the file represents a human observation at import time. The import file itself is copied into the run's raw directory after redaction and size validation.

## Raw Artifacts and Redaction

Raw artifacts use:

```text
data/steam-game-radar/raw/{run_id}/{provider_id}.json
```

Rules:

- `run_id` is UTC `YYYYMMDDTHHMMSSZ-{8 lowercase hex characters}`.
- Maximum persisted size is 5 MiB per provider by default.
- Objects are recursively redacted when a key contains `key`, `token`, `authorization`, `cookie`, or `secret`, case-insensitive.
- Raw writes occur only after JSON parsing, size validation, and redaction.
- Raw artifacts older than 14 days are removed only from the configured raw directory.
- Persistence failures stop the run with exit code 5.

## Snapshot and Merge Semantics

Snapshots use:

```text
data/steam-game-radar/snapshots/{run_id}.json
```

Each run creates a new immutable snapshot. Same-day reruns never overwrite snapshots.

Before any network, import, snapshot, or report work, the orchestrator atomically acquires `{data_dir}/.run.lock` using exclusive creation. The lock records PID, run ID, host, and acquisition time. A concurrent run exits with code 6. A lock older than two hours may be removed only when it belongs to the same host and its PID is no longer running; otherwise it remains blocking. The orchestrator releases its own lock in a `finally` block.

Because `scan` and `enrich` are separate commands, the lock alone does not make `latest.*` monotonic. Before replacing `latest.*`, the reporter reads its current canonical JSON and compares run timestamps. A candidate report replaces `latest.*` only when its run timestamp is newer, or when it belongs to the same run and advances that run from preliminary to final. A delayed enrichment for an older run still writes its immutable timestamped final report but skips `latest.*` publication.

Comparison selection:

- One-day comparison: observations 18 to 36 hours earlier; choose closest to 24 hours, then the newer observation on a tie.
- Seven-day comparison: observations 144 to 192 hours earlier; choose closest to 168 hours, then the newer observation on a tie.
- Deltas compare observations with the same metric name and `source_id`.
- If no observation fits the window, omit the delta.

Merge precedence for an import run:

1. When a valid official snapshot no older than 72 hours exists, start with it and merge manual values by metric name.
2. When no valid official snapshot exists, a valid SteamDB import creates a standalone manual baseline, exits 0, and reports confidence C with no growth claims or final action.
3. The newer `observed_at` wins for the same metric.
4. On an exact timestamp tie, an official value wins over a manual value.
5. Official app details determine release status when available. Otherwise, `released` wins only when its observed release date is not in the future; unresolved contradictions reject the affected record.

Out-of-order historical files are not supported by the import CLI; imports are observations made at run time.

Data older than 36 hours produces a visible stale warning. Official fallback is allowed up to 72 hours. Older data cannot produce an action score and provider failure exits with code 3.

## Enrichment Contract

The preliminary report identifies the top configured candidates by Steam heat. The Agent then uses `keyword-competition-analysis` plus available Google, YouTube, and Reddit research tools to create:

```json
{
  "schema_version": 1,
  "run_id": "20260824T030000Z-a1b2c3d4",
  "observed_at": "2026-08-24T03:20:00Z",
  "games": [
    {
      "appid": 123456,
      "google_competition_gap_score": 80,
      "expandable_queries": ["example game wiki", "example game guide"],
      "youtube_relevant_7d": 6,
      "reddit_relevant_7d": 4,
      "reddit_upvotes_7d": 240,
      "evidence": [
        {"source": "google", "url": "https://www.google.com/search?q=example+game"},
        {"source": "youtube", "url": "https://www.youtube.com/results?search_query=example+game"},
        {"source": "reddit", "url": "https://www.reddit.com/search/?q=example+game"}
      ]
    }
  ]
}
```

Scores must be integers from 0 to 100. Counts must be non-negative integers. Evidence entries allow only `google`, `youtube`, and `reddit` source values and valid HTTPS URLs. Each game requires a Google evidence entry and a matching typed evidence entry for every supplied YouTube or Reddit signal. The file is saved at `data/steam-game-radar/enrichment/{run_id}.json` after validation.

Scheduled agent runs perform this enrichment for preliminary Top 10 candidates. Conventional cron without an Agent stops at the preliminary report.

## Trend and Scoring Contract

Piecewise metrics use linear interpolation between listed points and clamp outside the range.

### Released Steam Heat

| Metric | Weight | 0-100 Transform |
|---|---:|---|
| Player growth | 25 | Use the larger available positive 1d/7d percentage: `(0,0)`, `(5,25)`, `(15,50)`, `(30,75)`, `(60,100)`; negative growth is 0 |
| Current player scale | 10 | `(0,0)`, `(100,20)`, `(1000,50)`, `(10000,80)`, `(100000,100)` on raw player count |
| Rank improvement | 15 | Select 7d same-source rank delta when available, otherwise 1d same-source delta, otherwise provider `previous_rank - current_rank`; then score `(0,0)`, `(5,40)`, `(20,70)`, `(50,100)` |
| Release recency | 10 | At most 7 days: 100; 8-30: 70; 31-90: 40; older: 0 |

Released Steam heat requires at least two metrics with at least 25 total configured weight. Otherwise the candidate is `insufficient_data`.

### Unreleased Steam Heat

| Metric | Weight | 0-100 Transform |
|---|---:|---|
| Upcoming rank improvement | 20 | Select 7d same-source rank delta when available, otherwise 1d same-source delta; then score `(0,0)`, `(5,40)`, `(20,70)`, `(50,100)` |
| Wishlist/follower 7d gain | 20 | Use the larger available gain: `(0,0)`, `(100,20)`, `(1000,60)`, `(5000,85)`, `(20000,100)` |
| Release proximity | 10 | 0-14 days: 100; 15-30: 80; 31-90: 60; 91-180: 30; later/TBA: 0 |
| Current upcoming visibility | 10 | Current `coming_soon` category rank: rank 1: 100; 2-5: 80; 6-20: 50; 21-50: 20; lower/unranked: 0 |

Unreleased Steam heat requires at least two metrics with at least 30 total configured weight. Otherwise the candidate is `insufficient_data`.

### SEO Opportunity

| Metric | Weight | Transform |
|---|---:|---|
| Google competition gap | 20 | Validated enrichment score 0-100 |
| Expandable query count | 10 | `min(query_count / 20 * 100, 100)` |
| YouTube/Reddit cross-signal | 10 | Score YouTube and Reddit relevant 7d counts with `(0,0)`, `(1,20)`, `(3,50)`, `(10,80)`, `(25,100)` and average available sources |

SEO opportunity requires Google competition gap plus at least one other SEO metric, for at least 30 total configured weight.

### Score Combination and Actions

- Weighted averages divide by available configured weight only after minimum metric gates pass.
- `final_score = 0.60 × steam_heat_score + 0.40 × seo_opportunity_score`.
- Preliminary records have Steam heat but no final score and action `needs_seo_enrichment`.
- Final actions are assigned only when both Steam heat and SEO opportunity pass their gates.

| Final Score | Action |
|---:|---|
| 80-100 | `immediate_action` |
| 65-79.99 | `worth_positioning` |
| 50-64.99 | `watch` |
| Below 50 | `skip` |

### Confidence

- **A:** official values, valid historical comparison, manual SteamDB confirmation, and valid SEO/community enrichment.
- **B:** either official or manual SteamDB values, valid historical comparison, and valid SEO/community enrichment.
- **C:** baseline, missing history, or incomplete enrichment.

Only A and B candidates receive final actions. C candidates remain preliminary or `insufficient_data`.

### Stable Ordering

Rank by:

1. final score descending when present, otherwise Steam heat descending
2. confidence A, B, C
3. current players descending for released games or wishlist/follower gain descending for unreleased games
4. case-folded name ascending
5. AppID ascending

## Report Contract

Reports use:

```text
reports/steam-game-radar/{run_id}.preliminary.json
reports/steam-game-radar/{run_id}.preliminary.md
reports/steam-game-radar/{run_id}.final.json
reports/steam-game-radar/{run_id}.final.md
reports/steam-game-radar/latest.json
reports/steam-game-radar/latest.md
```

Timestamped reports never overwrite. `latest.*` is atomically replaced after a successful report.

The JSON report is canonical and has this top-level shape:

```json
{
  "report_schema_version": 1,
  "run_id": "20260824T030000Z-a1b2c3d4",
  "phase": "preliminary",
  "mode": "official_scan",
  "generated_at": "2026-08-24T03:00:30Z",
  "data_status": "fresh",
  "released": [],
  "unreleased": [],
  "newly_observed": [],
  "warnings": [],
  "rejected_rows": []
}
```

Warnings and rejections use `{ "code": "stable_code", "message": "human text", "appid": 123456 }`, with AppID omitted for run-level warnings.

Markdown is rendered only from the canonical JSON object. It cannot independently sort or recompute values, which guarantees ordering parity.

Each candidate reports observed metrics, deltas, component scores, heat/SEO/final scores, confidence, action, warnings, per-metric provenance, typed evidence, and recommended content types.

## Error Handling and Security

- Apply bounded retries and exponential backoff to official requests.
- Use the latest snapshot only within the configured 72-hour fallback limit and label its age.
- Preserve a redacted raw response when normalization fails.
- Reject invalid import rows individually while processing valid rows.
- Never fabricate missing metrics or deltas.
- Never store API keys, authorization headers, cookies, or secrets.
- Restrict HTTP requests to the explicit Steam-owned host allowlist and reject all redirects.
- Fail with exit code 5 if canonical JSON and Markdown cannot both be persisted.

## Testing Strategy

Default tests use local fixtures and make no network requests.

Required tests:

- provider capability mapping for all four official endpoints
- host allowlist and redirect rejection, including a redirect toward SteamDB
- rate limit, timeout, retry exhaustion, and stale fallback
- recursive raw redaction, size cap, and retention boundary
- CSV and JSON import for all four views, aliases, numeric formats, dates, duplicates, and invalid rows
- per-metric provenance and merge precedence
- same-day reruns and immutable snapshot names
- exact one-day and seven-day comparison windows and tie-breaking
- baseline behavior and out-of-window history
- every piecewise score boundary and interpolation midpoint
- minimum metric gates, incomplete enrichment, and final score calculation
- separate released and unreleased ranking and stable tie-breaking
- preliminary/final JSON schema and Markdown parity
- all CLI commands and exit codes
- deterministic candidate union, deduplication, base-game filtering, and pool assignment
- SteamDB-only baseline behavior without an official snapshot
- exclusive run lock, stale-lock rules, and prevention of an older `latest.*` replacement
- delayed enrichment for an older run writes immutable output but cannot replace a newer run's `latest.*`
- a static check that automated HTTP code cannot address SteamDB

An opt-in live smoke test may validate Steam-owned endpoints but is excluded from normal CI.

## Acceptance Criteria

1. `steam-game-radar/SKILL.md` is independently discoverable and lists manual and scheduled triggers.
2. `scan`, `import-steamdb`, and `enrich` follow the documented CLI and exit-code contract.
3. The documented 11:00 `Asia/Shanghai` agent schedule runs scan and enrichment; the conventional cron example runs scan.
4. Fixture-based official scans produce separate released and unreleased preliminary reports.
5. A valid SteamDB file enriches or replaces permitted metrics without any SteamDB request.
6. Per-metric provenance and timestamps survive normalization, merge, analysis, and reporting.
7. First runs produce baselines without invented growth; later snapshots use the exact comparison windows.
8. Same-day reruns create new immutable snapshots and timestamped reports while atomically updating `latest.*`.
9. Score transforms, minimum metric gates, final combination, actions, confidence, and tie-breaking match this document.
10. Enrichment affects final scores only through the versioned evidence contract.
11. Raw artifacts are redacted, size-bounded, and retained for no more than the configured period.
12. The HTTP client permits only the two named Steam hosts and rejects redirects, including redirects to SteamDB.
13. The JSON report is versioned and canonical; Markdown is rendered from it with identical ordering.
14. Stale fallbacks, rejected rows, missing capabilities, and insufficient-data candidates are visible in reports.
15. The repository README documents the new skill and its separation from the HTML5 radar.
16. Candidate union, deduplication, base-game filtering, rank-delta selection, and pool assignment follow the deterministic contracts.
17. A SteamDB-only import can create a confidence-C baseline when no official snapshot exists.
18. Concurrent commands are blocked by the exclusive run lock; delayed enrichment for an older run cannot overwrite a newer run's `latest.*` report but still writes its immutable final report.
19. The full default test suite passes on Python 3.11+ without network access using the documented unittest command.
