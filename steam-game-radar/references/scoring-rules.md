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

## Combination, actions, and confidence

The exact combination is
`final_score = 0.60 * steam_heat_score + 0.40 * seo_opportunity_score`.
Preliminary heat has no final score and uses `needs_seo_enrichment`. Failed
Steam gates use `insufficient_data`.

| Exact final-score interval | Action |
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
