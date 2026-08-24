"""Pure, deterministic Steam heat and SEO opportunity scoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import math
from types import MappingProxyType
from typing import Collection, Literal, Mapping, Sequence

from .enrichment import EnrichmentRecord, Evidence
from .errors import InputValidationError
from .schemas import GameRecord, MetricObservation, WarningRecord
from .trend import AnalyzedCandidate, select_rank_improvement


Action = Literal[
    "needs_seo_enrichment",
    "insufficient_data",
    "immediate_action",
    "worth_positioning",
    "watch",
    "skip",
]
Confidence = Literal["A", "B", "C"]

_ACTIONS = {
    "needs_seo_enrichment",
    "insufficient_data",
    "immediate_action",
    "worth_positioning",
    "watch",
    "skip",
}
_FINAL_ACTIONS = {"immediate_action", "worth_positioning", "watch", "skip"}
_CONFIDENCES = {"A", "B", "C"}
_PROVENANCE_TOKENS = {
    "steam_official",
    "steamdb_manual_import",
    "historical_comparison",
}
_CONFIDENCE_ORDER = {"A": 0, "B": 1, "C": 2}
_STEAM_SOURCE_KINDS = {"steam_official", "steamdb_manual_import"}

_RELEASED_WEIGHTS = {
    "player_growth": 25,
    "current_player_scale": 10,
    "rank_improvement": 15,
    "release_recency": 10,
}
_UNRELEASED_WEIGHTS = {
    "upcoming_rank_improvement": 20,
    "wishlist_or_follower_gain": 20,
    "release_proximity": 10,
    "coming_soon_visibility": 10,
}

_GROWTH_POINTS = ((0, 0), (5, 25), (15, 50), (30, 75), (60, 100))
_CURRENT_PLAYER_POINTS = (
    (0, 0),
    (100, 20),
    (1_000, 50),
    (10_000, 80),
    (100_000, 100),
)
_RANK_POINTS = ((0, 0), (5, 40), (20, 70), (50, 100))
_GAIN_POINTS = ((0, 0), (100, 20), (1_000, 60), (5_000, 85), (20_000, 100))
_COMMUNITY_POINTS = ((0, 0), (1, 20), (3, 50), (10, 80), (25, 100))


@dataclass(frozen=True)
class ScoredCandidate:
    """Immutable candidate carrying all pure score-stage outputs."""

    record: GameRecord
    deltas: Mapping[str, float]
    metric_scores: Mapping[str, float]
    steam_heat_score: float | None
    seo_opportunity_score: float | None
    final_score: float | None
    action: Action
    confidence: Confidence
    warnings: Sequence[WarningRecord]
    evidence: Sequence[Evidence]
    recommended_content_types: Sequence[str]

    def __post_init__(self) -> None:
        if not isinstance(self.record, GameRecord):
            raise InputValidationError("record must be a GameRecord")
        frozen_deltas = _finite_mapping(self.deltas, "deltas", bounded=False)
        frozen_metrics = _finite_mapping(
            self.metric_scores,
            "metric_scores",
            bounded=True,
        )
        steam_heat = _optional_score(self.steam_heat_score, "steam_heat_score")
        seo = _optional_score(
            self.seo_opportunity_score,
            "seo_opportunity_score",
        )
        final = _optional_score(self.final_score, "final_score")
        if not isinstance(self.action, str) or self.action not in _ACTIONS:
            raise InputValidationError(f"invalid action: {self.action!r}")
        if not isinstance(self.confidence, str) or self.confidence not in _CONFIDENCES:
            raise InputValidationError(f"invalid confidence: {self.confidence!r}")
        if final is not None:
            if steam_heat is None or seo is None or self.action not in _FINAL_ACTIONS:
                raise InputValidationError(
                    "a final score requires both component scores and a final action"
                )
            if self.confidence not in {"A", "B"}:
                raise InputValidationError("confidence C candidates cannot have final scores")
            combined = 0.60 * steam_heat + 0.40 * seo
            if final != _rounded_score(combined):
                raise InputValidationError(
                    "final score does not match its weighted component scores"
                )
            if self.action != _action_for_raw_score(combined):
                raise InputValidationError("final action does not match final score")
        elif steam_heat is None and self.action != "insufficient_data":
            raise InputValidationError("missing Steam heat requires insufficient_data")
        elif steam_heat is not None and self.action != "needs_seo_enrichment":
            raise InputValidationError("preliminary candidates require needs_seo_enrichment")
        elif (
            steam_heat is not None
            and seo is not None
            and self.confidence in {"A", "B"}
        ):
            raise InputValidationError(
                "confidence A/B candidates with both components require a final score"
            )

        if isinstance(self.warnings, (str, bytes)) or not isinstance(
            self.warnings,
            Sequence,
        ):
            raise InputValidationError("warnings must be a sequence")
        warnings = tuple(self.warnings)
        if not all(isinstance(item, WarningRecord) for item in warnings):
            raise InputValidationError("warnings must contain WarningRecord values")
        if isinstance(self.evidence, (str, bytes)) or not isinstance(
            self.evidence,
            Sequence,
        ):
            raise InputValidationError("evidence must be a sequence")
        evidence = tuple(self.evidence)
        if not all(isinstance(item, Evidence) for item in evidence):
            raise InputValidationError("evidence must contain Evidence values")
        if isinstance(self.recommended_content_types, (str, bytes)) or not isinstance(
            self.recommended_content_types,
            Sequence,
        ):
            raise InputValidationError(
                "recommended_content_types must be a sequence"
            )
        content_types = tuple(self.recommended_content_types)
        if any(
            not isinstance(item, str) or not item.strip()
            for item in content_types
        ):
            raise InputValidationError(
                "recommended_content_types must contain non-empty strings"
            )
        if len(set(content_types)) != len(content_types):
            raise InputValidationError(
                "recommended_content_types must not contain duplicates"
            )
        object.__setattr__(self, "deltas", MappingProxyType(frozen_deltas))
        object.__setattr__(self, "metric_scores", MappingProxyType(frozen_metrics))
        object.__setattr__(self, "steam_heat_score", steam_heat)
        object.__setattr__(self, "seo_opportunity_score", seo)
        object.__setattr__(self, "final_score", final)
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "recommended_content_types", content_types)


def interpolate(points: Sequence[tuple[float, float]], value: float) -> float:
    """Linearly interpolate strictly ascending points and clamp both ends."""

    parsed_value = _finite_number(value, "interpolation value")
    if isinstance(points, (str, bytes)) or not isinstance(points, Sequence) or not points:
        raise InputValidationError("interpolation points must be a non-empty sequence")
    parsed: list[tuple[float, float]] = []
    for point in points:
        if not isinstance(point, Sequence) or isinstance(point, (str, bytes)) or len(point) != 2:
            raise InputValidationError("each interpolation point must contain x and score")
        x = _finite_number(point[0], "interpolation x")
        score = _finite_number(point[1], "interpolation score")
        if score < 0 or score > 100:
            raise InputValidationError("interpolation scores must be from 0 through 100")
        if parsed and x <= parsed[-1][0]:
            raise InputValidationError("interpolation x values must strictly increase")
        parsed.append((x, score))
    if parsed_value <= parsed[0][0]:
        return parsed[0][1]
    if parsed_value >= parsed[-1][0]:
        return parsed[-1][1]
    for (left_x, left_score), (right_x, right_score) in zip(parsed, parsed[1:]):
        if parsed_value <= right_x:
            fraction = (parsed_value - left_x) / (right_x - left_x)
            return left_score + fraction * (right_score - left_score)
    raise InputValidationError("unable to interpolate value")


def score_released(candidate: AnalyzedCandidate) -> ScoredCandidate:
    """Calculate the released-game Steam heat score with the fixed gate."""

    _candidate_for_status(candidate, "released")
    raw_scores: dict[str, float] = {}
    growth = None
    if _steam_observation(candidate.record, "current_players") is not None:
        growth = _largest_delta(
            candidate,
            ("current_players_1d_percent", "current_players_7d_percent"),
        )
    if growth is not None:
        raw_scores["player_growth"] = interpolate(_GROWTH_POINTS, max(growth, 0.0))
    players = _non_negative_metric(candidate.record, "current_players")
    if players is not None:
        raw_scores["current_player_scale"] = interpolate(
            _CURRENT_PLAYER_POINTS,
            players,
        )
    improvement = _select_historical_rank_improvement(candidate)
    if improvement is None:
        provider_candidate = AnalyzedCandidate(
            record=candidate.record,
            deltas={},
            newly_observed=candidate.newly_observed,
            warnings=candidate.warnings,
        )
        improvement = select_rank_improvement(provider_candidate)
    if improvement is not None:
        raw_scores["rank_improvement"] = interpolate(
            _RANK_POINTS,
            improvement,
        )
    age = _release_day_distance(candidate.record)
    if age is not None and age >= 0:
        raw_scores["release_recency"] = _released_recency(age)
    return _scored_from_metrics(candidate, raw_scores, _RELEASED_WEIGHTS, 2, 25)


def score_unreleased(candidate: AnalyzedCandidate) -> ScoredCandidate:
    """Calculate the unreleased-game Steam heat score with the fixed gate."""

    _candidate_for_status(candidate, "unreleased")
    raw_scores: dict[str, float] = {}
    improvement = _select_historical_rank_improvement(candidate)
    if improvement is not None:
        raw_scores["upcoming_rank_improvement"] = interpolate(
            _RANK_POINTS,
            improvement,
        )
    gains = tuple(
        value
        for value in (
            _non_negative_metric(candidate.record, "wishlist_gain_7d"),
            _non_negative_metric(candidate.record, "follower_gain_7d"),
        )
        if value is not None
    )
    if gains:
        raw_scores["wishlist_or_follower_gain"] = interpolate(
            _GAIN_POINTS,
            max(gains),
        )
    proximity = _release_day_distance(candidate.record)
    if proximity is not None and proximity >= 0:
        raw_scores["release_proximity"] = _release_proximity(proximity)
    rank = _positive_metric(candidate.record, "coming_soon_rank")
    if rank is not None:
        raw_scores["coming_soon_visibility"] = _coming_soon_visibility(rank)
    return _scored_from_metrics(candidate, raw_scores, _UNRELEASED_WEIGHTS, 2, 30)


def score_seo(record: EnrichmentRecord) -> float | None:
    """Calculate SEO opportunity when Google plus another metric pass weight 30."""

    if not isinstance(record, EnrichmentRecord):
        raise InputValidationError("record must be an EnrichmentRecord")
    scores: dict[str, tuple[float, int]] = {
        "google_competition_gap": (
            float(record.google_competition_gap_score),
            20,
        )
    }
    scores["expandable_query_count"] = (
        min(len(record.expandable_queries) / 20.0 * 100.0, 100.0),
        10,
    )
    community = tuple(
        interpolate(_COMMUNITY_POINTS, value)
        for value in (
            record.youtube_relevant_7d,
            record.reddit_relevant_7d,
        )
        if value is not None
    )
    if community:
        scores["youtube_reddit_cross_signal"] = (
            sum(community) / len(community),
            10,
        )
    weight = sum(item_weight for _, item_weight in scores.values())
    if len(scores) < 2 or weight < 30:
        return None
    return _rounded_score(
        sum(score * item_weight for score, item_weight in scores.values())
        / weight
    )


def apply_final_score(
    candidate: ScoredCandidate,
    record: EnrichmentRecord,
    provenance: Collection[str],
) -> ScoredCandidate:
    """Attach validated enrichment, confidence, and a gated final action.

    Provenance tokens are deliberately closed: ``steam_official``,
    ``steamdb_manual_import``, and ``historical_comparison``. Supplying an
    ``EnrichmentRecord`` represents valid enrichment evidence.
    """

    if not isinstance(candidate, ScoredCandidate):
        raise InputValidationError("candidate must be a ScoredCandidate")
    if not isinstance(record, EnrichmentRecord):
        raise InputValidationError("record must be an EnrichmentRecord")
    if candidate.record.appid != record.appid:
        raise InputValidationError("enrichment AppID does not match candidate")
    if isinstance(provenance, (str, bytes)) or not isinstance(provenance, Collection):
        raise InputValidationError("provenance must be a collection of tokens")
    if not all(isinstance(token, str) for token in provenance):
        raise InputValidationError("provenance contains an unknown token")
    tokens = set(provenance)
    if not tokens <= _PROVENANCE_TOKENS:
        raise InputValidationError("provenance contains an unknown token")

    seo_score = score_seo(record)
    confidence: Confidence = "C"
    if seo_score is not None and "historical_comparison" in tokens:
        has_official = "steam_official" in tokens
        has_manual = "steamdb_manual_import" in tokens
        if has_official and has_manual:
            confidence = "A"
        elif has_official or has_manual:
            confidence = "B"

    final_score: float | None = None
    action: Action
    if candidate.steam_heat_score is None:
        action = "insufficient_data"
    elif seo_score is None or confidence == "C":
        action = "needs_seo_enrichment"
    else:
        combined = 0.60 * candidate.steam_heat_score + 0.40 * seo_score
        final_score = _rounded_score(combined)
        action = _action_for_raw_score(combined)

    return ScoredCandidate(
        record=candidate.record,
        deltas=candidate.deltas,
        metric_scores=candidate.metric_scores,
        steam_heat_score=candidate.steam_heat_score,
        seo_opportunity_score=seo_score,
        final_score=final_score,
        action=action,
        confidence=confidence,
        warnings=candidate.warnings,
        evidence=record.evidence,
        recommended_content_types=_recommended_content_types(record),
    )


def candidate_sort_key(candidate: ScoredCandidate) -> Sequence[object]:
    """Return the exact five-level stable candidate ordering key."""

    if not isinstance(candidate, ScoredCandidate):
        raise InputValidationError("candidate must be a ScoredCandidate")
    primary = (
        candidate.final_score
        if candidate.final_score is not None
        else candidate.steam_heat_score
    )
    primary_value = -1.0 if primary is None else primary
    if candidate.record.release_status == "released":
        scale = _non_negative_metric(candidate.record, "current_players") or 0.0
    elif candidate.record.release_status == "unreleased":
        gains = (
            _non_negative_metric(candidate.record, "wishlist_gain_7d"),
            _non_negative_metric(candidate.record, "follower_gain_7d"),
        )
        scale = max((value for value in gains if value is not None), default=0.0)
    else:
        scale = 0.0
    return (
        -primary_value,
        _CONFIDENCE_ORDER[candidate.confidence],
        -scale,
        candidate.record.name.casefold(),
        candidate.record.appid,
    )


def _scored_from_metrics(
    candidate: AnalyzedCandidate,
    raw_scores: Mapping[str, float],
    weights: Mapping[str, int],
    minimum_count: int,
    minimum_weight: int,
) -> ScoredCandidate:
    available_weight = sum(weights[name] for name in raw_scores)
    heat = None
    if len(raw_scores) >= minimum_count and available_weight >= minimum_weight:
        heat = _rounded_score(
            sum(raw_scores[name] * weights[name] for name in raw_scores)
            / available_weight
        )
    return ScoredCandidate(
        record=candidate.record,
        deltas=candidate.deltas,
        metric_scores={name: _rounded_score(score) for name, score in raw_scores.items()},
        steam_heat_score=heat,
        seo_opportunity_score=None,
        final_score=None,
        action="needs_seo_enrichment" if heat is not None else "insufficient_data",
        confidence="C",
        warnings=candidate.warnings,
        evidence=(),
        recommended_content_types=(),
    )


def _candidate_for_status(candidate: object, expected: str) -> AnalyzedCandidate:
    if not isinstance(candidate, AnalyzedCandidate):
        raise InputValidationError("candidate must be an AnalyzedCandidate")
    if candidate.record.release_status != expected:
        raise InputValidationError(f"candidate must have {expected} release status")
    return candidate


def _largest_delta(candidate: AnalyzedCandidate, names: Sequence[str]) -> float | None:
    values = tuple(candidate.deltas[name] for name in names if name in candidate.deltas)
    return max(values) if values else None


def _select_historical_rank_improvement(
    candidate: AnalyzedCandidate,
) -> float | None:
    """Choose only 7d then 1d deltas for rank metrics present now.

    Unlike released scoring, upcoming scoring has no provider previous-rank
    fallback. Every selected delta must correspond exactly to a rank metric on
    the current candidate, preserving the Task 8 same-source history contract.
    """

    for window in ("7d", "1d"):
        values = tuple(
            candidate.deltas[key]
            for metric_name in sorted(candidate.record.metrics)
            if metric_name != "previous_rank"
            and (metric_name == "rank" or metric_name.endswith("_rank"))
            and _steam_observation(candidate.record, metric_name) is not None
            for key in (f"{metric_name}_{window}_change",)
            if key in candidate.deltas
        )
        if values:
            return max(values)
    return None


def _non_negative_metric(record: GameRecord, name: str) -> float | None:
    observation = _steam_observation(record, name)
    if observation is None:
        return None
    value = observation.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _positive_metric(record: GameRecord, name: str) -> float | None:
    value = _non_negative_metric(record, name)
    return value if value is not None and value > 0 else None


def _release_day_distance(record: GameRecord) -> int | None:
    observation = _steam_observation(record, "release_date")
    if observation is None or not isinstance(observation.value, str):
        return None
    try:
        release_date = date.fromisoformat(observation.value)
        observed = datetime.fromisoformat(
            observation.observed_at[:-1] + "+00:00"
        ).astimezone(timezone.utc).date()
    except ValueError:
        return None
    if record.release_status == "released":
        return (observed - release_date).days
    return (release_date - observed).days


def _released_recency(age: int) -> float:
    if age <= 7:
        return 100.0
    if age <= 30:
        return 70.0
    if age <= 90:
        return 40.0
    return 0.0


def _release_proximity(days: int) -> float:
    if days <= 14:
        return 100.0
    if days <= 30:
        return 80.0
    if days <= 90:
        return 60.0
    if days <= 180:
        return 30.0
    return 0.0


def _coming_soon_visibility(rank: float) -> float:
    if rank <= 1:
        return 100.0
    if rank <= 5:
        return 80.0
    if rank <= 20:
        return 50.0
    if rank <= 50:
        return 20.0
    return 0.0


def _recommended_content_types(record: EnrichmentRecord) -> tuple[str, ...]:
    result: list[str] = []
    if record.expandable_queries:
        result.append("wiki_or_guide")
    if record.youtube_relevant_7d is not None and record.youtube_relevant_7d > 0:
        result.append("video")
    if (
        (record.reddit_relevant_7d is not None and record.reddit_relevant_7d > 0)
        or (record.reddit_upvotes_7d is not None and record.reddit_upvotes_7d > 0)
    ):
        result.append("community")
    return tuple(result)


def _action_for_raw_score(value: float) -> Action:
    if value >= 80:
        return "immediate_action"
    if value >= 65:
        return "worth_positioning"
    if value >= 50:
        return "watch"
    return "skip"


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputValidationError(f"{name} must be a finite number")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as error:
        raise InputValidationError(f"{name} must be a finite number") from error
    if not math.isfinite(parsed):
        raise InputValidationError(f"{name} must be a finite number")
    return parsed


def _steam_observation(
    record: GameRecord,
    name: str,
) -> MetricObservation | None:
    observation = record.metrics.get(name)
    if observation is None or observation.source_kind not in _STEAM_SOURCE_KINDS:
        return None
    return observation


def _rounded_score(value: object) -> float:
    parsed = _finite_number(value, "score")
    if parsed < 0 or parsed > 100:
        raise InputValidationError("score must be from 0 through 100")
    return round(parsed, 1)


def _optional_score(value: object, name: str) -> float | None:
    if value is None:
        return None
    try:
        return _rounded_score(value)
    except InputValidationError as error:
        raise InputValidationError(f"{name} must be a finite score from 0 through 100") from error


def _finite_mapping(
    value: object,
    name: str,
    *,
    bounded: bool,
) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise InputValidationError(f"{name} must be a mapping")
    result: dict[str, float] = {}
    if any(not isinstance(key, str) or not key for key in value):
        raise InputValidationError(f"{name} keys must be non-empty strings")
    for key in sorted(value):
        parsed = _finite_number(value[key], f"{name} value")
        result[key] = _rounded_score(parsed) if bounded else parsed
    return result
