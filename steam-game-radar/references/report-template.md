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
