# Steam Game Radar Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone Steam trend radar with official Steam collection, manual SteamDB import, immutable history, deterministic scoring, SEO enrichment, reports, and scheduled/manual Skill instructions.

**Architecture:** A single Python 3.11 standard-library CLI orchestrates small modules. Network access is isolated behind an allowlisted no-redirect client; every metric carries provenance; snapshots and pure functions produce trends and scores; canonical JSON is the sole source for Markdown.

**Tech Stack:** Python 3.11+, standard-library `unittest`, `urllib`, `dataclasses`, CSV, JSON, Markdown, Agent Skills `SKILL.md`.

**Spec:** `docs/superpowers/specs/2026-08-24-steam-game-radar-design.md`

**Required practices:** @superpowers:test-driven-development, @code-development-rules, @superpowers:verification-before-completion.

---

## File Map

| File | Single responsibility |
|---|---|
| `steam-game-radar/steam_game_radar/errors.py` | Named domain exceptions used by CLI exit-code mapping |
| `steam-game-radar/steam_game_radar/config.py` | Configuration parsing and validation |
| `steam-game-radar/steam_game_radar/schemas.py` | Versioned domain records and JSON conversion |
| `steam-game-radar/steam_game_radar/artifacts.py` | Recursive redaction, raw size limits, atomic raw persistence, retention |
| `steam-game-radar/steam_game_radar/http_client.py` | Allowlisted, rate-limited, no-redirect JSON HTTP |
| `steam-game-radar/steam_game_radar/official_provider.py` | Steam endpoint parsing, candidate aggregation, official normalization |
| `steam-game-radar/steam_game_radar/steamdb_import.py` | Local CSV/JSON import and field normalization |
| `steam-game-radar/steam_game_radar/run_lock.py` | Exclusive project run lock |
| `steam-game-radar/steam_game_radar/snapshot.py` | Immutable snapshots and comparison lookup |
| `steam-game-radar/steam_game_radar/merge.py` | Official/manual metric merge and fallback eligibility |
| `steam-game-radar/steam_game_radar/trend.py` | Same-source 1d/7d deltas and rank-delta precedence |
| `steam-game-radar/steam_game_radar/enrichment.py` | Versioned SEO/community evidence validation |
| `steam-game-radar/steam_game_radar/score.py` | Pure transforms, gates, confidence, actions, ordering |
| `steam-game-radar/steam_game_radar/report.py` | Canonical JSON, Markdown rendering, monotonic publication |
| `steam-game-radar/scripts/steam_radar.py` | Import-safe CLI parsing and orchestration |
| `steam-game-radar/SKILL.md` | Discovery, triggers, two-phase workflow, schedules |
| `steam-game-radar/references/*` | Config, sources, imports, scoring, report contracts |
| `steam-game-radar/tests/*` | Offline unit, integration, policy, and opt-in live tests |
| `README.md` | Repository catalog entry and actual skill count |

## Chunk 1: Foundation and Official Collection

### Task 1: Errors, Configuration, and Domain Schemas

**Files:**
- Create: `steam-game-radar/steam_game_radar/__init__.py`
- Create: `steam-game-radar/steam_game_radar/errors.py`
- Create: `steam-game-radar/steam_game_radar/config.py`
- Create: `steam-game-radar/steam_game_radar/schemas.py`
- Create: `steam-game-radar/references/config.example.json`
- Create: `steam-game-radar/tests/test_config.py`
- Create: `steam-game-radar/tests/test_schemas.py`

- [ ] **Step 1: Write 10 failing configuration tests**

Create tests named `test_all_defaults`, `test_from_file`, `test_unknown_schema_version`, `test_invalid_integer_limit`, `test_invalid_timeout`, `test_invalid_retention`, `test_absolute_paths_preserved`, `test_relative_paths_use_project_root`, `test_missing_project_root_uses_cwd`, and `test_serialized_example_matches_defaults`. Assert every field from the approved config schema, including timezone, schedule, all candidate/report limits, network settings, retention/stale thresholds, and data/report paths.

- [ ] **Step 2: Write 8 failing schema tests**

Create tests named `test_metric_round_trip`, `test_game_round_trip`, `test_required_schema_version`, `test_positive_appid`, `test_https_steam_store_url`, `test_allowed_release_states`, `test_iso_utc_observed_at`, and `test_unknown_metrics_omitted_not_zero`. Use concrete `MetricObservation` and `GameRecord` fixtures and assert exact dictionaries.

- [ ] **Step 3: Run the red tests**

Run: `python3 -m unittest steam-game-radar/tests/test_config.py steam-game-radar/tests/test_schemas.py`

Expected: exit code 1 and final summary `FAILED (errors=2)` because both imported modules are absent.

- [ ] **Step 4: Implement named exception classes**

`errors.py` defines `RadarError`, `InputValidationError`, `ProviderUnavailableError`, `ConfigurationError`, `PersistenceError`, and `RunBusyError`. Each directly subclasses `RadarError`; `RadarError` subclasses `RuntimeError`. No exception contains exit-code logic.

- [ ] **Step 5: Implement the complete `RadarConfig` contract**

`RadarConfig` is a frozen dataclass with these fields and types: `schema_version: int`, `country: str`, `language: str`, `timezone: str`, `schedule: str`, `released_candidate_limit: int`, `unreleased_candidate_limit: int`, `preliminary_top_n: int`, `enrichment_top_n: int`, `final_top_n: int`, `request_timeout_seconds: float`, `max_retries: int`, `minimum_request_interval_seconds: float`, `raw_retention_days: int`, `raw_max_bytes_per_provider: int`, `stale_warning_hours: int`, `stale_fallback_limit_hours: int`, `data_dir: Path`, and `report_dir: Path`.

It exposes `from_mapping(value: Mapping[str, object], project_root: Path | None = None) -> RadarConfig` and `from_file(path: Path, project_root: Path | None = None) -> RadarConfig`. Unknown schema versions, non-positive limits/timeouts, negative retry counts, and stale-warning values not below fallback values raise `ConfigurationError`.

- [ ] **Step 6: Implement complete versioned records**

`MetricObservation` has `value: object`, `source_id: str`, `source_kind: Literal["steam_official", "steamdb_manual_import", "seo_enrichment"]`, and `observed_at: str`.

`GameRecord` has `schema_version: int`, `appid: int`, `name: str`, `release_status: Literal["released", "unreleased", "unknown"]`, `store_url: str`, `metrics: Mapping[str, MetricObservation]`, and `source_extra: Mapping[str, object]`.

Both expose `to_dict()` and `from_dict()` with strict validation. Also define frozen `WarningRecord(code: str, message: str, appid: int | None)` and `RejectedRow(row_number: int, code: str, message: str, appid: int | None)` with JSON conversion.

- [ ] **Step 7: Run the green tests**

Run: `python3 -m unittest steam-game-radar/tests/test_config.py steam-game-radar/tests/test_schemas.py`

Expected: exit code 0, `Ran 18 tests`, and `OK`.

- [ ] **Step 8: Commit**

```bash
git add steam-game-radar/steam_game_radar steam-game-radar/references/config.example.json steam-game-radar/tests/test_config.py steam-game-radar/tests/test_schemas.py
git commit -m "feat: add Steam radar configuration and schemas"
```

### Task 2: Shared Artifact Hygiene and Secure HTTP

**Files:**
- Create: `steam-game-radar/steam_game_radar/artifacts.py`
- Create: `steam-game-radar/steam_game_radar/http_client.py`
- Create: `steam-game-radar/tests/test_artifacts.py`
- Create: `steam-game-radar/tests/test_http_client.py`

- [ ] **Step 1: Write 8 failing artifact tests**

Test recursive case-insensitive redaction of keys containing `key`, `token`, `authorization`, `cookie`, and `secret`; redaction inside dicts and lists; pre-persistence size rejection; atomic JSON write; provider filename validation; retention boundary at exactly 14 days; refusal to prune outside configured raw root; and persistence-error conversion.

- [ ] **Step 2: Write 10 failing HTTP tests**

Test the two allowed HTTPS hosts; rejection of HTTP, userinfo, every explicit port including `:443`, third hosts, and `steamdb.info`; rejection of a 302 whose `Location` is SteamDB; timeout retry; HTTP 429/500 retry; HTTP 400 no retry; exact retry exhaustion; injected-clock one-second spacing; the full user-agent header; maximum response bytes; UTF-8/JSON errors; and no automatic redirects. Combine related assertions so the file contains exactly 10 test methods.

- [ ] **Step 3: Run the red tests**

Run: `python3 -m unittest steam-game-radar/tests/test_artifacts.py steam-game-radar/tests/test_http_client.py`

Expected: exit code 1 and `FAILED (errors=2)`.

- [ ] **Step 4: Implement artifact functions**

Implement `redact(value: object) -> object`, `atomic_write_json(path: Path, value: object) -> None`, `persist_raw(config: RadarConfig, run_id: str, provider_id: str, value: object, now: datetime) -> Path`, and `prune_raw(config: RadarConfig, now: datetime) -> Sequence[Path]`. Use temporary sibling files and `os.replace`. Validate resolved paths before deletion. Raise `InputValidationError` for invalid provider IDs/oversize input and `PersistenceError` for filesystem failure.

- [ ] **Step 5: Implement `JsonHttpClient`**

Define `ALLOWED_HOSTS = frozenset({"api.steampowered.com", "store.steampowered.com"})`, `validate_steam_url(url: str) -> urllib.parse.SplitResult`, `NoRedirectHandler`, and `JsonHttpClient.get_json(url: str) -> object`.

`get_json` validates HTTPS/host/no-userinfo and rejects every explicit port, including explicit `:443`. It adds `User-Agent: 7deer-steam-game-radar/1 (+https://github.com/destinationluo/7deer_skills)`, enforces the configured interval, disables redirects, applies configured timeout and exponential backoff, retries only timeout/429/5xx, caps bytes, decodes UTF-8, parses JSON, and raises `ProviderUnavailableError` after exhaustion.

- [ ] **Step 6: Run the green tests**

Run: `python3 -m unittest steam-game-radar/tests/test_artifacts.py steam-game-radar/tests/test_http_client.py`

Expected: exit code 0, `Ran 18 tests`, and `OK`.

- [ ] **Step 7: Commit**

```bash
git add steam-game-radar/steam_game_radar/artifacts.py steam-game-radar/steam_game_radar/http_client.py steam-game-radar/tests/test_artifacts.py steam-game-radar/tests/test_http_client.py
git commit -m "feat: secure Steam radar artifacts and HTTP"
```

### Task 3: Official Endpoint Parsers

**Files:**
- Create: `steam-game-radar/steam_game_radar/official_provider.py`
- Create: `steam-game-radar/tests/test_official_parsers.py`
- Create: `steam-game-radar/tests/fixtures/most_played.json`
- Create: `steam-game-radar/tests/fixtures/featured_categories.json`
- Create: `steam-game-radar/tests/fixtures/appdetails.json`
- Create: `steam-game-radar/tests/fixtures/current_players.json`

- [ ] **Step 1: Add four representative endpoint fixtures**

Include exact provider response shapes with previous/current ranks, top sellers, new releases, coming soon, one base game in both discovery sources, DLC/demo/software/unknown types, released/unreleased metadata, current players, and missing optional fields.

- [ ] **Step 2: Write 12 failing parser tests**

Use three tests per endpoint: valid mapping, missing optional fields remain absent, and malformed capability returns stable warning without partial fabricated values. Assert exact endpoint source IDs and per-value observation time.

- [ ] **Step 3: Run the red tests**

Run: `python3 -m unittest steam-game-radar/tests/test_official_parsers.py`

Expected: exit code 1 and `FAILED (errors=1)`.

- [ ] **Step 4: Implement parser records and functions**

Define generic `ParseResult[T](value: T, warnings: Sequence[WarningRecord])`, frozen `DiscoveryCandidate(appid: int, priority: tuple[int, int, int], source_ranks: Mapping[str, MetricObservation], source_names: Sequence[str])`, and frozen `AppIdentity(appid: int, name: str, app_type: str, release_status: str, release_date: str | None, genres: Sequence[str], observed_at: str)`.

Implement `parse_most_played(payload: object, observed_at: str) -> ParseResult[Sequence[DiscoveryCandidate]]`, `parse_featured(payload: object, observed_at: str) -> ParseResult[Mapping[str, Sequence[DiscoveryCandidate]]]`, `parse_appdetails(appid: int, payload: object, observed_at: str) -> ParseResult[AppIdentity | None]`, and `parse_current_players(appid: int, payload: object, observed_at: str) -> ParseResult[MetricObservation | None]`. Malformed payloads return an empty/`None` value plus a stable warning; they never return fabricated partial values.

Source IDs are exactly `steam_most_played_rank`, `steam_previous_rank`, `steam_top_seller_rank`, `steam_new_release_rank`, `steam_coming_soon_rank`, `steam_peak_players`, `steam_current_players`, and `steam_appdetails`.

- [ ] **Step 5: Run parser tests**

Run: `python3 -m unittest steam-game-radar/tests/test_official_parsers.py`

Expected: exit code 0, `Ran 12 tests`, and `OK`.

- [ ] **Step 6: Commit**

```bash
git add steam-game-radar/steam_game_radar/official_provider.py steam-game-radar/tests/test_official_parsers.py steam-game-radar/tests/fixtures
git commit -m "feat: parse official Steam trend data"
```

### Task 4: Deterministic Official Collection

**Files:**
- Modify: `steam-game-radar/steam_game_radar/official_provider.py`
- Create: `steam-game-radar/tests/test_official_collection.py`

- [ ] **Step 1: Write 10 failing collection tests**

Test exact endpoint URLs/query parameters; priority chart then top sellers then new releases; source-rank preservation after dedupe; cap before AppID requests; app-details type filter; stable warnings for DLC/demo/software/unknown; one-pool assignment; app-details identity precedence; current-player requests only for retained released games; exact per-candidate request bound; and four-entry capability map. Combine filter warnings in one table-driven method so the file has exactly 10 tests.

- [ ] **Step 2: Run the red tests**

Run: `python3 -m unittest steam-game-radar/tests/test_official_collection.py`

Expected: exit code 1 and `FAILED (errors=1)` because `collect_official` and collection helpers are not yet importable.

- [ ] **Step 3: Implement `CollectionResult` and collection**

`CollectionResult` has `released: Sequence[GameRecord]`, `unreleased: Sequence[GameRecord]`, `capabilities: Mapping[str, bool]`, `warnings: Sequence[WarningRecord]`, and `raw: Mapping[str, object]`.

Implement `build_released_candidates(most_played: Sequence[DiscoveryCandidate], featured: Mapping[str, Sequence[DiscoveryCandidate]], limit: int) -> Sequence[DiscoveryCandidate]`, `build_unreleased_candidates(featured: Mapping[str, Sequence[DiscoveryCandidate]], limit: int) -> Sequence[DiscoveryCandidate]`, and `collect_official(client: JsonHttpClient, config: RadarConfig, observed_at: str) -> CollectionResult` using the approved deterministic union/filter rules and four exact endpoints.

- [ ] **Step 4: Run all Chunk 1 tests**

Run: `python3 -m unittest steam-game-radar/tests/test_config.py steam-game-radar/tests/test_schemas.py steam-game-radar/tests/test_artifacts.py steam-game-radar/tests/test_http_client.py steam-game-radar/tests/test_official_parsers.py steam-game-radar/tests/test_official_collection.py`

Expected: exit code 0, `Ran 58 tests`, and `OK`; all fake clients report zero real network calls.

- [ ] **Step 5: Commit**

```bash
git add steam-game-radar/steam_game_radar/official_provider.py steam-game-radar/tests/test_official_collection.py
git commit -m "feat: collect official Steam candidates"
```

## Chunk 2: Imports, History, and Scoring

### Task 5: Manual SteamDB Import

**Files:**
- Create: `steam-game-radar/steam_game_radar/steamdb_import.py`
- Create: `steam-game-radar/tests/test_steamdb_import.py`
- Create: `steam-game-radar/tests/fixtures/steamdb_wishlist.csv`
- Create: `steam-game-radar/tests/fixtures/steamdb_views.json`

- [ ] **Step 1: Write 16 failing import tests**

Cover all four views; CSV view required; JSON array and wrapper; wrapper schema version; explicit/wrapper view mismatch; AppID and `/app/{id}` extraction; all aliases; comma/plus/K/M numbers; percentages; ISO and `DD Mon YYYY` dates in UTC; em-dash missing values; duplicate AppID rejection; unknown-column preservation; partial valid rows plus stable rejections; and no HTTP dependency.

- [ ] **Step 2: Run the red tests**

Run: `python3 -m unittest steam-game-radar/tests/test_steamdb_import.py`

Expected: exit code 1 and `FAILED (errors=1)`.

- [ ] **Step 3: Implement import records and functions**

Define `ImportResult(records: Sequence[GameRecord], rejected_rows: Sequence[RejectedRow], raw_canonical: object, view: str)`.

Implement `parse_number(value: object) -> int | float | None`, `parse_release_date(value: object) -> str | None`, `extract_appid(row: Mapping[str, object]) -> int`, and `import_steamdb(path: Path, view: str | None, observed_at: str) -> ImportResult`. The importer reads only local files, validates 5 MiB before parsing, creates per-metric `steamdb_manual_import` observations, and returns canonical parsed raw data for shared artifact persistence.

- [ ] **Step 4: Run the green tests**

Run: `python3 -m unittest steam-game-radar/tests/test_steamdb_import.py`

Expected: exit code 0, `Ran 16 tests`, and `OK`.

- [ ] **Step 5: Commit**

```bash
git add steam-game-radar/steam_game_radar/steamdb_import.py steam-game-radar/tests/test_steamdb_import.py steam-game-radar/tests/fixtures
git commit -m "feat: import manual SteamDB trends"
```

### Task 6: Exclusive Run Lock

**Files:**
- Create: `steam-game-radar/steam_game_radar/run_lock.py`
- Create: `steam-game-radar/tests/test_run_lock.py`

- [ ] **Step 1: Write 7 failing lock tests**

Test payload fields PID/run ID/host/acquired time, exclusive collision, context-manager cleanup, cleanup on exception, same-host live PID remains blocking after two hours, same-host dead PID older than two hours is removed, and foreign-host lock remains blocking regardless of age.

- [ ] **Step 2: Run the red tests**

Run: `python3 -m unittest steam-game-radar/tests/test_run_lock.py`

Expected: exit code 1 and `FAILED (errors=1)`.

- [ ] **Step 3: Implement `RunLock`**

Constructor parameters are `path: Path`, `run_id: str`, `now: Callable[[], datetime]`, `hostname: Callable[[], str]`, and `pid_alive: Callable[[int], bool]`. `__enter__` uses exclusive file creation; `__exit__` removes only the lock matching its own run ID. Blocking conditions raise `RunBusyError`.

- [ ] **Step 4: Run and commit**

Run: `python3 -m unittest steam-game-radar/tests/test_run_lock.py`

Expected: exit code 0, `Ran 7 tests`, and `OK`.

```bash
git add steam-game-radar/steam_game_radar/run_lock.py steam-game-radar/tests/test_run_lock.py
git commit -m "feat: lock Steam radar runs"
```

### Task 7: Immutable Snapshots and Raw Lifecycle

**Files:**
- Create: `steam-game-radar/steam_game_radar/snapshot.py`
- Create: `steam-game-radar/tests/test_snapshot.py`

- [ ] **Step 1: Write 12 failing snapshot tests**

Test run ID pattern; immutable same-day names; atomic snapshot JSON; snapshot schema version; sorted load order; 18h/36h one-day boundaries; 144h/192h seven-day boundaries; closest selection; newer-on-tie; out-of-window omission; manual canonical raw redaction/copy through `persist_raw`; and raw retention/size behavior through the shared artifact module.

- [ ] **Step 2: Run the red tests**

Run: `python3 -m unittest steam-game-radar/tests/test_snapshot.py`

Expected: exit code 1 and `FAILED (errors=1)`.

- [ ] **Step 3: Implement snapshot APIs**

Implement `make_run_id(now: datetime, entropy: bytes) -> str`, `persist_snapshot(config: RadarConfig, run_id: str, records: Sequence[GameRecord], metadata: Mapping[str, object]) -> Path`, `load_snapshots(config: RadarConfig) -> Sequence[Mapping[str, object]]`, and `select_comparison(snapshots: Sequence[Mapping[str, object]], current_time: datetime, target_hours: int, minimum_hours: int, maximum_hours: int) -> Mapping[str, object] | None`.

- [ ] **Step 4: Run and commit**

Run: `python3 -m unittest steam-game-radar/tests/test_snapshot.py steam-game-radar/tests/test_artifacts.py`

Expected: exit code 0, `Ran 20 tests`, and `OK`.

```bash
git add steam-game-radar/steam_game_radar/snapshot.py steam-game-radar/tests/test_snapshot.py
git commit -m "feat: persist Steam radar snapshots"
```

### Task 8: Merge, Fallback, and Trend Analysis

**Files:**
- Create: `steam-game-radar/steam_game_radar/merge.py`
- Create: `steam-game-radar/steam_game_radar/trend.py`
- Create: `steam-game-radar/tests/test_merge.py`
- Create: `steam-game-radar/tests/test_trend.py`

- [ ] **Step 1: Write 7 failing merge tests**

Test official snapshot eligibility at exactly 72h; manual-only baseline when no eligible official snapshot; newer metric wins; official wins exact-time tie; official app-details release status; valid manual released status only with non-future date; contradictory status rejection with `RejectedRow`; 36h stale warning; and manual baseline mode `manual_baseline` with status `manual_only`. Combine boundary assertions so the file contains exactly 7 methods. Confidence and action belong to Task 9 scoring tests.

- [ ] **Step 2: Write 5 failing trend tests**

Test same metric/source only; exact 1d percentage; exact 7d rank delta; no delta without history; rank choice 7d then 1d then provider previous; and newly observed flag. Combine the two no-history assertions into one method for exactly 5 tests.

- [ ] **Step 3: Run the red tests**

Run: `python3 -m unittest steam-game-radar/tests/test_merge.py steam-game-radar/tests/test_trend.py`

Expected: exit code 1 and `FAILED (errors=2)`.

- [ ] **Step 4: Implement merge API**

Define `MergeResult(records: Sequence[GameRecord], rejected_rows: Sequence[RejectedRow], warnings: Sequence[WarningRecord], mode: Literal["official_plus_manual", "manual_baseline"], data_status: Literal["fresh", "stale", "manual_only"])`.

Implement `merge_import_with_official(imported: Sequence[GameRecord], official_snapshot: Mapping[str, object] | None, now: datetime, config: RadarConfig) -> MergeResult` with exact timestamp/source/status precedence.

- [ ] **Step 5: Implement trend API**

Define `AnalyzedCandidate(record: GameRecord, deltas: Mapping[str, float], newly_observed: bool, warnings: Sequence[WarningRecord])`.

Implement `analyze_trends(current: Sequence[GameRecord], one_day_snapshot: Mapping[str, object] | None, seven_day_snapshot: Mapping[str, object] | None) -> Sequence[AnalyzedCandidate]` and `select_rank_improvement(candidate: AnalyzedCandidate) -> float | None`.

- [ ] **Step 6: Run and commit**

Run: `python3 -m unittest steam-game-radar/tests/test_merge.py steam-game-radar/tests/test_trend.py`

Expected: exit code 0, `Ran 12 tests`, and `OK`.

```bash
git add steam-game-radar/steam_game_radar/merge.py steam-game-radar/steam_game_radar/trend.py steam-game-radar/tests/test_merge.py steam-game-radar/tests/test_trend.py
git commit -m "feat: analyze Steam radar trends"
```

### Task 9: Enrichment and Exact Scores

**Files:**
- Create: `steam-game-radar/steam_game_radar/enrichment.py`
- Create: `steam-game-radar/steam_game_radar/score.py`
- Create: `steam-game-radar/tests/test_enrichment.py`
- Create: `steam-game-radar/tests/test_score.py`

- [ ] **Step 1: Write 8 failing enrichment tests**

Test schema/run ID, ISO observed time, integer score range, non-negative counts, HTTPS typed evidence, mandatory Google evidence, matching YouTube/Reddit evidence, duplicate AppID, and unknown evidence source. Combine range assertions for exactly 8 methods.

- [ ] **Step 2: Write 12 failing score tests**

Use table-driven subtests to cover every specified transform boundary and midpoint, released/unreleased minimum count and weight gates, 7d/1d/provider rank precedence, available-weight averaging, SEO gate, exact 60/40 combination, four actions, A/B/C confidence, a manual-only baseline becoming confidence C with no final action, C never final, and stable tie ordering.

- [ ] **Step 3: Run the red tests**

Run: `python3 -m unittest steam-game-radar/tests/test_enrichment.py steam-game-radar/tests/test_score.py`

Expected: exit code 1 and `FAILED (errors=2)`.

- [ ] **Step 4: Implement enrichment records**

Define `Evidence(source: Literal["google", "youtube", "reddit"], url: str)`, `EnrichmentRecord(appid: int, google_competition_gap_score: int, expandable_queries: Sequence[str], youtube_relevant_7d: int | None, reddit_relevant_7d: int | None, reddit_upvotes_7d: int | None, evidence: Sequence[Evidence])`, and `EnrichmentBundle(schema_version: int, run_id: str, observed_at: str, games: Mapping[int, EnrichmentRecord])`. Implement `load_enrichment(path: Path, expected_run_id: str) -> EnrichmentBundle`.

- [ ] **Step 5: Implement exact pure score functions**

Implement `interpolate(points: Sequence[tuple[float, float]], value: float) -> float`, `score_released(candidate: AnalyzedCandidate) -> ScoredCandidate`, `score_unreleased(candidate: AnalyzedCandidate) -> ScoredCandidate`, `score_seo(record: EnrichmentRecord) -> float | None`, `apply_final_score(candidate: ScoredCandidate, record: EnrichmentRecord, provenance: Collection[str]) -> ScoredCandidate`, and `candidate_sort_key(candidate: ScoredCandidate) -> Sequence[object]`.

`ScoredCandidate` contains record, deltas, metric scores, Steam heat, SEO score, final score, action, confidence, warnings, typed evidence, and recommended content types. Use the approved fixed tables, one-decimal persisted scores, gates, 60/40 combination, and action ranges. The module performs no I/O or clock access.

- [ ] **Step 6: Run all Chunk 2 tests**

Run: `python3 -m unittest steam-game-radar/tests/test_steamdb_import.py steam-game-radar/tests/test_run_lock.py steam-game-radar/tests/test_snapshot.py steam-game-radar/tests/test_merge.py steam-game-radar/tests/test_trend.py steam-game-radar/tests/test_enrichment.py steam-game-radar/tests/test_score.py`

Expected: exit code 0, `Ran 67 tests`, and `OK`.

- [ ] **Step 7: Commit**

```bash
git add steam-game-radar/steam_game_radar/enrichment.py steam-game-radar/steam_game_radar/score.py steam-game-radar/tests/test_enrichment.py steam-game-radar/tests/test_score.py
git commit -m "feat: score Steam trend opportunities"
```

## Chunk 3: Reports, CLI, and Skill Integration

### Task 10: Canonical Reports and Monotonic Publication

**Files:**
- Create: `steam-game-radar/steam_game_radar/report.py`
- Create: `steam-game-radar/tests/test_report.py`
- Create: `steam-game-radar/references/report-template.md`

- [ ] **Step 1: Write 12 failing report tests**

Test all top-level fields `report_schema_version`, `run_id`, `phase`, `mode`, `generated_at`, `data_status`, released/unreleased/newly-observed arrays, warnings, and rejected rows; every candidate field from the spec including provenance/typed evidence/content types; stable sorting; Markdown derived from JSON; immutable timestamped non-overwrite; same-run preliminary-to-final latest advancement; newer-run latest; older delayed enrichment skipped from latest; both latest files updated together; and rollback plus `PersistenceError` when one of JSON/Markdown cannot persist.

- [ ] **Step 2: Run the red tests**

Run: `python3 -m unittest steam-game-radar/tests/test_report.py`

Expected: exit code 1 and `FAILED (errors=1)`.

- [ ] **Step 3: Implement explicit report APIs**

Implement `build_report(run_id: str, phase: Literal["preliminary", "final"], mode: Literal["official_scan", "official_plus_manual", "manual_baseline"], generated_at: str, data_status: Literal["fresh", "stale", "manual_only"], released: Sequence[ScoredCandidate], unreleased: Sequence[ScoredCandidate], newly_observed: Sequence[int], warnings: Sequence[WarningRecord], rejected_rows: Sequence[RejectedRow]) -> dict[str, object]`.

Implement `render_markdown(report: Mapping[str, object]) -> str`, `should_publish_latest(candidate: Mapping[str, object], existing: Mapping[str, object] | None) -> bool`, and `persist_report(config: RadarConfig, report: Mapping[str, object], lock: RunLock) -> tuple[Path, Path]`.

Allowed phases/modes/statuses are exactly the literal values above. `persist_report` prepares and fsyncs JSON/Markdown temp files first, atomically publishes immutable files, rolls back the first immutable file if the second fails, then performs the same guarded pair update for latest files while the supplied lock is held. It raises `PersistenceError` on any incomplete pair.

- [ ] **Step 4: Run the green tests**

Run: `python3 -m unittest steam-game-radar/tests/test_report.py`

Expected: exit code 0, `Ran 12 tests`, and `OK`.

- [ ] **Step 5: Commit**

```bash
git add steam-game-radar/steam_game_radar/report.py steam-game-radar/tests/test_report.py steam-game-radar/references/report-template.md
git commit -m "feat: generate Steam radar reports"
```

### Task 11: Import-Safe CLI and Exit Codes

**Files:**
- Create: `steam-game-radar/scripts/steam_radar.py`
- Create: `steam-game-radar/tests/test_cli.py`

- [ ] **Step 1: Write 15 failing CLI tests**

Test exact parsing for scan/import/enrich, project-root path resolution, sibling-package import when executed by file path, lock lifecycle, scan baseline, fresh/stale/expired provider fallback, manual-only baseline, imported canonical raw persistence, partial rejection reporting, enrichment run match, report calls, exit codes 2 through 6, unexpected exception exit 1 without domain conversion, and delayed run-A enrichment after newer run-B latest.

- [ ] **Step 2: Run the red tests**

Run: `python3 -m unittest steam-game-radar/tests/test_cli.py`

Expected: exit code 1 and `FAILED (errors=1)`.

- [ ] **Step 3: Make direct script execution import-safe**

The first executable statements after standard-library imports are:

```python
SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))
```

This preserves the approved command `python3 steam-game-radar/scripts/steam_radar.py` without packaging installation.

- [ ] **Step 4: Implement CLI functions and exception mapping**

Implement `build_parser() -> argparse.ArgumentParser`, `run_scan(args: argparse.Namespace, services: Services) -> int`, `run_import(args: argparse.Namespace, services: Services) -> int`, `run_enrich(args: argparse.Namespace, services: Services) -> int`, and `main(argv: Sequence[str] | None = None) -> int`.

Map `InputValidationError` to 2, `ProviderUnavailableError` to 3, `ConfigurationError` to 4, `PersistenceError` to 5, and `RunBusyError` to 6. Unexpected exceptions print a traceback and return 1. `Services` injects client, clock, entropy, hostname/PID checks, and filesystem functions for offline tests.

- [ ] **Step 5: Run the green tests**

Run: `python3 -m unittest steam-game-radar/tests/test_cli.py`

Expected: exit code 0, `Ran 15 tests`, and `OK`.

- [ ] **Step 6: Commit**

```bash
git add steam-game-radar/scripts/steam_radar.py steam-game-radar/tests/test_cli.py
git commit -m "feat: orchestrate Steam radar runs"
```

### Task 12: Skill, References, Schedules, and Catalog

**Files:**
- Create: `steam-game-radar/SKILL.md`
- Create: `steam-game-radar/references/data-sources.md`
- Create: `steam-game-radar/references/scoring-rules.md`
- Create: `steam-game-radar/references/steamdb-import-format.md`
- Modify: `README.md`
- Create: `steam-game-radar/tests/test_skill_docs.py`

- [ ] **Step 1: Write 10 failing documentation tests**

Test valid frontmatter/name/description; both manual trigger phrases; exact three CLI commands; 11:00 `Asia/Shanghai` agent schedule running scan/enrichment; conventional cron explicitly preliminary-only; SteamDB no-scrape/import-only rule; data-sources reference containing all four endpoints/auth/hosts/capabilities; import reference containing four views, all aliases, formats, duplicate rules; scoring reference containing all transforms/gates/60-40/actions/confidence; report template containing all canonical top-level and candidate fields; README separating Steam/HTML5 and matching actual `SKILL.md` count; automated Python transport files containing neither `steamdb.info` nor a host outside the allowlist.

- [ ] **Step 2: Run the red tests**

Run: `python3 -m unittest steam-game-radar/tests/test_skill_docs.py`

Expected: exit code 1 and assertion failures for missing documents.

- [ ] **Step 3: Write concise Skill routing and complete references**

`SKILL.md` routes `跑 Steam 雷达` to scan then Agent Top-10 enrichment then enrich; routes SteamDB import to import then enrichment; provides the exact host-agent schedule and conventional cron; and states SteamDB is never requested automatically. Put endpoint tables, import tables, score tables, and report schema in the named references without changing the spec.

- [ ] **Step 4: Update README from the filesystem count**

Run `rg --files -g 'SKILL.md' | wc -l` after creating the new Skill, use that number consistently, add `steam-game-radar` under game/data intelligence, and retain `html5-game-radar` as browser-playable discovery.

- [ ] **Step 5: Run the green documentation tests**

Run: `python3 -m unittest steam-game-radar/tests/test_skill_docs.py`

Expected: exit code 0, `Ran 10 tests`, and `OK`.

- [ ] **Step 6: Commit**

```bash
git add steam-game-radar/SKILL.md steam-game-radar/references README.md steam-game-radar/tests/test_skill_docs.py
git commit -m "docs: add Steam game radar skill"
```

### Task 13: Offline Verification and Opt-In Live Smoke Test

**Files:**
- Create: `steam-game-radar/tests/test_live_official_provider.py`
- Create only the live test in this task. If verification exposes a defect, stop this task, return to the responsible earlier task, add a failing regression test, implement the fix, rerun that task's tests, and commit the exact affected files before resuming Task 13.

- [ ] **Step 1: Add one opt-in live test**

The test uses `@unittest.skipUnless(os.environ.get("STEAM_RADAR_LIVE") == "1", "live Steam check disabled")`, loads `config.example.json`, sets candidate limits to 2, calls the official provider, and asserts at least one capability is true without asserting changing game names or ranks.

- [ ] **Step 2: Run the canonical offline suite**

Run: `python3 -m unittest discover -s steam-game-radar/tests -p 'test_*.py'`

Expected: exit code 0, `Ran 163 tests`, `OK (skipped=1)`, and no network calls.

- [ ] **Step 3: Compile and inspect policy**

Run: `python3 -m compileall -q steam-game-radar`

Expected: exit code 0 and no output.

Run: `git diff --check`

Expected: exit code 0 and no output.

Run: `rg -n 'steamdb\.info' steam-game-radar/steam_game_radar steam-game-radar/scripts`

Expected: exit code 1 and no output.

- [ ] **Step 4: Run an exact manual-import smoke test**

```bash
REPO_ROOT=$(pwd -P)
RADAR_SMOKE_DIR=$(mktemp -d /tmp/steam-radar-smoke.XXXXXX)
cd "$RADAR_SMOKE_DIR"
python3 "$REPO_ROOT/steam-game-radar/scripts/steam_radar.py" import-steamdb \
  --config "$REPO_ROOT/steam-game-radar/references/config.example.json" \
  --view wishlist_activity \
  --input "$REPO_ROOT/steam-game-radar/tests/fixtures/steamdb_wishlist.csv"
python3 -c 'import glob,json,sys,pathlib; root=pathlib.Path(sys.argv[1]); js=glob.glob(str(root/"reports/steam-game-radar/*.preliminary.json")); md=glob.glob(str(root/"reports/steam-game-radar/*.preliminary.md")); assert len(js)==1 and len(md)==1; report=json.load(open(js[0], encoding="utf-8")); assert report["mode"]=="manual_baseline" and report["data_status"]=="manual_only"; candidates=report["unreleased"]; assert candidates and all(c["confidence"]=="C" for c in candidates); assert all(not c["deltas"] and c["final_score"] is None and c["action"]=="needs_seo_enrichment" for c in candidates); assert (root/"reports/steam-game-radar/latest.json").exists() and (root/"reports/steam-game-radar/latest.md").exists(); text=pathlib.Path(md[0]).read_text(encoding="utf-8"); assert "Unreleased" in text' "$RADAR_SMOKE_DIR"
```

The fixed wishlist fixture must contain an unreleased game with a release date within 30 days and a follower/wishlist gain, so it passes the preliminary Steam-heat gate. Expected: both commands exit 0; the machine assertions verify exactly one timestamped preliminary JSON/Markdown pair, both latest files, mode `manual_baseline`, status `manual_only`, confidence C, empty deltas, null final score, exact action `needs_seo_enrichment`, and an Unreleased Markdown section. The temporary directory may remain for inspection until system cleanup.

- [ ] **Step 5: Keep the live test opt-in**

Default action: do not set `STEAM_RADAR_LIVE`; record `not run` in handoff.

Optional command when explicitly authorized and network is available:

Run: `STEAM_RADAR_LIVE=1 python3 -m unittest steam-game-radar/tests/test_live_official_provider.py`

Expected if run: exit code 0, `Ran 1 test`, and `OK`. A provider timeout/failure is reported as live-environment evidence, not hidden or converted into offline-suite success.

- [ ] **Step 6: Commit the opt-in test**

```bash
git add steam-game-radar/tests/test_live_official_provider.py
git commit -m "test: verify Steam radar workflow"
```

- [ ] **Step 7: Record final evidence**

Run: `git status --short --branch` and `git log --oneline --max-count=15`.

Expected: clean branch with the design, implementation-plan, scoped feature commits, and exact verification results ready for handoff. Do not claim live Steam success unless Step 5 was authorized and passed.
