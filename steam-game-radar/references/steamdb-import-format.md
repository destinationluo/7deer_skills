# SteamDB manual import format

SteamDB states that it has no public API and disallows automated scraping and
crawling. Never request, never browse, never crawl, never scrape, and never
refresh steamdb.info automatically. This importer reads only a local CSV/JSON
export explicitly supplied by a human; the file limit is 5 MiB.
Policy source: <https://steamdb.info/faq/>.

## Views and shapes

Allowed views are `trending_games`, `wishlist_activity`,
`trending_followers`, and `recent_releases`.

- CSV requires an explicit `--view`.
- JSON accepts a row array with explicit `--view`, or the wrapper
  `{ "schema_version": 1, "view": "...", "rows": [...] }`.
- A wrapper view must match an explicitly supplied view.

Every row requires a name plus either numeric `appid` or a URL containing
`/app/{appid}`. Name-only matching is rejected.

## Canonical fields and aliases

| Canonical field | Accepted headers |
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

Unknown columns are preserved under `source_extra`; reserved `steamdb_view`
must agree with the selected view.

## Per-view requirements

| View | Required after AppID/URL and name |
|---|---|
| `trending_games` | `rank` or `current_players` |
| `wishlist_activity` | `wishlist_gain_7d` or `follower_gain_7d` |
| `trending_followers` | `follower_gain_7d`; `followers` when available |
| `recent_releases` | `release_date`, plus `current_players` or `peak_players` |

## Normalization and rejection

- Numbers accept commas, a leading plus, and K or M suffixes; counts and ranks
  must resolve to integers.
- Percentages accept `91.2` or `91.2%` and stay within 0-100.
- Dates accept `YYYY-MM-DD` or `DD Mon YYYY`; dates are interpreted in UTC.
- Empty values or an em dash mean missing.
- Duplicate AppIDs are rejected as ambiguous rather than merged.
- Conflicting aliases, invalid AppIDs/URLs, or a row missing its view contract
  produce a stable row rejection. Invalid rows are reported while valid rows
  continue through the pipeline.

Imported observations use the run start time and source kind
`steamdb_manual_import`. A valid import merges with an official snapshot no
older than 72 hours; otherwise it forms a manual-only confidence-C baseline
without growth claims or a final action.
