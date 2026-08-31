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
  Run warnings have no opportunity owner; source warnings have no opportunity
  owner and may name only their own collector.

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
`observed_at` pairs from same-run source health for every platform. A platform
without same-run source health is invalid. Candidate warnings are the typed
warnings attached to its score and each warning must own that candidate's
exact `opportunity_id`.

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
text for Markdown and HTML. Evidence URLs are HTML-escaped code text rather
than raw Markdown autolinks. Markdown displays:

- run ID, phase, and generated timestamp;
- every source status, observation timestamp, and source warning;
- ranked candidates, demand/action, all four components and total;
- platform keys, evidence timestamps, evidence URLs, and candidate warnings;
- run warnings and outstanding Agent tasks.

## Files and publication

Final run files are immutable at fixed paths:

```text
{report_dir}/{run_id}.final.json
{report_dir}/{run_id}.final.md
```

Preliminary collection may evolve several times within one run. Every version
therefore has a content-addressed immutable snapshot. The lowercase SHA-256 is
computed from the canonical JSON bytes and is shared by its JSON/Markdown pair:

```text
{report_dir}/{run_id}.preliminary.{canonical_json_sha256}.json
{report_dir}/{run_id}.preliminary.{canonical_json_sha256}.md
{report_dir}/{run_id}.preliminary.latest.json
{report_dir}/{run_id}.preliminary.latest.md
{report_dir}/{run_id}.preliminary.revisions.json
```

The two `preliminary.latest` files are a recoverable current view; all hashed
snapshots remain immutable. The revisions sidecar contains only the run ID,
content hashes, their consecutive first-persistence revision numbers, and the
current hash/revision. It never embeds the canonical report payload. Updates
are serialized by a run-scoped flock opened relative to the anchored report
directory. A new hash advances the sidecar before the latest pair is written,
so a retry repairs a partial latest pair. A retry of a known older hash repairs
only its immutable snapshot and never rolls latest backward. Identical current
retries are no-ops. Reusing a final or hashed snapshot path with different
content is a conflict.

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
timestamps and never moves to an older run or scheduled date. Every SQLite
`Publication.report_json` and `report_markdown` points to the run's immutable
snapshot, never to mutable dated/latest views; later same-date publication
therefore cannot invalidate an earlier audit record.

Final persistence order is run JSON, run Markdown, dated JSON, dated Markdown,
latest JSON, latest Markdown, then the SQLite `Publication`. Preliminary order
is hashed JSON, hashed Markdown, revision sidecar (for a first-seen hash),
preliminary-latest JSON, then preliminary-latest Markdown. Each file uses an
atomic sibling replacement and exact-content comparison. The configured root and
each child directory are opened with `O_NOFOLLOW`; reads, lock creation,
temporary creation, fsync, and atomic replacement use those directory handles
rather than re-resolving pathnames. Replacing the visible root or `daily`
pathname during a write therefore cannot redirect bytes outside the originally
anchored report tree. A retry accepts already-correct files and repairs missing
or incomplete mutable pairs. A retry of an already-recorded historical
publication returns its original record without rolling back a newer daily
view. The database write runs in a transaction only after every required file
succeeds; therefore a file-stage failure cannot create a false publication row.
