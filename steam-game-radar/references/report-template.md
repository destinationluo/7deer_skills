# Steam Game Radar report template

`steam_game_radar.report.render_markdown()` renders this logical structure from
the canonical JSON report. This file documents the view; it is not a second
data model and must not be used to re-sort candidates or recompute scores.

```text
# Steam Game Radar

- Run ID: {run_id}
- Phase: {phase}
- Mode: {mode}
- Generated at: {generated_at}
- Data status: {data_status}

## Released
### {rank}. {name} (AppID {appid})
- identity and store URL
- confidence, action, and persisted scores
- observed metrics with source_id, source_kind, and observed_at
- persisted deltas and component scores
- typed evidence URLs and recommended content types
- warnings

## Unreleased
{same candidate fields, preserving the canonical JSON array order}

## Newly observed
{AppIDs in canonical order}

## Run warnings
{stable warning code and message}

## Rejected rows
{row number, stable code, and message}
```

The paired files are `{run_id}.{phase}.json` and `{run_id}.{phase}.md`.
Timestamped pairs are immutable. `latest.json` and `latest.md` advance only for
a newer run timestamp or for a final phase replacing the same run's
preliminary phase.

## Canonical top-level fields

The JSON object contains exactly these fields:

- `report_schema_version`
- `run_id`
- `phase`
- `mode`
- `generated_at`
- `data_status`
- `released`
- `unreleased`
- `newly_observed`
- `warnings`
- `rejected_rows`

`phase` is `preliminary` or `final`; `mode` is `official_scan`,
`official_plus_manual`, or `manual_baseline`; `data_status` is `fresh`,
`stale`, or `manual_only`. `newly_observed` is a sorted unique AppID array.
Run warnings use `code`, `message`, and optional `appid`. Rejected rows use
`row_number`, `code`, `message`, and optional `appid`.

## Canonical candidate fields

Every object in `released` or `unreleased` contains exactly:

- `appid`
- `name`
- `release_status`
- `store_url`
- `observed_metrics`
- `deltas`
- `metric_scores`
- `steam_heat_score`
- `seo_opportunity_score`
- `final_score`
- `action`
- `confidence`
- `warnings`
- `evidence`
- `recommended_content_types`

Each `observed_metrics` value contains `value`, `source_id`, `source_kind`, and
`observed_at`. Evidence entries contain `source` and `url`. Candidate warnings
use the run-warning shape. Nullable scores remain JSON `null`; Markdown does not re-sort candidates or recompute scores from the canonical JSON.
