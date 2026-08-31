"""Pure opportunity component scoring, hard-gated actions, and ordering."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import math
from urllib.parse import urlsplit

from .demand import DemandClassification, classify_demand, completed_points
from .identity import normalize_name
from .schemas import (
    ExternalEvidence,
    OpportunityEvidence,
    ScoredOpportunity,
    SerpEvidence,
    WarningRecord,
)


SEARCH_EVIDENCE_MAX_AGE = timedelta(hours=24)
EXTERNAL_EVIDENCE_MAX_AGE = timedelta(days=7)
_ONE_DECIMAL = Decimal("0.1")
_DEMAND_STATES = frozenset({"pass", "early_watch", "fail", "unknown"})
_ACTION_PRIORITY = {
    "immediate_action": 0,
    "worth_content_mvp": 1,
    "watch": 2,
    "needs_verification": 3,
    "skip": 4,
}
_RELEVANT_INTENTS = frozenset(
    {
        "game",
        "codes",
        "code",
        "guide",
        "guides",
        "wiki",
        "answers",
        "answer",
        "walkthrough",
        "gameplay",
        "roblox",
        "steam",
        "itch",
        "play",
    }
)


def _round_one_decimal(value: float) -> float:
    return float(
        Decimal(str(value)).quantize(_ONE_DECIMAL, rounding=ROUND_HALF_UP)
    )


def _utc_instant(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    return value.astimezone(timezone.utc)


def _bounded_number(value: object, name: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(parsed) or parsed < 0 or parsed > maximum:
        raise ValueError(f"{name} must be between 0 and {maximum:g}")
    return parsed


def _fresh(
    observed_at: datetime,
    publication_time: datetime,
    maximum_age: timedelta,
) -> bool:
    age = publication_time - observed_at
    return timedelta(0) <= age <= maximum_age


def _later_local_maxima(values: tuple[float, ...]) -> tuple[float, ...]:
    if not values:
        return ()
    peak_index = values.index(max(values))
    maxima: list[float] = []
    for index in range(peak_index + 1, len(values)):
        value = values[index]
        previous = values[index - 1]
        following = values[index + 1] if index + 1 < len(values) else None
        if value >= previous and (following is None or value >= following):
            maxima.append(value)
    return tuple(maxima)


def _is_relevant_query(game_name: str, query: str) -> bool:
    try:
        normalized_name = normalize_name(game_name)
        normalized_query = normalize_name(query)
    except (TypeError, ValueError):
        return False
    prefix = f"{normalized_name} "
    if not normalized_query.startswith(prefix):
        return False
    suffix = normalized_query[len(prefix) :]
    return suffix.split(maxsplit=1)[0] in _RELEVANT_INTENTS


def _distinct_relevant_count(game_name: str, rows: Sequence[object]) -> int:
    queries: set[str] = set()
    for row in rows:
        query = getattr(row, "query", None)
        if isinstance(query, str) and _is_relevant_query(game_name, query):
            queries.add(normalize_name(query))
    return len(queries)


def _support_points(count: int) -> int:
    if count >= 2:
        return 4
    if count == 1:
        return 2
    return 0


def score_demand(
    evidence: OpportunityEvidence | None,
    *,
    game_name: str,
    publication_time: datetime,
) -> float:
    """Score verified demand strength and durability from zero to thirty."""

    publication = _utc_instant(publication_time, "publication_time")
    if evidence is None:
        return 0.0
    if not isinstance(evidence, OpportunityEvidence):
        raise ValueError("evidence must be OpportunityEvidence or None")
    classification = classify_demand(
        evidence,
        game_name=game_name,
        publication_time=publication,
    )
    if classification.state == "unknown" or evidence.trends is None:
        return 0.0

    complete = completed_points(
        evidence.trends,
        publication_time=publication,
    )
    if not complete or any(point.value is None for point in complete):
        return 0.0
    values = tuple(float(point.value) for point in complete if point.value is not None)
    peak = max(values, default=0.0)

    persistence = min(8, 2 * sum(value > 0 for value in values))
    retention = 0.0
    if peak > 0:
        retention = 8 * min(1.0, values[-1] / peak)

    wave_ratio = max(_later_local_maxima(values), default=0.0) / peak if peak else 0.0
    if wave_ratio >= 0.5:
        second_wave = 6
    elif wave_ratio >= 0.3:
        second_wave = 3
    else:
        second_wave = 0

    autocomplete = _support_points(
        _distinct_relevant_count(game_name, evidence.autocomplete_queries)
    )
    related = _support_points(
        _distinct_relevant_count(game_name, evidence.related_queries)
    )
    return _round_one_decimal(
        min(30.0, persistence + retention + second_wave + autocomplete + related)
    )


def _qualifying_external_rows(
    evidence: Iterable[ExternalEvidence],
    publication_time: datetime,
) -> tuple[ExternalEvidence, ...]:
    rows: list[ExternalEvidence] = []
    for index, row in enumerate(evidence):
        if not isinstance(row, ExternalEvidence):
            raise ValueError(
                f"external_evidence[{index}] must be ExternalEvidence"
            )
        published_age = publication_time - row.published_at
        if (
            row.author_relation == "independent"
            and timedelta(0) <= published_age <= EXTERNAL_EVIDENCE_MAX_AGE
            and row.observed_at <= publication_time
        ):
            rows.append(row)
    return tuple(rows)


def _source_domain(row: ExternalEvidence) -> str:
    hostname = urlsplit(row.url).hostname
    if hostname is None:
        return ""
    normalized = hostname.casefold().rstrip(".")
    if normalized.startswith("www."):
        normalized = normalized[4:]
    return normalized


def _engagement_points(rows: Sequence[ExternalEvidence]) -> int:
    verified = tuple(
        row.engagement_count
        for row in rows
        if row.engagement_count is not None
    )
    if not verified:
        return 0
    highest = max(verified)
    if highest >= 10_000:
        return 8
    if highest >= 1_000:
        return 6
    if highest >= 100:
        return 4
    if highest >= 20:
        return 2
    return 1


def score_external_spread(
    evidence: Iterable[ExternalEvidence],
    *,
    publication_time: datetime,
) -> float:
    """Score fresh independent organic spread from zero to twenty."""

    publication = _utc_instant(publication_time, "publication_time")
    if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Iterable):
        raise ValueError("evidence must be an iterable of ExternalEvidence")
    rows = _qualifying_external_rows(evidence, publication)
    if not rows:
        return 0.0

    domain_count = len({domain for row in rows if (domain := _source_domain(row))})
    diversity = min(8, 4 * domain_count)
    evidence_count = min(4, 2 * len(rows))
    engagement = _engagement_points(rows)
    newest_age = min(publication - row.published_at for row in rows)
    recency = 4 if newest_age <= timedelta(days=2) else 2
    return _round_one_decimal(
        min(20.0, diversity + evidence_count + engagement + recency)
    )


def score_seo_gap(
    evidence: SerpEvidence | None,
    *,
    publication_time: datetime,
) -> float:
    """Score known exact-intent SERP gaps from zero to twenty."""

    publication = _utc_instant(publication_time, "publication_time")
    if evidence is None:
        return 0.0
    if not isinstance(evidence, SerpEvidence):
        raise ValueError("evidence must be SerpEvidence or None")
    if not _fresh(evidence.observed_at, publication, SEARCH_EVIDENCE_MAX_AGE):
        return 0.0

    guide_count = evidence.guide_results
    if guide_count is None:
        guide = 0
    elif guide_count == 0:
        guide = 10
    elif guide_count <= 2:
        guide = 7
    elif guide_count <= 5:
        guide = 3
    else:
        guide = 0

    nonofficial_count = evidence.relevant_nonofficial_results
    if nonofficial_count is None:
        nonofficial = 0
    elif nonofficial_count == 0:
        nonofficial = 6
    elif nonofficial_count <= 3:
        nonofficial = 4
    elif nonofficial_count <= 10:
        nonofficial = 2
    else:
        nonofficial = 0

    missing_intents = min(4, len(evidence.missing_intents))
    return _round_one_decimal(min(20.0, guide + nonofficial + missing_intents))


def _known_serp(
    evidence: SerpEvidence | None,
    publication_time: datetime,
) -> bool:
    return bool(
        isinstance(evidence, SerpEvidence)
        and evidence.guide_results is not None
        and evidence.relevant_nonofficial_results is not None
        and _fresh(
            evidence.observed_at,
            publication_time,
            SEARCH_EVIDENCE_MAX_AGE,
        )
    )


def action_for(
    total_score: float,
    *,
    demand_state: str,
    demand_evidence_known: bool,
    serp_evidence_known: bool,
    has_independent_evidence: bool,
) -> str:
    """Apply demand/SERP hard gates before numeric action thresholds."""

    total = _round_one_decimal(
        _bounded_number(total_score, "total_score", 100)
    )
    if demand_state not in _DEMAND_STATES:
        raise ValueError("demand_state is invalid")
    flags = {
        "demand_evidence_known": demand_evidence_known,
        "serp_evidence_known": serp_evidence_known,
        "has_independent_evidence": has_independent_evidence,
    }
    for name, value in flags.items():
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")

    if demand_state == "fail":
        return "skip"
    if demand_state == "early_watch":
        return "watch"
    if demand_state == "unknown":
        return "needs_verification"
    if not demand_evidence_known or not serp_evidence_known:
        return "needs_verification"
    if total >= 80:
        if not has_independent_evidence:
            return "worth_content_mvp"
        return "immediate_action"
    if total >= 65:
        return "worth_content_mvp"
    if total >= 50:
        return "watch"
    return "skip"


def score_opportunity(
    *,
    run_id: str,
    opportunity_id: str,
    game_name: str,
    platform_score: float,
    evidence: OpportunityEvidence | None,
    publication_time: datetime,
    warnings: tuple[WarningRecord, ...] = (),
) -> ScoredOpportunity:
    """Build one deterministic scored record without persistence side effects."""

    publication = _utc_instant(publication_time, "publication_time")
    rounded_platform = _round_one_decimal(
        _bounded_number(platform_score, "platform_score", 30)
    )
    if evidence is not None:
        if not isinstance(evidence, OpportunityEvidence):
            raise ValueError("evidence must be OpportunityEvidence or None")
        if evidence.run_id != run_id:
            raise ValueError("evidence run_id must match scored run_id")
        if evidence.opportunity_id != opportunity_id:
            raise ValueError(
                "evidence opportunity_id must match scored opportunity_id"
            )

    classification: DemandClassification = classify_demand(
        evidence,
        game_name=game_name,
        publication_time=publication,
    )
    demand = score_demand(
        evidence,
        game_name=game_name,
        publication_time=publication,
    )
    external_rows = evidence.external_evidence if evidence is not None else ()
    external = score_external_spread(
        external_rows,
        publication_time=publication,
    )
    serp = evidence.serp if evidence is not None else None
    seo = score_seo_gap(serp, publication_time=publication)
    total = _round_one_decimal(
        float(
            sum(
                Decimal(str(value))
                for value in (rounded_platform, demand, external, seo)
            )
        )
    )
    has_independent = bool(
        _qualifying_external_rows(external_rows, publication)
    )
    action = action_for(
        total,
        demand_state=classification.state,
        demand_evidence_known=classification.state != "unknown",
        serp_evidence_known=_known_serp(serp, publication),
        has_independent_evidence=has_independent,
    )
    return ScoredOpportunity(
        schema_version=1,
        run_id=run_id,
        opportunity_id=opportunity_id,
        demand_state=classification.state,
        platform_score=rounded_platform,
        demand_score=demand,
        external_score=external,
        seo_score=seo,
        total_score=total,
        action=action,
        warnings=warnings,
    )


def opportunity_sort_key(
    opportunity: ScoredOpportunity,
    normalized_name: str,
) -> tuple[object, ...]:
    """Return the canonical stable unified-leaderboard ordering key."""

    if not isinstance(opportunity, ScoredOpportunity):
        raise ValueError("opportunity must be ScoredOpportunity")
    if opportunity.action not in _ACTION_PRIORITY:
        raise ValueError("opportunity action is invalid")
    try:
        name = normalize_name(normalized_name)
    except (TypeError, ValueError) as error:
        raise ValueError("normalized_name must be nonempty text") from error
    return (
        _ACTION_PRIORITY[opportunity.action],
        -opportunity.total_score,
        -opportunity.demand_score,
        -opportunity.platform_score,
        name,
        opportunity.opportunity_id,
    )


__all__ = [
    "action_for",
    "opportunity_sort_key",
    "score_demand",
    "score_external_spread",
    "score_opportunity",
    "score_seo_gap",
]
