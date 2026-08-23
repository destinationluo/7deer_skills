"""Pure parsers for the official Steam discovery and app endpoints."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import re
from types import MappingProxyType
from typing import Generic, TypeVar, cast
import unicodedata

from .errors import InputValidationError
from .schemas import MetricObservation, WarningRecord


STEAM_MOST_PLAYED_RANK_SOURCE_ID = "steam_most_played_rank"
STEAM_PREVIOUS_RANK_SOURCE_ID = "steam_previous_rank"
STEAM_TOP_SELLER_RANK_SOURCE_ID = "steam_top_seller_rank"
STEAM_NEW_RELEASE_RANK_SOURCE_ID = "steam_new_release_rank"
STEAM_COMING_SOON_RANK_SOURCE_ID = "steam_coming_soon_rank"
STEAM_PEAK_PLAYERS_SOURCE_ID = "steam_peak_players"
STEAM_CURRENT_PLAYERS_SOURCE_ID = "steam_current_players"
STEAM_APPDETAILS_SOURCE_ID = "steam_appdetails"

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
        _positive_integer(self.appid)
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
        _positive_integer(self.appid)
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
            appid = _positive_integer(row.get("appid"))
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
                appid = _positive_integer(item.get("id"))
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
        requested_appid = _positive_integer(appid)
        _validate_observed_at(observed_at)
        root = _required_mapping(payload)
        entry = _required_mapping(root.get(str(requested_appid)))
        if entry.get("success") is not True:
            raise _MalformedPayload
        data = _required_mapping(entry.get("data"))
        if _positive_integer(data.get("steam_appid")) != requested_appid:
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
        requested_appid = _positive_integer(appid)
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


def _nonnegative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _MalformedPayload
    return value


def _non_empty_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _MalformedPayload
    return value


def _warning_appid(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
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
