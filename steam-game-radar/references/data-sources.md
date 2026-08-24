# Steam Game Radar data sources

Version 1 uses no authentication and no API key. Automated HTTP is HTTPS-only,
allows only `api.steampowered.com` and `store.steampowered.com`, rejects
userinfo and every explicit port, including `:443`, and has redirects disabled.

## Official provider matrix

| Capability | Exact endpoint | Candidate use |
|---|---|---|
| `most_played` | `https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/` | released chart rank, previous rank, peak players |
| `current_players` | `https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={appid}` | retained released candidates only |
| `featured_categories` | `https://store.steampowered.com/api/featuredcategories?cc={country}&l={language}` | top sellers, new releases, coming soon |
| `appdetails` | `https://store.steampowered.com/api/appdetails?appids={appid}&cc={country}&l={language}` | identity, app type, release state/date, genres |

The two discovery capabilities run first. Released candidates are ordered by
most-played, then top sellers, then new releases; unreleased candidates come
from coming soon. Dedupe preserves source ranks. Candidate caps default to 50
released and 50 unreleased before per-AppID requests. Only base games survive
the app-details type filter.

## Transport limits

- Minimum request interval: 1.0 second.
- Per-attempt timeout: 15 seconds.
- Retries apply only to timeouts, HTTP 429, and HTTP 5xx, with exponential
  backoff; `max_retries=3` means 4 total attempts (the first plus 3 retries).
- Response/raw-provider cap: 5 MiB.
- Fixed header:
  `User-Agent: 7deer-steam-game-radar/1 (+https://github.com/destinationluo/7deer_skills)`.
  The client sends no credentials, cookies, authorization, API keys, or other
  secret headers.
- Redirects are never followed, even when `Location` points elsewhere.

Every metric records `source_id`, `source_kind`, and `observed_at`. A missing capability produces a stable warning and permits fallback; it never fabricates partial values. The remaining capabilities continue when useful.
If an official run is unusable, the orchestrator may fallback to the newest
valid official snapshot. Age over 36 hours produces a stale warning. Official
fallback is allowed through 72 hours inclusive; beyond 72 hours, provider
failure exits with code 3.

SteamDB is not an automated provider. See `steamdb-import-format.md` for the
human-supplied local-file contract.
