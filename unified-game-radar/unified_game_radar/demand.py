"""Pure aggregation and hard search-demand classification rules."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import InputValidationError
from .identity import normalize_name
from .schemas import OpportunityEvidence, SearchQueryEvidence, TrendEvidence, TrendPoint


EVIDENCE_MAX_AGE = timedelta(hours=24)
_GAME_INTENT_SUFFIXES = frozenset(
    {
        "game",
        "codes",
        "code",
        "guide",
        "guides",
        "wiki",
        "walkthrough",
        "gameplay",
        "roblox",
        "steam",
        "itch",
        "play",
    }
)


@dataclass(frozen=True)
class DemandClassification:
    """The hard gate outcome plus a stable machine-readable reason."""

    state: str
    reason: str


def _utc_instant(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    return value.astimezone(timezone.utc)


def _bounded_trend_value(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number or null")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(parsed) or parsed < 0 or parsed > 100:
        raise ValueError(f"{name} must be between 0 and 100")
    return parsed


def aggregate_daily_means(
    points: Iterable[tuple[datetime, float | None]],
    *,
    timezone_name: str,
    publication_time: datetime,
) -> tuple[TrendPoint, ...]:
    """Aggregate timestamped UTC Trends samples by declared local date.

    Null samples do not dilute a day's known readings. A day containing only
    null samples remains explicitly unknown. The publication day's local
    bucket is always incomplete, even when the source labels it otherwise.
    """

    publication = _utc_instant(publication_time, "publication_time")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError("timezone_name must be nonempty text")
    try:
        local_timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"unknown timezone: {timezone_name}") from error

    samples_by_day: dict[object, list[float | None]] = defaultdict(list)
    seen_instants: set[datetime] = set()
    for index, row in enumerate(points):
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise ValueError(f"points[{index}] must be a timestamp/value pair")
        observed_at = _utc_instant(row[0], f"points[{index}].observed_at")
        if observed_at > publication:
            raise ValueError(f"points[{index}].observed_at must not be in the future")
        if observed_at in seen_instants:
            raise ValueError("hourly Trends timestamps must be unique")
        seen_instants.add(observed_at)
        samples_by_day[observed_at.astimezone(local_timezone).date()].append(
            _bounded_trend_value(row[1], f"points[{index}].value")
        )

    current_day = publication.astimezone(local_timezone).date()
    result: list[TrendPoint] = []
    for local_day in sorted(samples_by_day):
        known = tuple(
            value for value in samples_by_day[local_day] if value is not None
        )
        mean = sum(known) / len(known) if known else None
        result.append(
            TrendPoint(
                date=local_day,
                value=mean,
                complete=local_day < current_day,
            )
        )
    return tuple(result)


def completed_points(
    trends: TrendEvidence,
    *,
    publication_time: datetime,
) -> tuple[TrendPoint, ...]:
    """Return completed, pre-publication-local-day points in date order."""

    if not isinstance(trends, TrendEvidence):
        raise ValueError("trends must be TrendEvidence")
    publication = _utc_instant(publication_time, "publication_time")
    current_day = publication.astimezone(ZoneInfo(trends.timezone)).date()
    return tuple(
        sorted(
            (
                point
                for point in trends.points
                if point.complete and point.date < current_day
            ),
            key=lambda point: point.date,
        )
    )


def _numeric_values(values: Sequence[float]) -> tuple[float, ...]:
    parsed_values: list[float] = []
    for index, value in enumerate(values):
        parsed = _bounded_trend_value(value, f"values[{index}]")
        if parsed is None:
            raise ValueError(f"values[{index}] must not be null")
        parsed_values.append(parsed)
    return tuple(parsed_values)


def _later_local_maxima(values: tuple[float, ...], peak_index: int) -> tuple[float, ...]:
    maxima: list[float] = []
    for index in range(peak_index + 1, len(values)):
        value = values[index]
        previous = values[index - 1]
        following = values[index + 1] if index + 1 < len(values) else None
        if value >= previous and (following is None or value >= following):
            maxima.append(value)
    return tuple(maxima)


def is_single_spike(values: Sequence[float]) -> bool:
    """Return whether completed values match the exact one-wave detector."""

    parsed = _numeric_values(values)
    if not parsed:
        return False
    peak = max(parsed)
    if peak <= 0:
        return False
    peak_index = parsed.index(peak)
    remaining = parsed[:peak_index] + parsed[peak_index + 1 :]
    second_highest = max(remaining, default=0.0)
    post_peak = parsed[peak_index + 1 :]
    if peak < 2 * second_highest or len(post_peak) < 2:
        return False
    if any(value >= 0.4 * peak for value in post_peak):
        return False
    return not any(
        value >= 0.5 * peak
        for value in _later_local_maxima(parsed, peak_index)
    )


def has_second_wave(values: Sequence[float]) -> bool:
    """Return whether a later completed local maximum retains half the peak."""

    parsed = _numeric_values(values)
    if not parsed:
        return False
    peak = max(parsed)
    if peak <= 0:
        return False
    peak_index = parsed.index(peak)
    return any(
        value >= 0.5 * peak
        for value in _later_local_maxima(parsed, peak_index)
    )


def is_unambiguous_game_query(game_name: str, query: str) -> bool:
    """Require the exact normalized title followed by the `game` modifier."""

    try:
        normalized_name = normalize_name(game_name)
        normalized_query = normalize_name(query)
    except (InputValidationError, TypeError, ValueError):
        return False
    return normalized_query == f"{normalized_name} game"


def _is_relevant_support_query(game_name: str, query: str) -> bool:
    try:
        normalized_name = normalize_name(game_name)
        normalized_query = normalize_name(query)
    except (InputValidationError, TypeError, ValueError):
        return False
    prefix = f"{normalized_name} "
    if not normalized_query.startswith(prefix):
        return False
    suffix = normalized_query[len(prefix) :]
    first_word = suffix.split(maxsplit=1)[0]
    return first_word in _GAME_INTENT_SUFFIXES


def _fresh(observed_at: datetime, publication_time: datetime) -> bool:
    age = publication_time - observed_at
    return timedelta(0) <= age <= EVIDENCE_MAX_AGE


def _infer_game_name(query: str) -> str | None:
    if not isinstance(query, str):
        return None
    words = query.split()
    if len(words) < 2 or words[-1].casefold() != "game":
        return None
    inferred = " ".join(words[:-1])
    return inferred or None


def _unknown(reason: str) -> DemandClassification:
    return DemandClassification(state="unknown", reason=reason)


def _trends_window_is_current(
    trends: TrendEvidence,
    publication_time: datetime,
) -> bool:
    """Validate the exact local-calendar window supported by v1 evidence."""

    if trends.timeframe != "now 7-d":
        return False
    current_day = publication_time.astimezone(ZoneInfo(trends.timezone)).date()
    first_selected_day = current_day - timedelta(days=7)
    if any(
        point.date < first_selected_day or point.date > current_day
        for point in trends.points
    ):
        return False
    completed_dates = tuple(
        point.date
        for point in trends.points
        if point.complete and point.date < current_day
    )
    return bool(completed_dates) and max(completed_dates) == current_day - timedelta(
        days=1
    )


def _search_contract_is_usable(
    evidence: OpportunityEvidence,
    trends: TrendEvidence,
    game_name: str,
    publication_time: datetime,
) -> tuple[bool, tuple[SearchQueryEvidence, ...]]:
    if (
        trends.query_type != "search_term"
        or trends.category != 0
        or trends.property != "web"
        or trends.raw_artifact is None
    ):
        return False, ()
    observed_claims = (evidence.observed_at, trends.observed_at)
    if not all(_fresh(instant, publication_time) for instant in observed_claims):
        return False, ()
    if not is_unambiguous_game_query(game_name, trends.query):
        return False, ()
    if evidence.serp is not None:
        if not _fresh(evidence.serp.observed_at, publication_time):
            return False, ()
        if not is_unambiguous_game_query(game_name, evidence.serp.query):
            return False, ()

    support = evidence.autocomplete_queries + evidence.related_queries
    for row in support:
        if not _fresh(row.observed_at, publication_time):
            return False, ()
    relevant_support = tuple(
        row
        for row in support
        if _is_relevant_support_query(game_name, row.query)
    )
    return True, relevant_support


def classify_demand(
    evidence: OpportunityEvidence | None,
    *,
    game_name: str | None = None,
    publication_time: datetime | None = None,
) -> DemandClassification:
    """Apply the hard gate in order: unknown, fail, early_watch, pass."""

    if publication_time is None:
        return _unknown("missing_publication_time")
    if evidence is None or not isinstance(evidence, OpportunityEvidence):
        return _unknown("missing_evidence")
    trends = evidence.trends
    if trends is None:
        return _unknown("missing_trends")

    try:
        publication = _utc_instant(
            publication_time,
            "publication_time",
        )
    except ValueError:
        return _unknown("invalid_publication_time")
    intended_name = game_name or _infer_game_name(trends.query)
    if intended_name is None:
        return _unknown("ambiguous_game_query")

    usable, support_rows = _search_contract_is_usable(
        evidence,
        trends,
        intended_name,
        publication,
    )
    if not usable:
        return _unknown("invalid_or_stale_search_evidence")

    all_dates = tuple(point.date for point in trends.points)
    if len(set(all_dates)) != len(all_dates):
        return _unknown("duplicate_trends_date")
    current_day = publication.astimezone(ZoneInfo(trends.timezone)).date()
    if any(point.date > current_day for point in trends.points):
        return _unknown("future_trends_date")
    if not _trends_window_is_current(trends, publication):
        return _unknown("invalid_trends_window")

    complete = completed_points(trends, publication_time=publication)
    if not complete:
        incomplete_positive = any(
            point.value is not None and point.value > 0
            for point in trends.points
        )
        if incomplete_positive:
            return DemandClassification("early_watch", "incomplete_positive_demand")
        return _unknown("no_completed_trends_points")
    if any(point.value is None for point in complete):
        return _unknown("missing_completed_trends_value")

    values = tuple(float(point.value) for point in complete if point.value is not None)
    support_present = bool(support_rows)
    incomplete_positive = any(
        (not point.complete or point.date >= current_day)
        and point.value is not None
        and point.value > 0
        for point in trends.points
    )
    peak = max(values)
    latest = values[-1]

    # fail precedes every plausible-but-immature classification.
    if peak == 0 and not support_present and not incomplete_positive:
        return DemandClassification("fail", "zero_demand")
    if peak > 0 and latest == 0 and not support_present:
        return DemandClassification("fail", "decayed_to_zero_without_support")

    # Exact one-wave detector must precede the otherwise permissive pass test.
    if is_single_spike(values):
        return DemandClassification("early_watch", "single_spike")

    nonzero_days = sum(value > 0 for value in values)
    retained = peak > 0 and latest >= 0.3 * peak
    second_wave = has_second_wave(values)
    if (
        nonzero_days >= 2
        and retained
        and (support_present or second_wave)
    ):
        return DemandClassification("pass", "durable_demand")

    if incomplete_positive:
        return DemandClassification("early_watch", "incomplete_positive_demand")
    if nonzero_days == 1:
        return DemandClassification("early_watch", "one_nonzero_day")
    if not retained:
        return DemandClassification("early_watch", "insufficient_retention")
    if not support_present and not second_wave:
        return DemandClassification("early_watch", "missing_intent_support")
    return DemandClassification("early_watch", "demand_not_yet_durable")


__all__ = [
    "DemandClassification",
    "aggregate_daily_means",
    "classify_demand",
    "completed_points",
    "has_second_wave",
    "is_single_spike",
    "is_unambiguous_game_query",
]
