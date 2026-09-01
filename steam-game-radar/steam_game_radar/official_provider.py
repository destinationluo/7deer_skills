"""Pure Steam parsers plus deterministic official collection and normalization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import json
import math
import re
from types import MappingProxyType
from typing import Generic, TypeVar, cast
import unicodedata

from .config import RadarConfig
from .errors import InputValidationError, ProviderUnavailableError
from .http_client import JsonHttpClient
from .schemas import (
    MAX_JSON_SAFE_INTEGER,
    MAX_STEAM_APPID,
    MIN_JSON_SAFE_INTEGER,
    GameRecord,
    MetricObservation,
    WarningRecord,
)


STEAM_MOST_PLAYED_RANK_SOURCE_ID = "steam_most_played_rank"
STEAM_PREVIOUS_RANK_SOURCE_ID = "steam_previous_rank"
STEAM_TOP_SELLER_RANK_SOURCE_ID = "steam_top_seller_rank"
STEAM_NEW_RELEASE_RANK_SOURCE_ID = "steam_new_release_rank"
STEAM_COMING_SOON_RANK_SOURCE_ID = "steam_coming_soon_rank"
STEAM_PEAK_PLAYERS_SOURCE_ID = "steam_peak_players"
STEAM_CURRENT_PLAYERS_SOURCE_ID = "steam_current_players"
STEAM_APPDETAILS_SOURCE_ID = "steam_appdetails"

MOST_PLAYED_URL = (
    "https://api.steampowered.com/"
    "ISteamChartsService/GetMostPlayedGames/v1/"
)
FEATURED_CATEGORIES_URL = (
    "https://store.steampowered.com/api/featuredcategories"
    "?cc={country}&l={language}"
)
APPDETAILS_URL = (
    "https://store.steampowered.com/api/appdetails"
    "?appids={appid}&cc={country}&l={language}"
)
CURRENT_PLAYERS_URL = (
    "https://api.steampowered.com/"
    "ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={appid}"
)

_MOST_PLAYED_WARNING = WarningRecord(
    code="steam_most_played_malformed",
    message="Steam most-played response is malformed.",
)
_FEATURED_WARNING = WarningRecord(
    code="steam_featured_categories_malformed",
    message="Steam featured-categories response is malformed.",
)

_FEATURED_CATEGORIES = (
    "top_sellers",
    "new_releases",
    "coming_soon",
)
_FEATURED_METRICS = {
    "top_sellers": (1, "top_seller_rank", STEAM_TOP_SELLER_RANK_SOURCE_ID),
    "new_releases": (2, "new_release_rank", STEAM_NEW_RELEASE_RANK_SOURCE_ID),
    "coming_soon": (0, "coming_soon_rank", STEAM_COMING_SOON_RANK_SOURCE_ID),
}
_RELEASE_STATUSES = {"released", "unreleased", "unknown"}
_CAPABILITY_NAMES = (
    "most_played",
    "featured_categories",
    "appdetails",
    "current_players",
)
_DISCOVERY_SOURCE_ORDER = (
    "most_played",
    "top_sellers",
    "new_releases",
    "coming_soon",
)
_APP_TYPE_EXCLUSIONS = {
    "dlc": (
        "steam_app_type_dlc_excluded",
        "Steam DLC was excluded.",
    ),
    "demo": (
        "steam_app_type_demo_excluded",
        "Steam demo was excluded.",
    ),
    "software": (
        "steam_app_type_software_excluded",
        "Steam software was excluded.",
    ),
    "video": (
        "steam_app_type_video_excluded",
        "Steam video was excluded.",
    ),
    "tool": (
        "steam_app_type_tool_excluded",
        "Steam tool was excluded.",
    ),
}
_LOCALIZED_DATE_PATTERNS = (
    re.compile(
        r"(?P<year>[0-9]{4})\s*年\s*(?P<month>[0-9]{1,2})\s*"
        r"月\s*(?P<day>[0-9]{1,2})\s*日"
    ),
    re.compile(
        r"(?P<year>[0-9]{4})\s*년\s*(?P<month>[0-9]{1,2})\s*"
        r"월\s*(?P<day>[0-9]{1,2})\s*일"
    ),
    re.compile(
        r"(?P<year>[0-9]{4})/(?P<month>[0-9]{1,2})/"
        r"(?P<day>[0-9]{1,2})"
    ),
    re.compile(
        r"(?P<year>[0-9]{4})\.(?P<month>[0-9]{1,2})\."
        r"(?P<day>[0-9]{1,2})"
    ),
)
_ALPHABETIC_DATE_PATTERNS = (
    re.compile(
        r"(?P<day>[0-9]{1,2})/(?P<month>[^\W\d_]+)\.?/"
        r"(?P<year>[0-9]{4})"
    ),
    re.compile(
        r"(?P<day>[0-9]{1,2})\.?\s+(?P<month>[^\W\d_]+)\.?\s+"
        r"(?P<year>[0-9]{4})"
    ),
)
# Deliberately bounded aliases for common Steam Store month abbreviations in
# English, French, German, Spanish, and Portuguese. Tokens are case-folded and
# stripped of accents before lookup; unknown locales are never guessed.
_MONTH_ALIASES = {
    "jan": 1,
    "janv": 1,
    "ene": 1,
    "feb": 2,
    "fev": 2,
    "fevr": 2,
    "mar": 3,
    "mars": 3,
    "marz": 3,
    "mrz": 3,
    "apr": 4,
    "avr": 4,
    "abr": 4,
    "may": 5,
    "mai": 5,
    "jun": 6,
    "juin": 6,
    "jul": 7,
    "juil": 7,
    "aug": 8,
    "aout": 8,
    "ago": 8,
    "sep": 9,
    "sept": 9,
    "set": 9,
    "oct": 10,
    "okt": 10,
    "out": 10,
    "nov": 11,
    "dec": 12,
    "dez": 12,
    "dic": 12,
}

T = TypeVar("T")


class _MalformedPayload(ValueError):
    """Internal sentinel used to make capability parsing all-or-nothing."""


@dataclass(frozen=True)
class ParseResult(Generic[T]):
    value: T
    warnings: Sequence[WarningRecord]

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _freeze_container(self.value))
        warnings = tuple(self.warnings)
        if not all(isinstance(warning, WarningRecord) for warning in warnings):
            raise InputValidationError("warnings must contain WarningRecord values")
        object.__setattr__(self, "warnings", warnings)


@dataclass(frozen=True)
class DiscoveryCandidate:
    appid: int
    priority: tuple[int, int, int]
    source_ranks: Mapping[str, MetricObservation]
    source_names: Sequence[str]

    def __post_init__(self) -> None:
        _steam_appid(self.appid)
        if (
            not isinstance(self.priority, tuple)
            or len(self.priority) != 3
            or any(
                isinstance(part, bool) or not isinstance(part, int)
                for part in self.priority
            )
            or self.priority[0] < 0
            or self.priority[1] <= 0
            or self.priority[2] != self.appid
        ):
            raise InputValidationError("priority must be a valid source/rank/AppID tuple")
        if not isinstance(self.source_ranks, Mapping):
            raise InputValidationError("source_ranks must be a mapping")
        frozen_ranks: dict[str, MetricObservation] = {}
        for metric_name, observation in self.source_ranks.items():
            if not isinstance(metric_name, str) or not metric_name:
                raise InputValidationError("source-rank names must be non-empty strings")
            if not isinstance(observation, MetricObservation):
                raise InputValidationError(
                    "source-rank values must be MetricObservation values"
                )
            frozen_ranks[metric_name] = observation
        names = _immutable_strings(
            self.source_names,
            "source_names",
            require_item=True,
        )
        object.__setattr__(self, "source_ranks", MappingProxyType(frozen_ranks))
        object.__setattr__(self, "source_names", names)


@dataclass(frozen=True)
class AppIdentity:
    appid: int
    name: str
    app_type: str
    release_status: str
    release_date: str | None
    genres: Sequence[str]
    observed_at: str

    def __post_init__(self) -> None:
        _steam_appid(self.appid)
        _non_empty_string(self.name)
        _non_empty_string(self.app_type)
        if self.release_status not in _RELEASE_STATUSES:
            raise InputValidationError("release_status is invalid")
        if self.release_date is not None:
            try:
                datetime.strptime(self.release_date, "%Y-%m-%d")
            except (TypeError, ValueError) as error:
                raise InputValidationError("release_date must be an ISO date") from error
        genres = _immutable_strings(self.genres, "genres")
        _validate_observed_at(self.observed_at)
        object.__setattr__(self, "genres", genres)


@dataclass(frozen=True)
class CollectionResult:
    released: Sequence[GameRecord]
    unreleased: Sequence[GameRecord]
    capabilities: Mapping[str, bool]
    warnings: Sequence[WarningRecord]
    raw: Mapping[str, object]

    def __post_init__(self) -> None:
        released = _immutable_records(self.released, "released")
        unreleased = _immutable_records(self.unreleased, "unreleased")
        capabilities = _immutable_capabilities(self.capabilities)
        warnings = _immutable_warnings(self.warnings)
        raw = _immutable_raw(self.raw)
        object.__setattr__(self, "released", released)
        object.__setattr__(self, "unreleased", unreleased)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "raw", raw)

    def raw_to_dict(self) -> dict[str, object]:
        """Return a fresh JSON-native copy suitable for artifact persistence."""

        return {
            key: _thaw_raw_json(payload)
            for key, payload in self.raw.items()
        }


def build_released_candidates(
    most_played: Sequence[DiscoveryCandidate],
    featured: Mapping[str, Sequence[DiscoveryCandidate]],
    limit: int,
) -> Sequence[DiscoveryCandidate]:
    """Build the capped released discovery union in source-priority order."""

    maximum = _positive_limit(limit)
    chart = _candidate_sequence(most_played, "most_played")
    categories = _candidate_mapping(featured)
    sources = (
        chart,
        _featured_candidates(categories, "top_sellers"),
        _featured_candidates(categories, "new_releases"),
    )
    return _ordered_candidate_union(sources)[:maximum]


def build_unreleased_candidates(
    featured: Mapping[str, Sequence[DiscoveryCandidate]],
    limit: int,
) -> Sequence[DiscoveryCandidate]:
    """Build the capped coming-soon discovery union."""

    maximum = _positive_limit(limit)
    categories = _candidate_mapping(featured)
    sources = (_featured_candidates(categories, "coming_soon"),)
    return _ordered_candidate_union(sources)[:maximum]


def collect_official(
    client: JsonHttpClient,
    config: RadarConfig,
    observed_at: str,
) -> CollectionResult:
    """Collect official Steam candidates without leaking provider failures."""

    _validate_collection_inputs(client, config, observed_at)
    capabilities = {name: True for name in _CAPABILITY_NAMES}
    warnings: list[WarningRecord] = []
    raw: dict[str, object] = {}

    most_played = _collect_most_played(
        client,
        observed_at,
        capabilities,
        warnings,
        raw,
    )
    featured = _collect_featured(
        client,
        config,
        observed_at,
        capabilities,
        warnings,
        raw,
    )
    released_candidates = build_released_candidates(
        most_played,
        featured,
        config.released_candidate_limit,
    )
    unreleased_candidates = build_unreleased_candidates(
        featured,
        config.unreleased_candidate_limit,
    )
    unique_candidates = _merge_candidate_pools(
        released_candidates,
        unreleased_candidates,
    )
    released, unreleased = _collect_appdetails(
        client,
        config,
        observed_at,
        unique_candidates,
        capabilities,
        warnings,
        raw,
    )
    released = _collect_current_players(
        client,
        config,
        observed_at,
        released,
        capabilities,
        warnings,
        raw,
    )
    return CollectionResult(
        released=released,
        unreleased=unreleased,
        capabilities=capabilities,
        warnings=warnings,
        raw=raw,
    )


def _collect_most_played(
    client: JsonHttpClient,
    observed_at: str,
    capabilities: dict[str, bool],
    warnings: list[WarningRecord],
    raw: dict[str, object],
) -> Sequence[DiscoveryCandidate]:
    try:
        payload = client.get_json(MOST_PLAYED_URL)
    except ProviderUnavailableError:
        capabilities["most_played"] = False
        warnings.append(
            WarningRecord(
                code="steam_most_played_unavailable",
                message="Steam most-played data is unavailable.",
            )
        )
        return ()
    raw["most_played"] = payload
    parsed = parse_most_played(payload, observed_at)
    warnings.extend(parsed.warnings)
    if parsed.warnings:
        capabilities["most_played"] = False
    return parsed.value


def _collect_featured(
    client: JsonHttpClient,
    config: RadarConfig,
    observed_at: str,
    capabilities: dict[str, bool],
    warnings: list[WarningRecord],
    raw: dict[str, object],
) -> Mapping[str, Sequence[DiscoveryCandidate]]:
    url = FEATURED_CATEGORIES_URL.format(
        country=config.country,
        language=config.language,
    )
    try:
        payload = client.get_json(url)
    except ProviderUnavailableError:
        capabilities["featured_categories"] = False
        warnings.append(
            WarningRecord(
                code="steam_featured_categories_unavailable",
                message="Steam featured-categories data is unavailable.",
            )
        )
        return _empty_featured()
    raw["featured_categories"] = payload
    parsed = parse_featured(payload, observed_at)
    warnings.extend(parsed.warnings)
    if parsed.warnings:
        capabilities["featured_categories"] = False
    return parsed.value


def _collect_appdetails(
    client: JsonHttpClient,
    config: RadarConfig,
    observed_at: str,
    candidates: Sequence[DiscoveryCandidate],
    capabilities: dict[str, bool],
    warnings: list[WarningRecord],
    raw: dict[str, object],
) -> tuple[tuple[GameRecord, ...], tuple[GameRecord, ...]]:
    released: list[GameRecord] = []
    unreleased: list[GameRecord] = []
    for candidate_value in candidates:
        appid = _steam_appid(candidate_value.appid)
        url = APPDETAILS_URL.format(
            appid=appid,
            country=config.country,
            language=config.language,
        )
        try:
            payload = client.get_json(url)
        except ProviderUnavailableError:
            capabilities["appdetails"] = False
            warnings.append(
                WarningRecord(
                    code="steam_appdetails_unavailable",
                    message="Steam app-details data is unavailable.",
                    appid=appid,
                )
            )
            continue
        raw[f"appdetails_{appid}"] = payload
        parsed = parse_appdetails(appid, payload, observed_at)
        warnings.extend(parsed.warnings)
        identity = parsed.value
        if identity is None:
            capabilities["appdetails"] = False
            continue
        if identity.app_type != "game":
            warnings.append(_app_type_warning(identity))
            continue
        if identity.release_status == "unknown":
            warnings.append(
                WarningRecord(
                    code="steam_release_status_unknown_excluded",
                    message="Steam app with an unknown release status was excluded.",
                    appid=appid,
                )
            )
            continue
        record = _game_record(candidate_value, identity)
        if identity.release_status == "released":
            released.append(record)
        else:
            unreleased.append(record)
    return tuple(released), tuple(unreleased)


def _collect_current_players(
    client: JsonHttpClient,
    config: RadarConfig,
    observed_at: str,
    records: Sequence[GameRecord],
    capabilities: dict[str, bool],
    warnings: list[WarningRecord],
    raw: dict[str, object],
) -> tuple[GameRecord, ...]:
    enriched: list[GameRecord] = []
    request_limit = config.released_candidate_limit
    for position, record in enumerate(records):
        if position >= request_limit:
            enriched.append(record)
            continue
        appid = _steam_appid(record.appid)
        url = CURRENT_PLAYERS_URL.format(appid=appid)
        try:
            payload = client.get_json(url)
        except ProviderUnavailableError:
            capabilities["current_players"] = False
            warnings.append(
                WarningRecord(
                    code="steam_current_players_unavailable",
                    message="Steam current-player data is unavailable.",
                    appid=appid,
                )
            )
            enriched.append(record)
            continue
        raw[f"current_players_{appid}"] = payload
        parsed = parse_current_players(appid, payload, observed_at)
        warnings.extend(parsed.warnings)
        if parsed.warnings:
            capabilities["current_players"] = False
        observation = parsed.value
        if observation is None:
            enriched.append(record)
            continue
        metrics = dict(record.metrics)
        metrics["current_players"] = observation
        enriched.append(
            GameRecord(
                schema_version=record.schema_version,
                appid=record.appid,
                name=record.name,
                release_status=record.release_status,
                store_url=record.store_url,
                metrics=metrics,
                source_extra=cast(
                    Mapping[str, object],
                    record.to_dict()["source_extra"],
                ),
            )
        )
    return tuple(enriched)


def _game_record(
    candidate_value: DiscoveryCandidate,
    identity: AppIdentity,
) -> GameRecord:
    appid = _steam_appid(identity.appid)
    metrics = dict(candidate_value.source_ranks)
    if identity.release_date is not None:
        metrics["release_date"] = MetricObservation(
            value=identity.release_date,
            source_id=STEAM_APPDETAILS_SOURCE_ID,
            source_kind="steam_official",
            observed_at=identity.observed_at,
        )
    return GameRecord(
        schema_version=1,
        appid=appid,
        name=identity.name,
        release_status=cast(str, identity.release_status),
        store_url=f"https://store.steampowered.com/app/{appid}/",
        metrics=metrics,
        source_extra={
            "app_type": identity.app_type,
            "genres": list(identity.genres),
            "discovery_sources": list(candidate_value.source_names),
        },
    )


def _app_type_warning(identity: AppIdentity) -> WarningRecord:
    code, message = _APP_TYPE_EXCLUSIONS.get(
        identity.app_type,
        (
            "steam_app_type_unknown_excluded",
            "Steam app with an unknown type was excluded.",
        ),
    )
    return WarningRecord(code=code, message=message, appid=identity.appid)


def _merge_candidate_pools(
    released: Sequence[DiscoveryCandidate],
    unreleased: Sequence[DiscoveryCandidate],
) -> tuple[DiscoveryCandidate, ...]:
    return _merge_candidate_rows((*released, *unreleased))


def _ordered_candidate_union(
    sources: Sequence[Sequence[DiscoveryCandidate]],
) -> tuple[DiscoveryCandidate, ...]:
    ordered_rows: list[DiscoveryCandidate] = []
    for raw_source in sources:
        ordered_rows.extend(
            sorted(
                raw_source,
                key=lambda row: (row.priority[1], row.appid),
            )
        )
    return _merge_candidate_rows(ordered_rows)


def _merge_candidate_rows(
    rows: Sequence[DiscoveryCandidate],
) -> tuple[DiscoveryCandidate, ...]:
    ordered_appids: list[int] = []
    candidates: dict[int, dict[str, object]] = {}
    for row in rows:
        state = candidates.get(row.appid)
        if state is None:
            state = {
                "priority": row.priority,
                "source_ranks": {},
                "source_names": [],
            }
            candidates[row.appid] = state
            ordered_appids.append(row.appid)
        ranks = cast(dict[str, MetricObservation], state["source_ranks"])
        for name, observation in row.source_ranks.items():
            existing = ranks.get(name)
            ranks[name] = (
                observation
                if existing is None
                else _select_observation(name, existing, observation)
            )
        names = cast(list[str], state["source_names"])
        for name in row.source_names:
            if name not in names:
                names.append(name)
    return tuple(
        DiscoveryCandidate(
            appid=appid,
            priority=cast(tuple[int, int, int], candidates[appid]["priority"]),
            source_ranks=_ordered_source_ranks(candidates[appid]),
            source_names=_ordered_source_names(
                cast(list[str], candidates[appid]["source_names"])
            ),
        )
        for appid in ordered_appids
    )


def _ordered_source_ranks(
    candidate_state: Mapping[str, object],
) -> Mapping[str, MetricObservation]:
    ranks = cast(dict[str, MetricObservation], candidate_state["source_ranks"])
    return {name: ranks[name] for name in sorted(ranks)}


def _select_observation(
    metric_name: str,
    first: MetricObservation,
    second: MetricObservation,
) -> MetricObservation:
    if first == second:
        return first
    observations = (first, second)
    numeric = tuple(
        (observation, _numeric_observation_value(observation))
        for observation in observations
    )
    valid_numeric = tuple(
        (observation, value)
        for observation, value in numeric
        if value is not None
    )
    if metric_name.endswith("_rank") and valid_numeric:
        best_value = min(value for _, value in valid_numeric)
        finalists = tuple(
            observation
            for observation, value in valid_numeric
            if value == best_value
        )
        return min(finalists, key=_canonical_observation)
    if _is_player_count_metric(metric_name) and valid_numeric:
        best_value = max(value for _, value in valid_numeric)
        finalists = tuple(
            observation
            for observation, value in valid_numeric
            if value == best_value
        )
        return min(finalists, key=_canonical_observation)
    return min(observations, key=_canonical_observation)


def _numeric_observation_value(
    observation: MetricObservation,
) -> int | float | None:
    value = observation.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _is_player_count_metric(metric_name: str) -> bool:
    return (
        metric_name in {"peak_players", "current_players", "player_count"}
        or metric_name.endswith("_players")
        or metric_name.endswith("_player_count")
    )


def _canonical_observation(observation: MetricObservation) -> str:
    return json.dumps(
        observation.to_dict(),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _ordered_source_names(names: Sequence[str]) -> tuple[str, ...]:
    known_positions = {
        name: position for position, name in enumerate(_DISCOVERY_SOURCE_ORDER)
    }
    return tuple(
        sorted(
            set(names),
            key=lambda name: (
                known_positions.get(name, len(known_positions)),
                "" if name in known_positions else name,
            ),
        )
    )


def _validate_collection_inputs(
    client: object,
    config: object,
    observed_at: object,
) -> None:
    if not callable(getattr(client, "get_json", None)):
        raise InputValidationError("client must provide get_json")
    if not isinstance(config, RadarConfig):
        raise InputValidationError("config must be a RadarConfig")
    if not isinstance(observed_at, str):
        raise InputValidationError("observed_at must be a UTC timestamp")
    _validate_observed_at(observed_at)


def _candidate_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InputValidationError("featured must be a mapping")
    return cast(Mapping[str, object], value)


def _featured_candidates(
    featured: Mapping[str, object],
    category: str,
) -> tuple[DiscoveryCandidate, ...]:
    if category not in featured:
        raise InputValidationError(f"featured is missing {category}")
    return _candidate_sequence(featured[category], f"featured[{category!r}]")


def _candidate_sequence(
    value: object,
    field_name: str,
) -> tuple[DiscoveryCandidate, ...]:
    if not _is_sequence(value):
        raise InputValidationError(f"{field_name} must be a candidate sequence")
    candidates = tuple(cast(Sequence[object], value))
    if not all(isinstance(row, DiscoveryCandidate) for row in candidates):
        raise InputValidationError(
            f"{field_name} must contain DiscoveryCandidate values"
        )
    return cast(tuple[DiscoveryCandidate, ...], candidates)


def _positive_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InputValidationError("limit must be a positive integer")
    return value


def _immutable_records(value: object, name: str) -> tuple[GameRecord, ...]:
    if not _is_sequence(value):
        raise InputValidationError(f"{name} must be a sequence of GameRecord values")
    records = tuple(cast(Sequence[object], value))
    if not all(isinstance(record, GameRecord) for record in records):
        raise InputValidationError(f"{name} must contain GameRecord values")
    return cast(tuple[GameRecord, ...], records)


def _immutable_capabilities(value: object) -> Mapping[str, bool]:
    if not isinstance(value, Mapping) or set(value) != set(_CAPABILITY_NAMES):
        raise InputValidationError("capabilities must contain the four official providers")
    capabilities: dict[str, bool] = {}
    for name in _CAPABILITY_NAMES:
        available = value[name]
        if not isinstance(available, bool):
            raise InputValidationError("capability values must be booleans")
        capabilities[name] = available
    return MappingProxyType(capabilities)


def _immutable_warnings(value: object) -> tuple[WarningRecord, ...]:
    if not _is_sequence(value):
        raise InputValidationError("warnings must be a sequence")
    warnings = tuple(cast(Sequence[object], value))
    if not all(isinstance(warning, WarningRecord) for warning in warnings):
        raise InputValidationError("warnings must contain WarningRecord values")
    return cast(tuple[WarningRecord, ...], warnings)


def _immutable_raw(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InputValidationError("raw must be a mapping")
    frozen: dict[str, object] = {}
    for key, payload in value.items():
        if not isinstance(key, str) or not key:
            raise InputValidationError("raw keys must be non-empty strings")
        frozen[key] = _freeze_raw_json(payload)
    return MappingProxyType(frozen)


def _freeze_raw_json(value: object) -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if value < MIN_JSON_SAFE_INTEGER or value > MAX_JSON_SAFE_INTEGER:
            raise InputValidationError(
                "raw integers must be within the JSON-safe range"
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InputValidationError("raw values must be JSON-compatible")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise InputValidationError("raw mappings must use string keys")
            frozen[key] = _freeze_raw_json(nested)
        return MappingProxyType(frozen)
    if _is_sequence(value):
        return tuple(_freeze_raw_json(item) for item in cast(Sequence[object], value))
    raise InputValidationError("raw values must be JSON-compatible")


def _thaw_raw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _thaw_raw_json(nested)
            for key, nested in value.items()
        }
    if isinstance(value, tuple):
        return [_thaw_raw_json(item) for item in value]
    return value


def parse_most_played(
    payload: object,
    observed_at: str,
) -> ParseResult[Sequence[DiscoveryCandidate]]:
    """Parse the most-played chart without returning partial capabilities."""

    try:
        _validate_observed_at(observed_at)
        root = _required_mapping(payload)
        response = _required_mapping(root.get("response"))
        ranks = _required_sequence(response.get("ranks"))
        candidates: list[DiscoveryCandidate] = []
        for raw_row in ranks:
            row = _required_mapping(raw_row)
            appid = _steam_appid(row.get("appid"))
            rank = _positive_integer(row.get("rank"))
            metrics = {
                "most_played_rank": _observation(
                    rank,
                    STEAM_MOST_PLAYED_RANK_SOURCE_ID,
                    observed_at,
                )
            }
            if "last_week_rank" in row:
                previous_rank = row["last_week_rank"]
                if (
                    isinstance(previous_rank, bool)
                    or not isinstance(previous_rank, int)
                    or previous_rank == 0
                    or previous_rank < -1
                ):
                    raise _MalformedPayload
                if previous_rank > 0:
                    metrics["previous_rank"] = _observation(
                        previous_rank,
                        STEAM_PREVIOUS_RANK_SOURCE_ID,
                        observed_at,
                    )
            if "peak_in_game" in row:
                peak_players = _nonnegative_integer(row["peak_in_game"])
                metrics["peak_players"] = _observation(
                    peak_players,
                    STEAM_PEAK_PLAYERS_SOURCE_ID,
                    observed_at,
                )
            candidates.append(
                DiscoveryCandidate(
                    appid=appid,
                    priority=(0, rank, appid),
                    source_ranks=metrics,
                    source_names=("most_played",),
                )
            )
        return ParseResult(
            value=tuple(sorted(candidates, key=lambda candidate: candidate.priority)),
            warnings=(),
        )
    except (KeyError, TypeError, ValueError, RecursionError, InputValidationError):
        return ParseResult(value=(), warnings=(_MOST_PLAYED_WARNING,))


def parse_featured(
    payload: object,
    observed_at: str,
) -> ParseResult[Mapping[str, Sequence[DiscoveryCandidate]]]:
    """Parse the three featured categories as one atomic capability."""

    empty = _empty_featured()
    try:
        _validate_observed_at(observed_at)
        root = _required_mapping(payload)
        parsed: dict[str, tuple[DiscoveryCandidate, ...]] = {}
        for category in _FEATURED_CATEGORIES:
            category_payload = _required_mapping(root.get(category))
            raw_items = category_payload.get("items", ())
            items = _required_sequence(raw_items)
            source_priority, metric_name, source_id = _FEATURED_METRICS[category]
            candidates: list[DiscoveryCandidate] = []
            for rank, raw_item in enumerate(items, start=1):
                item = _required_mapping(raw_item)
                appid = _steam_appid(item.get("id"))
                candidates.append(
                    DiscoveryCandidate(
                        appid=appid,
                        priority=(source_priority, rank, appid),
                        source_ranks={
                            metric_name: _observation(rank, source_id, observed_at)
                        },
                        source_names=(category,),
                    )
                )
            parsed[category] = tuple(
                sorted(candidates, key=lambda candidate: candidate.priority)
            )
        return ParseResult(value=parsed, warnings=())
    except (KeyError, TypeError, ValueError, RecursionError, InputValidationError):
        return ParseResult(value=empty, warnings=(_FEATURED_WARNING,))


def parse_appdetails(
    appid: int,
    payload: object,
    observed_at: str,
) -> ParseResult[AppIdentity | None]:
    """Parse exactly one requested AppID from the app-details response."""

    try:
        requested_appid = _steam_appid(appid)
        _validate_observed_at(observed_at)
        root = _required_mapping(payload)
        entry = _required_mapping(root.get(str(requested_appid)))
        if entry.get("success") is not True:
            raise _MalformedPayload
        data = _required_mapping(entry.get("data"))
        if _steam_appid(data.get("steam_appid")) != requested_appid:
            raise _MalformedPayload
        name = _non_empty_string(data.get("name"))
        app_type = _non_empty_string(data.get("type"))
        release_status, release_date, date_unparsed = _parse_release_date(
            data.get("release_date")
        )
        genres = _parse_genres(data.get("genres"))
        identity = AppIdentity(
            appid=requested_appid,
            name=name,
            app_type=app_type,
            release_status=release_status,
            release_date=release_date,
            genres=genres,
            observed_at=observed_at,
        )
        warnings: tuple[WarningRecord, ...] = ()
        if date_unparsed:
            warnings = (
                WarningRecord(
                    code="steam_appdetails_release_date_unparsed",
                    message=(
                        "Steam app-details release date could not be normalized."
                    ),
                    appid=requested_appid,
                ),
            )
        return ParseResult(value=identity, warnings=warnings)
    except (KeyError, TypeError, ValueError, RecursionError, InputValidationError):
        return ParseResult(
            value=None,
            warnings=(
                WarningRecord(
                    code="steam_appdetails_malformed",
                    message="Steam app-details response is malformed.",
                    appid=_warning_appid(appid),
                ),
            ),
        )


def parse_current_players(
    appid: int,
    payload: object,
    observed_at: str,
) -> ParseResult[MetricObservation | None]:
    """Parse one current-player observation, preserving a missing count."""

    try:
        requested_appid = _steam_appid(appid)
        _validate_observed_at(observed_at)
        root = _required_mapping(payload)
        response = _required_mapping(root.get("response"))
        if "result" in response and (
            isinstance(response["result"], bool)
            or not isinstance(response["result"], int)
            or response["result"] != 1
        ):
            raise _MalformedPayload
        if "player_count" not in response:
            return ParseResult(value=None, warnings=())
        count = _nonnegative_integer(response["player_count"])
        return ParseResult(
            value=_observation(count, STEAM_CURRENT_PLAYERS_SOURCE_ID, observed_at),
            warnings=(),
        )
    except (KeyError, TypeError, ValueError, RecursionError, InputValidationError):
        return ParseResult(
            value=None,
            warnings=(
                WarningRecord(
                    code="steam_current_players_malformed",
                    message="Steam current-players response is malformed.",
                    appid=_warning_appid(appid),
                ),
            ),
        )


def _parse_release_date(value: object) -> tuple[str, str | None, bool]:
    if not isinstance(value, Mapping):
        return "unknown", None, _has_nonempty_date(value)
    coming_soon = value.get("coming_soon")
    if not isinstance(coming_soon, bool):
        return "unknown", None, _has_nonempty_date(value.get("date"))
    status = "unreleased" if coming_soon else "released"
    raw_date = value.get("date")
    if not isinstance(raw_date, str) or not raw_date.strip():
        return status, None, _has_nonempty_date(raw_date)
    date_text = raw_date.strip()
    for date_format in ("%Y-%m-%d", "%d %b, %Y", "%b %d, %Y"):
        try:
            normalized = datetime.strptime(date_text, date_format).date().isoformat()
            return status, normalized, False
        except ValueError:
            continue
    for pattern in _LOCALIZED_DATE_PATTERNS:
        match = pattern.fullmatch(date_text)
        if match is None:
            continue
        try:
            normalized = date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            ).isoformat()
        except ValueError:
            return status, None, True
        return status, normalized, False
    alphabetic = _normalize_alphabetic_date(date_text)
    if alphabetic is not None:
        return status, alphabetic, False
    return status, None, True


def _normalize_alphabetic_date(value: str) -> str | None:
    """Normalize only explicitly allowlisted localized month-name dates."""

    for pattern in _ALPHABETIC_DATE_PATTERNS:
        match = pattern.fullmatch(value)
        if match is None:
            continue
        month_token = unicodedata.normalize(
            "NFKD", match.group("month").casefold()
        )
        month_alias = "".join(
            character
            for character in month_token
            if not unicodedata.combining(character)
        )
        month = _MONTH_ALIASES.get(month_alias)
        if month is None:
            return None
        try:
            return date(
                int(match.group("year")),
                month,
                int(match.group("day")),
            ).isoformat()
        except ValueError:
            return None
    return None


def _has_nonempty_date(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (bytes, bytearray, Mapping, Sequence)):
        return len(value) > 0
    return True


def _parse_genres(value: object) -> tuple[str, ...]:
    if value is None or not _is_sequence(value):
        return ()
    genres: list[str] = []
    for raw_genre in cast(Sequence[object], value):
        if not isinstance(raw_genre, Mapping):
            continue
        description = raw_genre.get("description")
        if isinstance(description, str) and description.strip():
            genres.append(description)
    return tuple(genres)


def _observation(
    value: int,
    source_id: str,
    observed_at: str,
) -> MetricObservation:
    return MetricObservation(
        value=value,
        source_id=source_id,
        source_kind="steam_official",
        observed_at=observed_at,
    )


def _validate_observed_at(observed_at: str) -> None:
    _observation(0, STEAM_APPDETAILS_SOURCE_ID, observed_at)


def _required_mapping(value: object) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise _MalformedPayload
    return value


def _required_sequence(value: object) -> Sequence[object]:
    if not _is_sequence(value):
        raise _MalformedPayload
    return cast(Sequence[object], value)


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _MalformedPayload
    return value


def _steam_appid(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > MAX_STEAM_APPID
    ):
        raise InputValidationError(
            f"appid must be an integer from 1 through {MAX_STEAM_APPID}"
        )
    return value


def _nonnegative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _MalformedPayload
    return value


def _non_empty_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _MalformedPayload
    return value


def _warning_appid(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > MAX_STEAM_APPID
    ):
        return None
    return value


def _empty_featured() -> Mapping[str, tuple[DiscoveryCandidate, ...]]:
    return {category: () for category in _FEATURED_CATEGORIES}


def _immutable_strings(
    value: object,
    field_name: str,
    *,
    require_item: bool = False,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise InputValidationError(f"{field_name} must be a sequence of strings")
    items = tuple(value)
    if require_item and not items:
        raise InputValidationError(f"{field_name} must not be empty")
    if any(not isinstance(item, str) or not item for item in items):
        raise InputValidationError(
            f"{field_name} must contain non-empty strings"
        )
    return items


def _freeze_container(value: T) -> T:
    if isinstance(value, Mapping):
        return cast(
            T,
            MappingProxyType(
                {key: _freeze_container(nested) for key, nested in value.items()}
            ),
        )
    if _is_sequence(value):
        return cast(T, tuple(_freeze_container(item) for item in value))
    return value
