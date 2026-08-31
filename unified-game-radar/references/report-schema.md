# Unified Game Radar Report Schema

The canonical JSON mapping is the sole version-1 report model. Markdown is a
deterministic view rendered from this mapping in array order; it does not
recalculate scores, actions, ranks, source health, or evidence.

## Top-level object

The object has exactly these eight keys:

```json
{
  "schema_version": 1,
  "run_id": "20260831T020000Z-a1b2c3d4",
  "phase": "final",
  "generated_at": "2026-08-31T02:00:00Z",
  "candidates": [],
  "source_health": [],
  "warnings": [],
  "outstanding_tasks": []
}
```

- `schema_version` is exactly `1`.
- `phase` is `preliminary` or `final`.
- `generated_at` is the canonical UTC timestamp encoded by `run_id`. This
  makes reconstruction deterministic; freshness timestamps remain on source
  health and evidence provenance.
- `source_health`, `warnings`, and `outstanding_tasks` contain the exact
  version-1 schema records. Source health is ordered itch, Steam, Roblox.

## Candidate object

Every candidate has exactly these fourteen keys:

```json
{
  "rank": 1,
  "opportunity_id": "0f840f6f-5c62-4ca6-9d53-e0be9ab2740b",
  "name": "Signal Garden",
  "normalized_name": "signal garden",
  "developer": "Example Studio",
  "official_domain": "example.com",
  "platforms": [],
  "evidence_timestamps": [],
  "evidence_urls": [],
  "demand_state": "pass",
  "component_scores": {
    "platform": 24.0,
    "demand": 26.0,
    "external": 14.0,
    "seo": 16.0
  },
  "total_score": 80.0,
  "action": "immediate_action",
  "warnings": []
}
```

`platforms` contains complete `PlatformRecord` objects in itch, Steam, Roblox
order and then platform-ID order. `evidence_urls` is the matching deduplicated
platform URL list. `evidence_timestamps` contains exact `collector` and
`observed_at` pairs from same-run source health for those platforms. Candidate
warnings are the typed warnings attached to its score.

Candidates with scores use the stable unified opportunity sort and consecutive
one-based ranks. A preliminary report may retain an unscored candidate; its
`component_scores`, `demand_state`, `total_score`, and `action` are all `null`
and its score-warning array is empty. A final report requires exactly one
`ScoredOpportunity` for every candidate. Component scores and totals remain
one-decimal schema values and the total must equal their sum.

## Canonical serialization and Markdown

JSON is UTF-8, finite JSON-native data with sorted object keys, compact
separators, unescaped Unicode, and one trailing newline. Array order is
semantic and must not be resorted during rendering. Markdown escapes visible
text and displays:

- run ID, phase, and generated timestamp;
- every source status, observation timestamp, and source warning;
- ranked candidates, demand/action, all four components and total;
- platform keys, evidence timestamps, evidence URLs, and candidate warnings;
- run warnings and outstanding Agent tasks.

## Files and publication

Run files are immutable:

```text
{report_dir}/{run_id}.{phase}.json
{report_dir}/{run_id}.{phase}.md
```

An identical retry is a no-op. Reusing a run/phase path with different content
is a conflict.

The scheduled collection at 10:00 in the configured timezone records only the
run pair. A final scheduled run at the configured daily publish hour (16:00 by
default), or a manual final run with explicit `publish_daily`, may write:

```text
{report_dir}/daily/YYYY-MM-DD.json
{report_dir}/daily/YYYY-MM-DD.md
{report_dir}/daily/latest.json
{report_dir}/daily/latest.md
```

The daily date comes from the run start in the configured timezone, not retry
time. A preliminary report never advances daily output. `latest` compares run
timestamps and never moves to an older run or scheduled date.

Persistence order is run JSON, run Markdown, dated JSON, dated Markdown,
latest JSON, latest Markdown, then the SQLite `Publication`. Each file uses an
atomic sibling replacement and exact-content comparison. A retry accepts
already-correct files and repairs missing or incomplete mutable pairs. The
database write runs in a transaction only after every required file succeeds;
therefore a file-stage failure cannot create a false publication row.
