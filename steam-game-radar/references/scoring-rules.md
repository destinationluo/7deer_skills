# Steam Game Radar scoring rules

All piecewise transforms linearly interpolate between points, clamp outside
the listed range, and persist one decimal place. Weighted averages divide by
available configured weight only after the relevant minimum gate passes.

## Released Steam heat

| Metric | Weight | Transform |
|---|---:|---|
| Player growth | 25 | `(0,0)`, `(5,25)`, `(15,50)`, `(30,75)`, `(60,100)` |
| Current player scale | 10 | `(0,0)`, `(100,20)`, `(1000,50)`, `(10000,80)`, `(100000,100)` |
| Rank improvement | 15 | `(0,0)`, `(5,40)`, `(20,70)`, `(50,100)` |
| Release recency | 10 | age 0-7: 100; 8-30: 70; 31-90: 40; older: 0 |

Player growth uses the larger available positive 1d/7d percentage; negative
growth scores zero and requires a current-player observation. Rank precedence
is 7d same-source -> 1d same-source -> provider previous_rank - current_rank.
The gate is at least 2 metrics and at least 25 configured weight.

## Unreleased Steam heat

| Metric | Weight | Transform |
|---|---:|---|
| Upcoming rank improvement | 20 | `(0,0)`, `(5,40)`, `(20,70)`, `(50,100)` |
| Wishlist/follower 7d gain | 20 | `(0,0)`, `(100,20)`, `(1000,60)`, `(5000,85)`, `(20000,100)` |
| Release proximity | 10 | 0-14 days: 100; 15-30: 80; 31-90: 60; 91-180: 30; later/TBA: 0 |
| Coming-soon visibility | 10 | rank 1: 100; 2-5: 80; 6-20: 50; 21-50: 20; lower/unranked: 0 |

Upcoming rank precedence is 7d same-source -> 1d same-source; provider previous_rank is not allowed. Gain uses the larger available non-negative
wishlist/follower value. The gate is at least 2 metrics and at least 30
configured weight.

Missing or TBA `release_date` contributes `release_proximity = 0` at weight 10.
Missing or unranked `coming_soon_rank` contributes `coming_soon_visibility = 0` at weight 10.
Both zero-valued signals count toward metric count and available weight.
Malformed or non-Steam observations remain omitted rather than becoming an
observed zero.

## SEO opportunity

| Metric | Weight | Transform |
|---|---:|---|
| Google competition gap | 20 | validated 0-100 |
| Expandable query count | 10 | `min(query_count / 20 * 100, 100)` |
| YouTube/Reddit cross-signal | 10 | `(0,0)`, `(1,20)`, `(3,50)`, `(10,80)`, `(25,100)` |

Each supplied YouTube and Reddit count is transformed separately, then the
available sources are averaged. SEO requires Google competition gap plus at
least one other SEO metric, for at least 30 configured weight. An empty
expandable_queries is an observed zero. Valid HTTPS typed evidence is
mandatory: Google evidence always, plus matching YouTube/Reddit evidence for
each supplied signal.

## Enrichment file contract

Write this local file from evidence gathered for the preliminary manifest's
authoritative AppID list:

```json
{
  "schema_version": 1,
  "run_id": "20260824T030000Z-a1b2c3d4",
  "observed_at": "2026-08-24T03:20:00Z",
  "games": [
    {
      "appid": 123456,
      "google_competition_gap_score": 80,
      "expandable_queries": [
        "example game wiki",
        "example game guide"
      ],
      "youtube_relevant_7d": 6,
      "reddit_relevant_7d": 4,
      "reddit_upvotes_7d": 240,
      "evidence": [
        {
          "source": "google",
          "url": "https://www.google.com/search?q=example+game"
        },
        {
          "source": "youtube",
          "url": "https://www.youtube.com/results?search_query=example+game"
        },
        {
          "source": "reddit",
          "url": "https://www.reddit.com/search/?q=example+game"
        }
      ]
    }
  ]
}
```

The envelope is exact: `schema_version` is 1, `observed_at` is UTC, `games` is
an array with unique positive AppIDs, and the file `run_id` must exactly match
the preliminary manifest `run_id`. `google_competition_gap_score` is an integer
from 0 through 100. `expandable_queries` is an array of non-empty strings.
YouTube/Reddit counts are null or non-negative integers. Google evidence is
required; supplied YouTube or Reddit signals require source-matching evidence.
Evidence `source` is only `google`, `youtube`, or `reddit`. Typed evidence URLs
must use HTTPS. Evidence URLs must use HTTPS and reject credentials/userinfo,
fragments, whitespace, and backslashes. An explicit port is allowed only when
it is exactly 443. The loader rejects unknown fields, duplicate AppIDs,
invalid types, and mismatched run IDs without making network requests.

## Combination, actions, and confidence

The exact combination is
`final_score = 0.60 * steam_heat_score + 0.40 * seo_opportunity_score`.
Preliminary heat has no final score and uses `needs_seo_enrichment`. Failed
Steam gates use `insufficient_data`.

Action is selected from exact pre-round composite hundredths before final_score
is persisted half-up to one decimal. Therefore a displayed score can cross a
label boundary without changing the action: Steam heat 50.0 and SEO 49.9 give
49.96 -> persisted 50.0 -> `skip`; Steam heat 65.0 and SEO 64.9 give 64.96 ->
persisted 65.0 -> `watch`.

| Exact pre-round composite interval | Action |
|---|---|
| 80.00-100.00 | `immediate_action` |
| 65.00-79.99 | `worth_positioning` |
| 50.00-64.99 | `watch` |
| 0.00-49.99 | `skip` |

- confidence A: official values + valid history + manual SteamDB confirmation
  + valid SEO/community enrichment.
- confidence B: official or manual SteamDB values + valid history + valid
  SEO/community enrichment.
- confidence C: baseline, missing history, or incomplete enrichment. C never
  receives a final score/action and remains preliminary or insufficient.

## Stable ordering

1. Final score descending when present, otherwise Steam heat descending.
2. Confidence A, B, C.
3. Current players descending for released; wishlist/follower gain descending
   for unreleased.
4. Case-folded name ascending.
5. AppID ascending.
