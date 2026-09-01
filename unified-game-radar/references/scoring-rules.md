# Platform Heat and Cohort Normalization

This document defines the version-1 platform-heat contract implemented by
`unified_game_radar.normalize`. Platform heat measures discovery acceleration
inside one platform. It is not search demand and cannot by itself produce a
"worth doing" action.

## Input contract

The normalization module accepts frozen, typed fact records, not browser JSON
or database rows:

- `ItchHeatInput`
- `SteamReleasedHeatInput`
- `SteamUpcomingHeatInput`
- `RobloxHeatInput`

Collectors and orchestration must validate source data before constructing
these records. Each record carries a canonical `run_id`, `platform_key`, and
one or more source `observation_ids`. Delta fields have explicit compatibility
flags. A value does not earn growth points unless both the value and its
compatibility flag are present. Steam upcoming follower/wishlist growth also
requires `growth_verified=True`, meaning it came from an allowed verified
import.

Growth values use percentage points: `100` means 100% growth, `50` means 50%,
and any finite value strictly greater than `0` satisfies a `>0%` rule.

Missing facts (`None`) contribute zero. Scores are never reweighted to make up
for a missing component. A first observation has no compatible prior value and
therefore earns no delta or growth points. History is compatible only when the
provider, surface, geo, locale, normalized query parameters, and metric
definition version match the current observation. Intraday history cannot be
presented as a one-day delta.

## Compatible cohort labels

`PlatformHeat.surface` is a normalization cohort label, not necessarily the
raw source surface embedded in an observation ID:

- `itch_discovery`
- `steam_released`
- `steam_upcoming`
- `roblox_global`
- `roblox_personalized`

Raw discovery surfaces remain visible through every retained observation ID.
Steam released and upcoming records never share a cohort. Roblox global and
personalized chart observations never share a cohort. Platforms never share a
cohort.

## itch.io heat

| Component | Verified fact | Points |
|---|---:|---:|
| First-seen age | <=24 hours | 25 |
| | <=72 hours | 15 |
| | <=168 hours | 5 |
| | older or missing | 0 |
| Popular rank | <=10 | 35 |
| | <=25 | 25 |
| | <=50 | 15 |
| | lower or missing | 0 |
| Compatible popular-rank improvement | >=20 places | 20 |
| | 5-19 | 10 |
| | 1-4 | 5 |
| | <=0, missing, or incompatible | 0 |
| Originality | verified original | 10 |
| | unknown | 5 |
| | missing | 0 |
| Browser playable | verified true | 5 |
| Author history | non-spam author with >=2 prior original releases | 5 |

A known reupload is excluded, even if other facts would score positively.
`collector_eligible=False` also excludes an entry; collectors use that flag
for Jam-only entries, copied commercial games, mass reuploads, and equivalent
filters. Excluded rows return no `PlatformHeat`, so filtered facts cannot
become positive later.

## Steam released heat

| Component | Verified fact | Points |
|---|---:|---:|
| Official rank | <=10 | 25 |
| | <=25 | 20 |
| | <=50 | 15 |
| | <=100 | 8 |
| Compatible rank improvement | >=20 places | 25 |
| | 10-19 | 18 |
| | 5-9 | 10 |
| | 1-4 | 5 |
| One-day current-player growth | >=100% | 25 |
| | >=50% | 20 |
| | >=20% | 12 |
| | >0% | 5 |
| Current players | >=10,000 | 15 |
| | >=1,000 | 10 |
| | >=100 | 5 |
| Release age | <=7 days | 10 |
| | <=30 days | 5 |

Every unspecified, missing, nonpositive, or incompatible case earns zero.

## Steam upcoming heat

| Component | Verified fact | Points |
|---|---:|---:|
| Coming-soon rank | <=10 | 30 |
| | <=25 | 24 |
| | <=50 | 15 |
| Compatible rank improvement | >=20 places | 30 |
| | 10-19 | 20 |
| | 1-9 | 10 |
| Verified follower/wishlist growth | >=50% | 20 |
| | >=20% | 15 |
| | >0% | 8 |
| Release proximity | 0-7 days | 10 |
| | >7 and <=30 days | 7 |
| | >30 days | 3 |
| Same-run Steam-owned discovery presence | >=2 surfaces | 10 |

Past or unknown release dates earn zero proximity points. Unverified,
missing, first-observation, or incompatible follower/wishlist growth earns
zero.

## Roblox heat

| Component | Verified fact | Points |
|---|---:|---:|
| Chart rank | <=10 | 30 |
| | <=25 | 24 |
| | <=50 | 15 |
| Compatible one-day rank improvement | >=20 places | 30 |
| | 10-19 | 20 |
| | 1-9 | 10 |
| One-day concurrent-player growth | >=100% | 25 |
| | >=50% | 20 |
| | >=20% | 12 |
| | >0% | 5 |
| Concurrent players | >=10,000 | 10 |
| | >=1,000 | 7 |
| | >=100 | 3 |
| Consecutive compatible chart appearances | >=3 | 5 |
| | exactly 2 | 3 |

Every unspecified, missing, nonpositive, or incompatible case earns zero.
The input explicitly selects `roblox_global` or `roblox_personalized`.

## Multi-surface record selection

`select_record_heat` accepts heat records for exactly one run, platform key,
and compatible cohort surface. It selects the maximum heat and retains the
sorted union of observation IDs from every contributing surface. Mixed keys,
runs, or cohort surfaces are rejected. Sorting the evidence IDs makes ties
deterministic regardless of input order.

## Eligibility and normalization

The absolute heat floor is `30.0`. A candidate below the floor is excluded.
`eligible_cohort` can filter a broader same-run sequence by exact platform and
cohort surface. Every input row must belong to that one run even when the row
would later be filtered for platform, surface, or heat. Results are ordered by
heat descending and platform key ascending.

`normalize_cohort` accepts exactly one run/platform/cohort surface and rejects
mixed or duplicate platform-key input. Eligibility is applied before cohort
size and ranks are calculated.

For five or more eligible candidates, candidates are sorted by descending
heat. Ties receive their average one-based rank:

```text
percentile = (cohort_size - average_rank) / (cohort_size - 1)
platform_score = min(30 * heat / 100, 30 * percentile)
```

For fewer than five eligible candidates:

```text
platform_score = min(15, 30 * heat / 100)
```

If every eligible candidate ties, the small-cohort formula applies even when
the cohort contains five or more records. This also ensures a one-item cohort
can receive no more than 15 platform points.

Persisted `heat` and `platform_score` use decimal half-up rounding to one
decimal place. Output order is deterministic: heat descending, then platform
key ascending, then observation IDs. Numeric inputs reject booleans, nonfinite
values, and integers too large for finite float conversion with `ValueError`.
Independent parameters such as `heat_floor` are validated even for an empty
cohort.

## Unified opportunity score

`unified_game_radar.score` combines a normalized platform score with verified
search, independent-spread, and exact-intent SERP evidence. Every scoring
function is pure and requires an explicit UTC publication time whenever
freshness affects credit. Missing or stale evidence contributes zero; its
weight is never transferred to another component. Components and totals use
decimal half-up rounding to one decimal place.

| Component | Maximum |
|---|---:|
| Normalized platform momentum | 30 |
| Search demand | 30 |
| Independent external spread | 20 |
| SEO content gap | 20 |

### Search demand

Only completed Trends days participate. Evidence that the demand classifier
marks `unknown` earns no demand points.

- Persistence: 2 points per completed nonzero day, capped at 8.
- Latest retention: `8 * min(1, latest / peak)`.
- Later local maximum: 6 points at 50% or more of peak, 3 points from 30%
  through less than 50%, otherwise 0.
- Relevant autocomplete: 2 points for one distinct normalized query, 4 for at
  least two.
- Relevant related queries: the same 0/2/4 rule, independently.

The component is capped at 30. A repeated query with different casing or
spacing is one query. Queries outside the intended game's supported game-intent
suffixes receive no credit.

### Independent external spread

Only evidence with `author_relation=independent`, an observation no later than
publication, and publication age from zero through exactly seven days scores.
Developer and unknown-author rows stay available as provenance but earn zero.
Distinct domains come from canonical HTTPS URL hostnames with a leading
`www.` removed.

- Domain diversity: 4 points per distinct domain, capped at 8.
- Evidence rows: 2 points per qualifying row, capped at 4.
- Highest verified engagement: 8 at 10,000+, 6 at 1,000+, 4 at 100+, 2 at
  20+, 1 for a present count below 20, and 0 when every count is missing.
- Recency: 4 points when the newest qualifying publication is at most two days
  old; otherwise 2 through exactly seven days.

The component is capped at 20.

### SEO content gap

Freshness is inclusive through 24 hours. Stale or absent SERP evidence earns
zero. A missing count earns zero for that subcomponent without reweighting,
but makes SERP evidence unknown for action selection.

- Guide results: 10 points for 0, 7 for 1–2, 3 for 3–5, otherwise 0.
- Relevant nonofficial results: 6 points for 0, 4 for 1–3, 2 for 4–10,
  otherwise 0.
- Missing intents among `guide`, `codes`, `answers`, and `wiki`: 1 point each,
  capped at 4.

### Hard-gated actions

Numeric thresholds apply only after the search-demand and SERP gates:

| Total | Action |
|---|---|
| 80–100 | `immediate_action` |
| 65–79.9 | `worth_content_mvp` |
| 50–64.9 | `watch` |
| below 50 | `skip` |

The overrides are authoritative: demand `fail` is always `skip`,
`early_watch` is always `watch`, and `unknown` is always
`needs_verification`. A `pass` with unknown/stale demand or SERP evidence is
also `needs_verification`. A score of at least 80 without qualifying
independent evidence is demoted to `worth_content_mvp`; a high total never
overrides a hard gate.

### Stable unified ordering

The daily leaderboard sorts by action priority (`immediate_action`,
`worth_content_mvp`, `watch`, `needs_verification`, `skip`), then total score,
demand score, and platform score descending, followed by normalized name and
`opportunity_id` ascending.
