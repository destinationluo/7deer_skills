# Unified Game Opportunity Radar Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one local Agent Skill that produces a daily ranked opportunity list across itch.io, Steam, and Roblox while preventing zero-demand and single-spike games from receiving positive action labels.

**Architecture:** A Python 3.11 standard-library CLI stores immutable platform observations in SQLite, normalizes platform-local heat, accepts browser/Agent evidence through versioned JSON envelopes, applies an ordered search-demand gate, and publishes canonical JSON/Markdown reports. The unified Steam collector adapts the already tested `steam-game-radar` package; itch.io and Roblox use strict browser-observation contracts so volatile page automation remains in the Agent layer.

**Tech Stack:** Python 3.11+, standard-library `dataclasses`, `sqlite3`, `urllib`, `json`, `unittest`, Agent Skills Markdown.

**Spec:** `docs/superpowers/specs/2026-08-31-unified-game-opportunity-radar-design.md`

**Required practices:** @code-development-rules, @superpowers:test-driven-development, @skill-creator, @superpowers:verification-before-completion.

---

## File Map

| File | Responsibility |
|---|---|
| `unified-game-radar/unified_game_radar/errors.py` | Domain exceptions and CLI exit-code mapping |
| `unified-game-radar/unified_game_radar/config.py` | Versioned configuration and project-relative paths |
| `unified-game-radar/unified_game_radar/schemas.py` | Immutable identities, observations, evidence, scores, and JSON conversion |
| `unified-game-radar/unified_game_radar/storage.py` | SQLite schema, transactions, snapshots, and idempotency |
| `unified-game-radar/unified_game_radar/run_lock.py` | Project-scoped exclusive run lock adapted from the tested Steam contract |
| `unified-game-radar/unified_game_radar/artifacts.py` | Redacted, bounded, immutable raw artifact persistence and retention |
| `unified-game-radar/unified_game_radar/identity.py` | Name normalization and conservative cross-platform linking |
| `unified-game-radar/unified_game_radar/normalize.py` | Platform heat transforms and percentile normalization |
| `unified-game-radar/unified_game_radar/demand.py` | Trends aggregation, disambiguation, ordered demand gate |
| `unified-game-radar/unified_game_radar/score.py` | Exact component scores, action overrides, stable ordering |
| `unified-game-radar/unified_game_radar/report.py` | Canonical JSON/Markdown and publication semantics |
| `unified-game-radar/unified_game_radar/orchestration.py` | Scan, ingest, enrich, report service methods |
| `unified-game-radar/unified_game_radar/collectors/base.py` | Collector protocol and source-health result |
| `unified-game-radar/unified_game_radar/collectors/steam.py` | Adapter over committed `steam-game-radar` provider code |
| `unified-game-radar/unified_game_radar/collectors/itch.py` | itch.io browser-observation validation and heat inputs |
| `unified-game-radar/unified_game_radar/collectors/roblox.py` | Roblox chart-observation validation and heat inputs |
| `unified-game-radar/scripts/game_radar.py` | Import-safe CLI entry point |
| `unified-game-radar/SKILL.md` | Agent workflow, triggers, schedules, and safety boundaries |
| `unified-game-radar/references/*.md` | Collection, evidence, score, and report contracts |
| `unified-game-radar/references/config.example.json` | Runnable default configuration |
| `unified-game-radar/tests/test_*.py` | Offline unit and integration tests |
| `html5-game-radar/SKILL.md` | Deprecate competing schedule; route users to unified Skill |
| `steam-game-radar/SKILL.md` | Deprecate competing schedule; route users to unified Skill |
| `README.md` | Catalog the unified Skill and canonical daily workflow |

## Chunk 1: Core Domain, Persistence, Identity, and Normalization

### Task 1A: Errors and Complete Configuration Contract

**Files:**
- Create: `unified-game-radar/unified_game_radar/__init__.py`
- Create: `unified-game-radar/unified_game_radar/errors.py`
- Create: `unified-game-radar/unified_game_radar/config.py`
- Create: `unified-game-radar/references/config.example.json`
- Create: `unified-game-radar/tests/test_config.py`

- [ ] **Step 1: Write failing configuration tests**

Cover defaults, file loading, project-relative paths, rejected unknown fields,
invalid timezone, invalid schema version, invalid limits, 10:00/16:00
publication settings, identity aliases, and Steam sub-configuration conversion.
Lock the complete version-1 field/default map:

```python
RadarConfig(
    schema_version=1,
    timezone="Asia/Shanghai",
    country="US",
    locale="en",
    steam_language="english",
    steam_released_candidate_limit=50,
    steam_unreleased_candidate_limit=50,
    collection_hours=(10, 16),
    daily_publish_hour=16,
    enabled_platforms=("itch", "steam", "roblox"),
    preliminary_top_n=20,
    enrichment_top_n=10,
    final_top_n=10,
    heat_floor=30.0,
    fresh_hours=6,
    stale_fallback_hours=72,
    raw_retention_days=14,
    raw_max_bytes_per_provider=5_242_880,
    request_timeout_seconds=15.0,
    max_retries=3,
    minimum_request_interval_seconds=1.0,
    data_dir=Path("data/unified-game-radar"),
    report_dir=Path("reports/unified-game-radar"),
    identity_aliases=(),
)
```

`IdentityAlias` is a versioned pair of exact platform keys:
`IdentityAlias(schema_version=1, source="steam:123", target="roblox:456")`.
`RadarConfig.to_steam_config()` lazily constructs the existing
`steam_game_radar.config.RadarConfig` with country, language, timezone,
released/unreleased limits, request policy, retention, and Steam-specific
temporary data/report paths. The default mapping is `locale="en"` to
`steam_language="english"`; tests assert the complete legacy dataclass rather
than a partial dictionary.

```python
def test_defaults_resolve_from_project_root(self):
    config = RadarConfig.from_mapping({}, project_root=Path("/tmp/project"))
    self.assertEqual(config.data_dir, Path("/tmp/project/data/unified-game-radar"))
    self.assertEqual(config.report_dir, Path("/tmp/project/reports/unified-game-radar"))
    self.assertEqual(config.collection_hours, (10, 16))
    self.assertEqual(config.daily_publish_hour, 16)
```

- [ ] **Step 2: Run red configuration tests**

Run: `python3 -m unittest unified-game-radar/tests/test_config.py`

Expected: exit 1 with import errors for the not-yet-created package.

- [ ] **Step 3: Implement errors and configuration**

Use a frozen dataclass, `ZoneInfo` validation, exact-field mapping validation,
immutable alias records, and project-relative `Path` resolution. Define domain
exceptions and exit codes: 0 success, 2 input/schema, 3 provider unavailable,
4 configuration, 5 persistence/report, 6 run lock, 7 idempotency conflict.

- [ ] **Step 4: Run green configuration tests**

Run: `python3 -m unittest unified-game-radar/tests/test_config.py`

Expected: exit 0 and `OK`.

- [ ] **Step 5: Commit**

```bash
git add unified-game-radar/unified_game_radar/__init__.py unified-game-radar/unified_game_radar/errors.py unified-game-radar/unified_game_radar/config.py unified-game-radar/references/config.example.json unified-game-radar/tests/test_config.py
git commit -m "feat: configure unified game radar"
```

### Task 1B: Shared Versioned Schemas

**Files:**
- Create: `unified-game-radar/unified_game_radar/schemas.py`
- Create: `unified-game-radar/tests/test_schemas.py`

- [ ] **Step 1: Write failing schema round-trip and validation tests**

Define and test every cross-task type:

```python
RadarRun(schema_version, run_id, started_at, mode, platforms, publish_daily)
PlatformRecord(schema_version, platform, platform_id, name, developer,
               official_domain, url)
GameIdentity(schema_version, opportunity_id, name, normalized_name,
             developer, official_domain, platform_records)
PlatformObservation(schema_version, observation_id, run_id, platform,
                    platform_id, provider, surface, geo, locale,
                    query_parameters, metric_definition_version, observed_at,
                    release_at, source_rank, raw_metrics, evidence_urls)
ObservationEnvelope(schema_version, run_id, collector, surface, geo, locale,
                    metric_definition_version, observations)
TrendPoint(date, value, complete)
TrendEvidence(query, query_type, timeframe, geo, category, property, timezone,
              points, comparison_term, comparison_average, evidence_url,
              raw_artifact, observed_at)
SearchQueryEvidence(schema_version, query, observed_at, source_url)
ExternalEvidence(source, url, published_at, observed_at, author_relation,
                 engagement_count, evidence_kind)
SerpEvidence(query, relevant_nonofficial_results, guide_results,
             missing_intents, evidence_url, observed_at)
OpportunityEvidence(schema_version, run_id, opportunity_id, observed_at,
                    trends, autocomplete_queries, related_queries,
                    external_evidence, serp)
SourceHealth(schema_version, run_id, collector, status, observed_at,
             capabilities, warnings)
PlatformHeat(schema_version, run_id, platform_key, surface, observation_ids,
             heat)
NormalizedHeat(schema_version, run_id, platform_key, surface, observation_ids,
               heat, platform_score)
ScoredOpportunity(schema_version, run_id, opportunity_id, demand_state,
                  platform_score, demand_score, external_score, seo_score,
                  total_score, action, warnings)
WarningRecord(schema_version, code, message, collector, opportunity_id)
RawArtifact(schema_version, run_id, provider, path, observed_at, sha256)
Publication(schema_version, run_id, phase, published_at, report_json,
            report_markdown, daily_date, advances_daily_latest)
CommandManifest(schema_version, run_id, phase, report_json, report_markdown,
                source_health, warnings, outstanding_tasks)
OutstandingTask(schema_version, run_id, collector, surface, action,
                collection_contract)
PreliminaryResult(schema_version, run_id, candidates, source_health, warnings,
                  outstanding_tasks)
```

Reject unexpected JSON keys. Every independently persisted record carries
`schema_version=1`; every per-run record carries `run_id`. Optional unavailable
metric values serialize as omitted or `null`, never fabricated zero. Also
validate UUIDs, UTC timestamps, HTTPS evidence URLs, allowed literals, finite
numbers, and immutable mappings.

- [ ] **Step 2: Run red schema tests**

Run: `python3 -m unittest unified-game-radar/tests/test_schemas.py`

Expected: exit 1 because `schemas.py` does not exist.

- [ ] **Step 3: Implement frozen schemas and exact converters**

Use frozen dataclasses, explicit `to_dict`/`from_dict`, and
`MappingProxyType`. Share small validation helpers rather than accepting loose
dictionaries in later modules.

- [ ] **Step 4: Run green schemas and legacy Steam suite**

Run: `python3 -m unittest unified-game-radar/tests/test_schemas.py`

Expected: exit 0 and `OK`.

Run: `python3 -m unittest discover -s steam-game-radar/tests -p 'test_*.py'`

Expected: exit 0, 163 tests pass, 1 skipped.

- [ ] **Step 5: Commit**

```bash
git add unified-game-radar/unified_game_radar/schemas.py unified-game-radar/tests/test_schemas.py
git commit -m "feat: define unified radar domain schemas"
```

### Task 2A: SQLite Schema, Transactions, and Basic CRUD

**Files:**
- Create: `unified-game-radar/unified_game_radar/storage.py`
- Create: `unified-game-radar/tests/test_storage.py`

- [ ] **Step 1: Write failing database and transaction tests**

Cover schema creation and migration version, foreign keys, transaction rollback,
run creation, identity/platform binding, source-health CRUD, score CRUD,
publication CRUD, the same opportunity across multiple runs, run foreign-key
enforcement for source health/scores, and connection closure.

```python
def test_transaction_rolls_back_all_rows(self):
    with self.assertRaises(RuntimeError):
        with store.transaction():
            store.create_run(run)
            raise RuntimeError("stop")
    self.assertIsNone(store.get_run(run.run_id))
```

- [ ] **Step 2: Run red test**

Run: `python3 -m unittest unified-game-radar/tests/test_storage.py`

Expected: exit 1 because `storage.py` does not exist.

- [ ] **Step 3: Implement the SQLite schema and basic store API**

Create tables `runs`, `source_health`, `game_identities`, `platform_records`, `observations`, `identity_links`, `evidence`, `scores`, and `publications`. Enable `PRAGMA foreign_keys=ON`, use WAL when supported, write through transactions, and persist canonical JSON plus indexed identity/time fields. Implement:

```python
class RadarStore:
    def initialize(self) -> None: ...
    @contextmanager
    def transaction(self) -> Iterator[None]: ...
    def create_run(self, run: RadarRun) -> None: ...
    def get_run(self, run_id: str) -> RadarRun | None: ...
    def upsert_identity(self, identity: GameIdentity) -> None: ...
    def bind_platform_record(self, opportunity_id: str,
                             record: PlatformRecord) -> None: ...
    def save_source_health(self, health: SourceHealth) -> None: ...
    def save_score(self, score: ScoredOpportunity) -> None: ...
    def publish(self, publication: Publication) -> None: ...
```

- [ ] **Step 4: Run green test**

Run: `python3 -m unittest unified-game-radar/tests/test_storage.py`

Expected: exit 0 and `OK`.

- [ ] **Step 5: Commit**

```bash
git add unified-game-radar/unified_game_radar/storage.py unified-game-radar/tests/test_storage.py
git commit -m "feat: persist unified radar snapshots"
```

### Task 2B: Observation/Evidence Idempotency and Compatible History

**Files:**
- Modify: `unified-game-radar/unified_game_radar/storage.py`
- Create: `unified-game-radar/tests/test_storage_history.py`

- [ ] **Step 1: Write failing immutable-ingest tests**

Test observation run mismatch, identical observation retry no-op, changed
observation payload conflict, one evidence row per `(run_id, opportunity_id)`,
identical evidence retry no-op, and changed evidence conflict.

```python
def test_observation_id_conflict_rejects_changed_payload(self):
    self.assertTrue(store.insert_observation(observation))
    self.assertFalse(store.insert_observation(observation))
    with self.assertRaises(IdempotencyConflictError):
        store.insert_observation(replace(observation, source_rank=99))
```

- [ ] **Step 2: Write failing compatible-history tests**

Compatibility requires identical provider, surface, geo, locale, canonical
sorted query parameters, and metric-definition version. Selection is past-only,
targets 24h±6h or 168h±24h, sorts by absolute target-distance, then newest
`observed_at`, then lexical `observation_id`, and returns
`PlatformObservation | None`. Prove intraday observations never fill 1d fields.

- [ ] **Step 3: Run red tests**

Run: `python3 -m unittest unified-game-radar/tests/test_storage_history.py`

Expected: exit 1 because history/idempotency methods are missing.

- [ ] **Step 4: Implement typed methods**

```python
def insert_observation(self, observation: PlatformObservation) -> bool: ...
def insert_evidence(self, evidence: OpportunityEvidence) -> bool: ...
def compatible_observation(
    self,
    current: PlatformObservation,
    target_hours: int,
    tolerance_hours: int,
) -> PlatformObservation | None: ...
```

- [ ] **Step 5: Run green tests and commit**

Run: `python3 -m unittest unified-game-radar/tests/test_storage.py unified-game-radar/tests/test_storage_history.py`

Expected: exit 0 and `OK`.

```bash
git add unified-game-radar/unified_game_radar/storage.py unified-game-radar/tests/test_storage_history.py
git commit -m "feat: enforce immutable radar history"
```

### Task 2C: Project-Scoped Run Lock

**Files:**
- Create: `unified-game-radar/unified_game_radar/run_lock.py`
- Create: `unified-game-radar/tests/test_run_lock.py`

- [ ] **Step 1: Write failing lock-contract tests**

Adapt the existing `steam_game_radar.run_lock` behavior. Cover exclusive
ownership, context-manager release, release in `finally`, same-host recovery
only when the lock is older than the stale threshold, the recorded PID is dead,
and lock identity is unchanged; live same-process/live-PID locks always block.
Also cover conservative foreign-host refusal, malformed lock refusal, and
owner-only release.

- [ ] **Step 2: Run red test**

Run: `python3 -m unittest unified-game-radar/tests/test_run_lock.py`

Expected: exit 1 because unified `run_lock.py` does not exist.

- [ ] **Step 3: Port the tested lock contract with unified paths/errors**

Do not modify the Steam lock module. Reuse its file format and safety rules,
then expose `RunLock.acquire()`, `RunLock.release()`, and context-manager
methods under the unified package.

- [ ] **Step 4: Run unified and Steam lock tests**

Run: `python3 -m unittest unified-game-radar/tests/test_run_lock.py steam-game-radar/tests/test_run_lock.py`

Expected: exit 0 and `OK`.

- [ ] **Step 5: Commit**

```bash
git add unified-game-radar/unified_game_radar/run_lock.py unified-game-radar/tests/test_run_lock.py
git commit -m "feat: lock unified radar runs"
```

### Task 3: Conservative Identity Linking

**Files:**
- Create: `unified-game-radar/unified_game_radar/identity.py`
- Create: `unified-game-radar/tests/test_identity.py`

- [ ] **Step 1: Write failing identity tests**

Test Unicode normalization, punctuation folding, exact platform-ID reuse,
title+developer merge, title+official-domain merge, versioned manual aliases,
same-name/different-developer separation, missing-developer separation,
order-independent linking, and repeated-run ID stability.

```python
def test_same_name_different_developer_does_not_merge(self):
    left = record("Echo", "Studio A", "steam", "1")
    right = record("Echo", "Studio B", "roblox", "2")
    self.assertIsNone(match_identity(left, (identity_for(left),), aliases={}))
```

- [ ] **Step 2: Run red test**

Run: `python3 -m unittest unified-game-radar/tests/test_identity.py`

Expected: exit 1 because `identity.py` does not exist.

- [ ] **Step 3: Implement pure normalization and matching**

Implement `normalize_name`, `normalize_developer`, `canonical_domain`,
`platform_key`, and `match_identity`. `match_identity` returns an existing
`opportunity_id` or `None`; it never creates IDs. Orchestration creates a new
identity with an injected `id_factory` and immediately persists it before
linking additional records. Manual aliases come from `RadarConfig` as exact
versioned source/target platform-key pairs and are never inferred from a bare
title.

- [ ] **Step 4: Run green test and commit**

Run: `python3 -m unittest unified-game-radar/tests/test_identity.py`

Expected: exit 0 and `OK`.

```bash
git add unified-game-radar/unified_game_radar/identity.py unified-game-radar/tests/test_identity.py
git commit -m "feat: link cross-platform game identities"
```

### Task 4: Exact Platform Heat and Cohort Normalization

**Files:**
- Create: `unified-game-radar/unified_game_radar/normalize.py`
- Create: `unified-game-radar/tests/test_normalize.py`
- Create: `unified-game-radar/references/scoring-rules.md`

- [ ] **Step 1: Write table-driven failing heat tests**

Encode every itch.io, Steam released/upcoming, and Roblox boundary from the
spec. Include missing components, heat floor 30, first-observation behavior,
incompatible deltas, and multi-surface max selection while retaining all
observation IDs.

- [ ] **Step 2: Write failing percentile tests**

Cover one item, four items, five items, tied heat, all-low heat, all-tied heat,
average ranks, deterministic ordering, one-decimal rounding, and proof that
platform or incompatible surface cohorts never mix.

```python
def test_one_item_cohort_cannot_receive_more_than_fifteen(self):
    scored = normalize_cohort((heat("only", 100),))
    self.assertEqual(scored[0].platform_score, 15.0)
```

- [ ] **Step 3: Run red tests**

Run: `python3 -m unittest unified-game-radar/tests/test_normalize.py`

Expected: exit 1 because heat functions are undefined.

- [ ] **Step 4: Implement explicit heat contracts and pure functions**

Use the `PlatformHeat` and `NormalizedHeat` schemas. Implement
`score_itch_heat`, `score_steam_released_heat`,
`score_steam_upcoming_heat`, `score_roblox_heat`, `select_record_heat`,
`eligible_cohort`, `average_tie_rank`, and `normalize_cohort` exactly from the
spec. `select_record_heat` chooses the maximum compatible-surface heat for one
platform record but retains every contributing observation ID. Cohort grouping
by platform and compatible surface occurs here; orchestration passes all
eligible record heats and receives normalized results. Keep I/O and clock
access out of this module.

- [ ] **Step 5: Document exact formulas and run green tests**

Run: `python3 -m unittest unified-game-radar/tests/test_normalize.py`

Expected: exit 0 and `OK`.

- [ ] **Step 6: Run Chunk 1 tests and commit**

Run: `python3 -m unittest unified-game-radar/tests/test_config.py unified-game-radar/tests/test_schemas.py unified-game-radar/tests/test_storage.py unified-game-radar/tests/test_storage_history.py unified-game-radar/tests/test_run_lock.py unified-game-radar/tests/test_identity.py unified-game-radar/tests/test_normalize.py`

Expected: exit 0 and `OK`.

```bash
git add unified-game-radar/unified_game_radar/normalize.py unified-game-radar/tests/test_normalize.py unified-game-radar/references/scoring-rules.md
git commit -m "feat: normalize platform opportunity heat"
```

## Chunk 2: Platform Collectors and Preliminary Pipeline

### Task 5: Collector Protocol and Source Health

**Files:**
- Create: `unified-game-radar/unified_game_radar/collectors/__init__.py`
- Create: `unified-game-radar/unified_game_radar/collectors/base.py`
- Create: `unified-game-radar/tests/test_collector_base.py`

- [ ] **Step 1: Write failing result-contract tests**

Test immutable observations, capability status, raw artifact descriptors,
`fresh/partial/stale/unavailable/not_run`, six-hour fresh cutoff, 72-hour
stale cutoff, partial capability semantics, provider exception isolation, and
browser `not_run → fresh/partial` transitions.

- [ ] **Step 2: Run red test**

Run: `python3 -m unittest unified-game-radar/tests/test_collector_base.py`

Expected: exit 1 because collector types do not exist.

- [ ] **Step 3: Implement `CollectorResult` and health helpers**

```python
class Collector(Protocol):
    def collect(self, run: RadarRun) -> CollectorResult: ...

@dataclass(frozen=True)
class CollectorResult:
    collector: str
    observations: tuple[PlatformObservation, ...]
    health: SourceHealth
    raw_artifacts: tuple[RawArtifact, ...]

def classify_source_health(
    run_id: str,
    now: datetime,
    attempted: bool,
    active_observations: Sequence[PlatformObservation],
    capabilities: Mapping[str, bool] | None,
    fallback_observed_at: datetime | None,
    warnings: Sequence[WarningRecord],
    fresh_hours: int,
    stale_fallback_hours: int,
) -> SourceHealth: ...
```

`SourceHealth.warnings` is the canonical warning owner; collectors do not keep
a second warning list. `attempted=False` is `not_run`. When attempted, active
observations no older than configured `fresh_hours` are `fresh`
when all declared capabilities succeeded and `partial` when a same-run
capability failed. No active observations plus a fallback no older than
configured `stale_fallback_hours` is `stale`; older/no fallback is
`unavailable`. Test otherwise-identical empty inputs with `attempted=False`
and `attempted=True` separately. Orchestration converts a provider exception
into stale/unavailable health without aborting other collectors.

- [ ] **Step 4: Run green test and commit**

Run: `python3 -m unittest unified-game-radar/tests/test_collector_base.py`

Expected: exit 0 and `OK`.

```bash
git add unified-game-radar/unified_game_radar/collectors unified-game-radar/tests/test_collector_base.py
git commit -m "feat: define radar collector contract"
```

### Task 6: Adapt the Existing Steam Provider

**Files:**
- Create: `unified-game-radar/unified_game_radar/collectors/steam.py`
- Create: `unified-game-radar/tests/test_steam_collector.py`
- Modify: `unified-game-radar/references/config.example.json`

- [ ] **Step 1: Write failing adapter tests against existing Steam fixtures**

Use `steam-game-radar/tests/fixtures/most_played.json`,
`featured_categories.json`, `appdetails.json`, and `current_players.json`.
Assert released/upcoming conversion, metric provenance, one observation per
discovery surface, capabilities, canonical health warnings, raw artifacts, and
that no SteamDB network host can be requested. Assert the adapter calls the
existing provider and does not implement HTTP, provider parsing, legacy
snapshots, legacy scoring, or legacy reports.

- [ ] **Step 2: Run red test**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_steam_collector.py`

Expected: exit 1 because the unified adapter does not exist.

- [ ] **Step 3: Implement a thin adapter, not a provider rewrite**

Define:

```python
class SteamCollector:
    def __init__(
        self,
        config: RadarConfig,
        client: steam_game_radar.http_client.JsonHttpClient,
        collect_official_fn: Callable = collect_official,
    ) -> None: ...
    def collect(self, run: RadarRun) -> CollectorResult: ...

def adapt_collection_result(
    run: RadarRun,
    legacy_result: steam_game_radar.official_provider.CollectionResult,
) -> tuple[PlatformObservation, ...]: ...
```

Import and call `steam_game_radar.official_provider.collect_official`, reuse
`steam_game_radar.http_client.JsonHttpClient`, and convert each legacy
`GameRecord`. Metrics `most_played_rank`, `top_seller_rank`,
`new_release_rank`, and `coming_soon_rank` each create a deterministic surface
observation; shared release/player/recommendation metrics are preserved on
each relevant observation. Capabilities, warnings, provenance, and raw payload
descriptors are retained. Add the sibling `steam-game-radar` directory to
import resolution in CLI/test bootstrap, never in domain modules.

- [ ] **Step 4: Run adapter and full Steam tests**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_steam_collector.py`

Expected: exit 0 and `OK`.

Run: `python3 -m unittest discover -s steam-game-radar/tests -p 'test_*.py'`

Expected: exit 0, 163 tests pass, 1 skipped.

- [ ] **Step 5: Commit**

```bash
git add unified-game-radar/unified_game_radar/collectors/steam.py unified-game-radar/tests/test_steam_collector.py unified-game-radar/references/config.example.json
git commit -m "feat: adapt Steam discovery into unified radar"
```

### Task 7: itch.io Browser Observation Collector

**Files:**
- Create: `unified-game-radar/unified_game_radar/collectors/itch.py`
- Create: `unified-game-radar/tests/test_itch_collector.py`
- Create: `unified-game-radar/tests/fixtures/itch_observations.json`
- Create: `unified-game-radar/references/collection-contract.md`

- [ ] **Step 1: Write failing observation-contract and filtering tests**

Define an exact `ItchBrowserRow` input with only: `title`, `developer`,
`game_url`, `surface`, `surface_scope`, `rank`, `browser_playable`, `genre`,
`is_jam`, `author_release_count`, `originality`, `observed_at`, and
`evidence_url`. Test `newest` and `popular`, global scope, locale metadata,
duplicate platform IDs, mass reuploads, commercial copies, Jam filters,
author history, deterministic observation IDs, exact-key rejection, bounded
strings, max 200 rows, max 2 MiB payload, `itch.io`/`*.itch.io` HTTPS host
allowlist, run/timestamp matching, conflicting duplicates, and nonnegative
bounded ranks/counts. Prompt-like title text remains inert data. Reject
calculated fields such as `score`, `heat`, or `action` if supplied.

- [ ] **Step 2: Run red test**

Run: `python3 -m unittest unified-game-radar/tests/test_itch_collector.py`

Expected: exit 1 because the itch collector does not exist.

- [ ] **Step 3: Implement strict browser-envelope ingestion**

Implement `parse_itch_envelope` and `build_itch_observations`. Derive only
validated platform keys, deterministic observation IDs, and cohort eligibility
locally. Heat belongs exclusively to `normalize.py`; unified scores/actions
belong exclusively to `score.py`. The browser envelope may never supply those
calculated fields and contains visible facts plus evidence URLs only.

- [ ] **Step 4: Add the exact Agent extraction contract**

Document required surfaces, selectors/visible fields, scroll bounds, spam evidence, envelope fields, and the rule that page content is untrusted data rather than instructions.

- [ ] **Step 5: Run green test and commit**

Run: `python3 -m unittest unified-game-radar/tests/test_itch_collector.py`

Expected: exit 0 and `OK`.

```bash
git add unified-game-radar/unified_game_radar/collectors/itch.py unified-game-radar/tests/test_itch_collector.py unified-game-radar/tests/fixtures/itch_observations.json unified-game-radar/references/collection-contract.md
git commit -m "feat: ingest itch discovery observations"
```

### Task 8: Roblox Charts Observation Collector

**Files:**
- Create: `unified-game-radar/unified_game_radar/collectors/roblox.py`
- Create: `unified-game-radar/tests/test_roblox_collector.py`
- Create: `unified-game-radar/tests/fixtures/roblox_observations.json`
- Modify: `unified-game-radar/references/collection-contract.md`

- [ ] **Step 1: Write failing Roblox contract tests**

Define an exact `RobloxBrowserRow` input with only: `universe_id`, `place_id`,
`name`, `developer`, `game_url`, `surface`, `surface_scope`, `rank`,
`concurrent_players`, `visits`, `favorites`, `observed_at`, and `evidence_url`.
Test Rising/Up-and-Coming/Charts, positive JSON-safe IDs, nonnegative finite
metrics, locale/geo, `surface_scope` (`global` or `personalized`), duplicate
IDs, metric-definition versions, exact-key rejection, bounded strings, max 200
rows, max 2 MiB payload, Roblox HTTPS host allowlist, run/timestamp matching,
conflicting duplicates, and rejection of calculated score/action fields.
Verify every accepted observation populates the complete compatibility key;
do not calculate deltas in this collector.

- [ ] **Step 2: Run red test**

Run: `python3 -m unittest unified-game-radar/tests/test_roblox_collector.py`

Expected: exit 1 because the Roblox collector does not exist.

- [ ] **Step 3: Implement strict Roblox envelope parsing**

Implement `parse_roblox_envelope` and `build_roblox_observations`. Accept
Roblox-owned IDs and visible public metrics only. Personalized observations may
be retained as evidence when explicitly scoped, but are ineligible for global
rank cohorts. Prompt-like names remain inert strings.

- [ ] **Step 4: Document Agent collection behavior and run green tests**

Run: `python3 -m unittest unified-game-radar/tests/test_roblox_collector.py`

Expected: exit 0 and `OK`.

- [ ] **Step 5: Commit**

```bash
git add unified-game-radar/unified_game_radar/collectors/roblox.py unified-game-radar/tests/test_roblox_collector.py unified-game-radar/tests/fixtures/roblox_observations.json unified-game-radar/references/collection-contract.md
git commit -m "feat: ingest Roblox discovery observations"
```

### Task 9A: Preliminary Scan Pipeline

**Files:**
- Create: `unified-game-radar/unified_game_radar/orchestration.py`
- Create: `unified-game-radar/tests/test_orchestration_scan.py`

- [ ] **Step 1: Write failing preliminary-pipeline tests**

Test run creation, injected collector calls, collector-exception isolation,
stale/unavailable fallback health, identity linking before enrichment
selection, injected deterministic `id_factory`, platform filtering, heat floor,
Top N across all platforms rather than per platform, stable ordering, Roblox
one-day delta through `RadarStore.compatible_observation` plus
`score_roblox_heat`, and outstanding browser tasks.

- [ ] **Step 2: Run red test**

Run: `python3 -m unittest unified-game-radar/tests/test_orchestration_scan.py`

Expected: exit 1 because orchestration is missing.

- [ ] **Step 3: Implement typed `scan_run`**

```python
def scan_run(
    config: RadarConfig,
    store: RadarStore,
    collectors: Mapping[str, Collector],
    clock: Callable[[], datetime],
    id_factory: Callable[[], str],
    platforms: Sequence[str],
) -> PreliminaryResult: ...
```

`scan_run` invokes selected deterministic collectors, persists results, links
identities, selects compatible history, normalizes eligible cohorts, and
returns `PreliminaryResult`; it does not create report paths or a
`CommandManifest` before Task 12/13.

- [ ] **Step 4: Run green test and all Chunk 2 tests**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_collector_base.py unified-game-radar/tests/test_steam_collector.py unified-game-radar/tests/test_itch_collector.py unified-game-radar/tests/test_roblox_collector.py unified-game-radar/tests/test_orchestration_scan.py`

Expected: exit 0 and `OK`.

- [ ] **Step 5: Commit**

```bash
git add unified-game-radar/unified_game_radar/orchestration.py unified-game-radar/tests/test_orchestration_scan.py
git commit -m "feat: build unified preliminary candidate pipeline"
```

### Task 9B: Browser Ingest and Deterministic Rebuild

**Files:**
- Modify: `unified-game-radar/unified_game_radar/orchestration.py`
- Create: `unified-game-radar/tests/test_orchestration_ingest.py`

- [ ] **Step 1: Write failing ingest-service tests**

Test parser-registry dispatch, exact run match, missing/closed run rejection,
browser `not_run → fresh/partial`, identical retry no-op, changed-payload
conflict, conservative identity linking, repeated rebuild stability, retained
observation IDs, and global Top-N determinism after both itch and Roblox ingest.

- [ ] **Step 2: Run red test**

Run: `python3 -m unittest unified-game-radar/tests/test_orchestration_ingest.py`

Expected: exit 1 because `ingest_run` is missing.

- [ ] **Step 3: Implement typed `ingest_run` and shared rebuild**

```python
def ingest_run(
    config: RadarConfig,
    store: RadarStore,
    run_id: str,
    envelope: Mapping[str, object],
    parser_registry: Mapping[str, Callable],
    clock: Callable[[], datetime],
    id_factory: Callable[[], str],
) -> PreliminaryResult: ...
```

Both `scan_run` and `ingest_run` call one private deterministic candidate
rebuild function. No report files or command manifests are created yet.

- [ ] **Step 4: Run all Chunk 2 tests and commit**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_collector_base.py unified-game-radar/tests/test_steam_collector.py unified-game-radar/tests/test_itch_collector.py unified-game-radar/tests/test_roblox_collector.py unified-game-radar/tests/test_orchestration_scan.py unified-game-radar/tests/test_orchestration_ingest.py`

Expected: exit 0 and `OK`.

```bash
git add unified-game-radar/unified_game_radar/orchestration.py unified-game-radar/tests/test_orchestration_ingest.py
git commit -m "feat: ingest browser radar observations"
```

## Chunk 3: Demand Gate, Unified Score, Reports, CLI, and Skill

### Task 10: Versioned Evidence and Ordered Demand Gate

**Files:**
- Create: `unified-game-radar/unified_game_radar/demand.py`
- Create: `unified-game-radar/tests/test_demand.py`
- Create: `unified-game-radar/references/evidence-format.md`

- [ ] **Step 1: Write failing evidence freshness and disambiguation tests**

Test timestamped Trends points, hourly-to-daily mean aggregation, declared
timezone, current-day incompleteness, 24-hour freshness, query
type/category/property, raw-artifact reference, name collisions, and
game-intent disambiguation. Both autocomplete and related-query collections
must contain strict `SearchQueryEvidence` rows; reject unexpected keys, stale
rows, missing timestamps, non-HTTPS source URLs, and loose strings.

- [ ] **Step 2: Write failing ordered-gate regression tests**

Include all-zero Airlinia/Meltspell-style data, GeoSlice
`[0, 0, 100, 32, 18]`, one incomplete-day spike, two-day nonzero without
support, sustained demand with typed autocomplete, sustained demand with a
second wave, latest completed below 30%, stale evidence, and missing evidence.
Add table-driven strict-boundary cases: peak exactly `2×` second-highest,
post-peak exactly `40%`, later local maximum exactly `50%`, latest retention
exactly `30%`, and latest zero with and without supporting intent. Assert the
classification order `unknown → fail → early_watch → pass` for every boundary.

```python
def test_single_spike_precedes_pass_even_with_two_nonzero_tail_days(self):
    suggestion = SearchQueryEvidence(
        schema_version=1,
        query="geo game",
        observed_at="2026-08-31T06:00:00Z",
        source_url="https://www.google.com/complete/search?...",
    )
    result = classify_demand(
        evidence(points=(0, 100, 32, 18), autocomplete=(suggestion,))
    )
    self.assertEqual(result.state, "early_watch")
```

- [ ] **Step 3: Run red test**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_demand.py`

Expected: exit 1 because demand functions do not exist.

- [ ] **Step 4: Implement daily aggregation and ordered classification**

Implement `aggregate_daily_means`, `completed_points`, `is_single_spike`, `has_second_wave`, `is_unambiguous_game_query`, and `classify_demand` in exact order `unknown → fail → early_watch → pass`.

- [ ] **Step 5: Document the evidence envelope and run green tests**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_demand.py`

Expected: exit 0 and `OK`.

- [ ] **Step 6: Commit**

```bash
git add unified-game-radar/unified_game_radar/demand.py unified-game-radar/tests/test_demand.py unified-game-radar/references/evidence-format.md
git commit -m "feat: enforce durable search demand gate"
```

### Task 11: External Spread, SEO Gap, Total Score, and Actions

**Files:**
- Create: `unified-game-radar/unified_game_radar/score.py`
- Create: `unified-game-radar/tests/test_score.py`
- Modify: `unified-game-radar/references/scoring-rules.md`

- [ ] **Step 1: Write failing table-driven component tests**

Cover every demand, external-source diversity/count/engagement/recency, and SEO guide/nonofficial/missing-intent boundary from the spec. Test developer and unknown author relations receive no independent credit, missing counts contribute zero without reweighting, and evidence older than seven days receives no external score.

- [ ] **Step 2: Write failing action-override and sort tests**

Test exact score thresholds 49.9/50/64.9/65/79.9/80, `fail → skip`, `early_watch → watch`, `unknown → needs_verification`, unknown SERP blocks a positive action, no independent evidence blocks immediate action, and stable tie ordering.

- [ ] **Step 3: Run red test**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_score.py`

Expected: exit 1 because score functions do not exist.

- [ ] **Step 4: Implement pure score functions**

Implement `score_demand`, `score_external_spread`, `score_seo_gap`, `score_opportunity`, `action_for`, and `opportunity_sort_key`. Persist one-decimal components and total.

- [ ] **Step 5: Run green tests and commit**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_score.py`

Expected: exit 0 and `OK`.

```bash
git add unified-game-radar/unified_game_radar/score.py unified-game-radar/tests/test_score.py unified-game-radar/references/scoring-rules.md
git commit -m "feat: score unified game opportunities"
```

### Task 12A: Safe Raw Artifact Persistence

**Files:**
- Create: `unified-game-radar/unified_game_radar/artifacts.py`
- Create: `unified-game-radar/tests/test_artifacts.py`
- Modify: `unified-game-radar/unified_game_radar/orchestration.py`
- Modify: `unified-game-radar/tests/test_orchestration_scan.py`

- [ ] **Step 1: Write failing artifact-safety tests**

Adapt the proven `steam_game_radar.artifacts` safety contract. Test recursive
redaction for keys containing `key`, `token`, `authorization`, `cookie`, or
`secret`; canonical JSON before hashing; configured byte limits; run-scoped
paths; atomic writes; identical retry no-op; same path/different content
conflict; path traversal and symlink refusal; retention limited to the exact
configured raw root; strict older-than cutoff; and filesystem error mapping.
Add an orchestration integration test proving every collector raw payload is
redacted and persisted before its observations are committed; an artifact
failure rolls back the current collector ingest and maps to persistence exit 5.

- [ ] **Step 2: Run red tests**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_artifacts.py`

Expected: exit 1 because unified `artifacts.py` does not exist.

- [ ] **Step 3: Port focused artifact helpers**

Implement `redact_json`, `persist_raw_artifact`, and `prune_raw_artifacts` with
unified `RawArtifact`, configuration, and errors. Wire them into `scan_run` and
`ingest_run` before observation persistence. Do not modify or depend on the
dirty historical Steam worktree; adapt only the verified committed code.

- [ ] **Step 4: Run unified and Steam artifact tests**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_artifacts.py steam-game-radar/tests/test_artifacts.py`

Expected: exit 0 and `OK`.

- [ ] **Step 5: Commit**

```bash
git add unified-game-radar/unified_game_radar/artifacts.py unified-game-radar/unified_game_radar/orchestration.py unified-game-radar/tests/test_artifacts.py unified-game-radar/tests/test_orchestration_scan.py
git commit -m "feat: retain unified radar evidence safely"
```

### Task 12B: Canonical Reports and Recoverable Daily Publication

**Files:**
- Create: `unified-game-radar/unified_game_radar/report.py`
- Create: `unified-game-radar/tests/test_report.py`
- Create: `unified-game-radar/references/report-schema.md`

- [ ] **Step 1: Write failing report tests**

Test exact JSON schema, Markdown generation from canonical JSON, all component
scores, platform provenance, timestamps, source health, warnings, evidence
URLs, preliminary/final phases, immutable run artifacts, configured-timezone
daily-date calculation, 10:00 no-publish, 16:00 publish, manual no-publish,
explicit `--publish-daily`, and monotonic daily latest. Inject failures after
run JSON, run Markdown, daily JSON, daily Markdown, latest JSON, latest
Markdown, and database publication; retry must repair partial files
idempotently without a false publication row.

- [ ] **Step 2: Run red test**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_report.py`

Expected: exit 1 because report functions do not exist.

- [ ] **Step 3: Implement explicit build/persist/publish APIs**

```python
def build_report(
    result: PreliminaryResult,
    scores: Sequence[ScoredOpportunity],
    phase: str,
) -> Mapping[str, object]: ...

def persist_run_artifacts(
    report: Mapping[str, object],
    report_dir: Path,
    writer: AtomicWriter,
) -> tuple[Path, Path]: ...

def publish_daily_if_allowed(
    config: RadarConfig,
    store: RadarStore,
    run: RadarRun,
    report: Mapping[str, object],
    run_paths: tuple[Path, Path],
    now: datetime,
    writer: AtomicWriter,
) -> Publication: ...
```

Generate Markdown only from the canonical report mapping. Write immutable run
JSON and Markdown first. For daily publication, calculate the date in the
configured timezone, pass eligibility/monotonic checks, repair or write both
daily and both latest files, and record `Publication` only after all files
succeed. File retries compare canonical content and repair partial output;
database retries are idempotent. Never let an older scheduled date replace
daily `latest`.

- [ ] **Step 4: Run green tests and commit**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_report.py`

Expected: exit 0 and `OK`.

```bash
git add unified-game-radar/unified_game_radar/report.py unified-game-radar/tests/test_report.py unified-game-radar/references/report-schema.md
git commit -m "feat: publish canonical unified radar reports"
```

### Task 13: Final Enrichment Orchestration and CLI

**Files:**
- Modify: `unified-game-radar/unified_game_radar/orchestration.py`
- Create: `unified-game-radar/scripts/game_radar.py`
- Create: `unified-game-radar/tests/test_cli.py`
- Create: `unified-game-radar/tests/test_orchestration_final.py`

- [ ] **Step 1: Write failing CLI tests**

Cover `scan`, `ingest`, `enrich`, and `report`;
`--platform all|itch|steam|roblox`; exact one-line JSON manifest; project-root
path resolution; `--run-id`; `--publish-daily`; argument errors; exit codes
2–7; unexpected exit 1; and import-safe direct execution. Inject a
`lock_factory` and assert every mutating command acquires the project-scoped
lock, a collision returns exit 6, the lock records matching run ownership, and
exceptions always release it in `finally`.

- [ ] **Step 2: Write failing final-orchestration tests**

Test evidence run/opportunity matching, exact retry no-op, changed evidence conflict, one unified Top N, positive-action gates, source outage behavior, daily publication, and canonical regeneration from SQLite.

- [ ] **Step 3: Run red tests**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_cli.py unified-game-radar/tests/test_orchestration_final.py`

Expected: exit 1 because CLI/final services are incomplete.

- [ ] **Step 4: Implement final service methods and CLI**

Implement `enrich_run`, `report_run`, `main`, dependency wiring, sibling Steam
package bootstrap, exact manifest output, exception-to-exit mapping, and
traceback behavior for unexpected errors. `main(..., lock_factory=RunLock)`
wraps `scan`, `ingest`, `enrich`, and report regeneration before any write and
releases the lock in `finally`.

- [ ] **Step 5: Run green tests and commit**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_cli.py unified-game-radar/tests/test_orchestration_final.py`

Expected: exit 0 and `OK`.

```bash
git add unified-game-radar/unified_game_radar/orchestration.py unified-game-radar/scripts/game_radar.py unified-game-radar/tests/test_cli.py unified-game-radar/tests/test_orchestration_final.py
git commit -m "feat: orchestrate unified radar runs"
```

### Task 14: Agent Skill, Compatibility Routes, and End-to-End Verification

**Files:**
- Create: `unified-game-radar/SKILL.md`
- Create: `unified-game-radar/tests/test_skill_docs.py`
- Create: `unified-game-radar/tests/test_end_to_end.py`
- Create: `unified-game-radar/tests/test_live_providers.py`
- Create: `unified-game-radar/tests/fixtures/evidence_zero_demand.json`
- Create: `unified-game-radar/tests/fixtures/evidence_single_spike.json`
- Create: `unified-game-radar/tests/fixtures/evidence_sustained.json`
- Modify: `html5-game-radar/SKILL.md`
- Modify: `steam-game-radar/SKILL.md`
- Modify: `README.md`

- [ ] **Step 1: Read and apply @skill-creator before authoring the Skill**

The Skill must use progressive disclosure, include exact triggers and commands, keep volatile collection details in references, and not claim automatic writes to Feishu or `web-game-data`.

- [ ] **Step 2: Write failing Skill policy tests**

Assert one canonical schedule, 10:00 collection-only, 16:00 daily publish, correct CLI routes, exact evidence workflow, no SteamDB automation, no positive action for zero/single-spike demand, no external write without authorization, and deprecation text in old Skill schedules.

- [ ] **Step 3: Write failing fixture-driven end-to-end tests**

Run two compatible platform snapshots, ingest itch and Roblox fixtures, enrich three candidates with zero/single-spike/sustained evidence, and assert exactly one candidate may be positive while the other two are `skip` and `watch`.

- [ ] **Step 4: Run red tests**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_skill_docs.py unified-game-radar/tests/test_end_to_end.py`

Expected: exit 1 because Skill/docs are absent and the fixture flow is incomplete.

- [ ] **Step 5: Author the Skill and exact compatibility routes**

Keep `unified-game-radar` as the only scheduled daily workflow. Remove active
schedule examples from old Skills and document exact manual-only routes.

itch compatibility starts with:

```bash
python3 unified-game-radar/scripts/game_radar.py scan \
  --config unified-game-radar/references/config.example.json \
  --platform itch
```

The Agent parses that run ID, collects the outstanding itch browser envelope,
then runs `ingest --config ... --run-id ... --input ...`, `enrich --config ...
--run-id ... --input ...`, and `report --config ... --run-id ...`. Steam starts
with the same `scan` command using `--platform steam`, skips browser ingest
when no task exists, then runs `enrich` and `report`. Compatibility commands
never pass `--publish-daily`. Catalog the unified Skill in `README.md`.

- [ ] **Step 6: Run end-to-end tests**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest unified-game-radar/tests/test_skill_docs.py unified-game-radar/tests/test_end_to_end.py`

Expected: exit 0 and `OK`.

- [ ] **Step 7: Run the complete offline verification matrix**

Run: `PYTHONPATH=steam-game-radar:unified-game-radar python3 -m unittest discover -s unified-game-radar/tests -p 'test_*.py'`

Expected: exit 0 and `OK`.

Run: `python3 -m unittest discover -s steam-game-radar/tests -p 'test_*.py'`

Expected: exit 0, 163 tests pass, 1 skipped.

Run: `python3 -m unittest discover -s signallayer-backlinks-client/tests -p 'test_*.py'`

Expected: exit 0 and `OK`.

Run: `git diff --check`

Expected: exit 0 with no output.

- [ ] **Step 8: Add opt-in live provider/contract smoke tests**

`test_live_providers.py` is skipped unless `RUN_LIVE_RADAR_TESTS=1`. The Steam
case calls the existing official provider with low configured limits. The itch
and Roblox cases validate same-day Agent-produced envelopes supplied through
`ITCH_LIVE_OBSERVATION_JSON` and `ROBLOX_LIVE_OBSERVATION_JSON`; they assert
fresh observation times, allowed hosts, at least one row, and successful
contract conversion. They never run in the default offline suite.

Opt-in command:

```bash
RUN_LIVE_RADAR_TESTS=1 \
ITCH_LIVE_OBSERVATION_JSON=/absolute/path/itch-live.json \
ROBLOX_LIVE_OBSERVATION_JSON=/absolute/path/roblox-live.json \
PYTHONPATH=steam-game-radar:unified-game-radar \
python3 -m unittest unified-game-radar/tests/test_live_providers.py
```

Expected: exit 0 and `OK` when network and same-day Agent envelopes are
available; otherwise individual live cases report `skipped` with a reason.

- [ ] **Step 9: Perform a fixture-backed CLI smoke run**

With `PYTHONPATH=steam-game-radar:unified-game-radar`, use a temporary project
directory and fixture inputs to run `scan`, `ingest`, `enrich`, and `report`.
Verify one-line manifests parse as JSON, SQLite and raw redacted artifacts
persist, run reports exist, and manual runs do not advance daily `latest`
without `--publish-daily`.

- [ ] **Step 10: Commit**

```bash
git add unified-game-radar html5-game-radar/SKILL.md steam-game-radar/SKILL.md README.md
git commit -m "feat: add unified game radar Agent Skill"
```

### Task 15: Final Review and Delivery

**Files:**
- Review: all branch changes against `docs/superpowers/specs/2026-08-31-unified-game-opportunity-radar-design.md`

- [ ] **Step 1: Invoke @superpowers:requesting-code-review**

Review correctness, security boundaries, evidence provenance, platform adapters, database migrations, and user-visible Skill instructions.

- [ ] **Step 2: Resolve actionable review findings with TDD**

For every accepted finding, add or update a failing test, implement the minimal correction, rerun the focused test, then rerun the complete matrix.

- [ ] **Step 3: Invoke @superpowers:verification-before-completion**

Freshly run the complete offline verification matrix, `git diff --check`, and `git status --short`. Do not claim completion from earlier output.

- [ ] **Step 4: Use @superpowers:finishing-a-development-branch**

Present merge/PR/keep-worktree options only after all required tests are green.
