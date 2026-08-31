"""Strict ingestion for Agent-collected Roblox discovery rows.

The browser remains outside this module. This boundary accepts public visible
facts, validates their Roblox provenance, and produces immutable platform
observations. Historical deltas, heat, opportunity scores, and actions are
deliberately calculated elsewhere.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import re
import unicodedata
from urllib.parse import urlsplit

from ..errors import InputValidationError
from ..schemas import PlatformObservation, RadarRun


MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 200
MAX_SAFE_INTEGER = 2**53 - 1

_ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "collector",
        "geo",
        "locale",
        "metric_definition_version",
        "observed_at",
        "rows",
    }
)
_ROW_KEYS = frozenset(
    {
        "universe_id",
        "place_id",
        "name",
        "developer",
        "game_url",
        "surface",
        "surface_scope",
        "rank",
        "concurrent_players",
        "visits",
        "favorites",
        "observed_at",
        "evidence_url",
    }
)
_SURFACES = frozenset({"rising", "up-and-coming", "charts"})
_SURFACE_SCOPES = frozenset({"global", "personalized"})
_COUNTRY = re.compile(r"[A-Z]{2}\Z", flags=re.ASCII)
_LOCALE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*\Z")
_UTC_SECONDS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_ROBLOX_HOSTS = frozenset({"roblox.com", "www.roblox.com"})
_EVIDENCE_PATHS = {
    "rising": "/charts/top-trending",
    "up-and-coming": "/charts/top-up-and-coming",
    "charts": "/charts/top-playing-now",
}
_EVIDENCE_URLS = {
    surface: frozenset(
        f"https://{host}{path}" for host in _ROBLOX_HOSTS
    )
    for surface, path in _EVIDENCE_PATHS.items()
}
_GAME_PATH = re.compile(
    r"/games/(?P<place_id>[1-9][0-9]*)/"
    r"(?P<slug>[A-Za-z0-9](?:[A-Za-z0-9_-]{0,254}[A-Za-z0-9])?)\Z",
    flags=re.ASCII,
)


def _invalid(message: str) -> InputValidationError:
    return InputValidationError(message)


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _invalid(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _materialize_payload(value: object) -> Mapping[str, object]:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        try:
            raw = value.encode("utf-8")
        except UnicodeError as error:
            raise _invalid("Roblox envelope must be valid UTF-8") from error
    elif isinstance(value, Mapping):
        try:
            raw = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (
            TypeError,
            ValueError,
            OverflowError,
            UnicodeError,
            RecursionError,
        ) as error:
            raise _invalid(
                "Roblox envelope must contain JSON-compatible data"
            ) from error
    else:
        raise _invalid("Roblox envelope must be a JSON object, string, or bytes")

    if len(raw) > MAX_ENVELOPE_BYTES:
        raise _invalid(
            f"Roblox envelope must not exceed {MAX_ENVELOPE_BYTES} UTF-8 bytes"
        )
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_json_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise _invalid("Roblox envelope must be valid UTF-8 JSON") from error
    if not isinstance(parsed, Mapping):
        raise _invalid("Roblox envelope must contain a JSON object")
    return parsed


def _exact_keys(
    value: object,
    expected: frozenset[str],
    owner: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid(f"{owner} must be a JSON object")
    actual = set(value)
    unexpected = actual - expected
    if unexpected:
        names = ", ".join(sorted(str(name) for name in unexpected))
        raise _invalid(f"{owner} has unexpected keys: {names}")
    missing = expected - actual
    if missing:
        names = ", ".join(sorted(missing))
        raise _invalid(f"{owner} is missing keys: {names}")
    return value


def _text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _invalid(f"{name} must be nonempty text without surrounding whitespace")
    if "\x00" in value or len(value) > maximum:
        raise _invalid(f"{name} must not exceed {maximum} characters")
    return value


def _visible_text(value: object, name: str, maximum: int) -> str:
    parsed = _text(value, name, maximum)
    if any(unicodedata.category(character).startswith("C") for character in parsed):
        raise _invalid(f"{name} must not contain Unicode other-category characters")
    return parsed


def _literal(
    value: object,
    name: str,
    choices: frozenset[str],
) -> str:
    parsed = _text(value, name, 64)
    if parsed not in choices:
        allowed = ", ".join(sorted(choices))
        raise _invalid(f"{name} must be one of: {allowed}")
    return parsed


def _integer(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int:
        raise _invalid(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise _invalid(f"{name} must be between {minimum} and {maximum}")
    return value


def _positive_id(value: object, name: str) -> int:
    return _integer(value, name, minimum=1, maximum=MAX_SAFE_INTEGER)


def _positive_id_text(value: object, name: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise _invalid(f"{name} must be a positive ASCII integer")
    maximum = str(MAX_SAFE_INTEGER)
    if len(value) > len(maximum) or (
        len(value) == len(maximum) and value > maximum
    ):
        raise _invalid(f"{name} exceeds the JSON safe integer limit")
    return int(value)


def _metric(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _integer(value, name, minimum=0, maximum=MAX_SAFE_INTEGER)


def _utc_seconds(value: object, name: str) -> datetime:
    parsed = _text(value, name, 20)
    if _UTC_SECONDS.fullmatch(parsed) is None:
        raise _invalid(f"{name} must use canonical second-precision UTC format")
    try:
        instant = datetime.strptime(parsed, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise _invalid(f"{name} must be a valid UTC timestamp") from error
    if instant.strftime("%Y-%m-%dT%H:%M:%SZ") != parsed:
        raise _invalid(f"{name} must be a canonical UTC timestamp")
    return instant.replace(tzinfo=timezone.utc)


def _utc_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise _invalid(f"{name} must be a UTC datetime")
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise _invalid(f"{name} must be a UTC datetime")
    if value.microsecond:
        raise _invalid(f"{name} must use second precision")
    return value.astimezone(timezone.utc)


def _roblox_url(value: object, name: str) -> str:
    parsed = _text(value, name, 8192)
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in parsed):
        raise _invalid(f"{name} must not contain control or format characters")
    try:
        split = urlsplit(parsed)
        hostname = split.hostname
        port = split.port
    except ValueError as error:
        raise _invalid(f"{name} must be a valid Roblox HTTPS URL") from error
    if (
        split.scheme != "https"
        or not split.netloc
        or hostname not in _ROBLOX_HOSTS
        or split.username is not None
        or split.password is not None
        or port is not None
        or split.netloc != split.netloc.lower()
    ):
        raise _invalid(f"{name} must use HTTPS on an allowed Roblox host")
    return parsed


def _game_url(value: object, place_id: int) -> str:
    parsed = _roblox_url(value, "game_url")
    split = urlsplit(parsed)
    match = _GAME_PATH.fullmatch(split.path)
    if match is None or split.query or split.fragment:
        raise _invalid("game_url must be a canonical Roblox game URL")
    path_place_id = _positive_id_text(
        match.group("place_id"),
        "game_url place ID",
    )
    if path_place_id != place_id:
        raise _invalid("game_url place ID must match place_id")
    return parsed


def _evidence_url(value: object, surface: str) -> str:
    parsed = _roblox_url(value, "evidence_url")
    if parsed not in _EVIDENCE_URLS[surface]:
        raise _invalid(
            "evidence_url must be the exact Roblox chart URL for its surface"
        )
    return parsed


@dataclass(frozen=True)
class RobloxBrowserRow:
    """One exact visible row collected from a Roblox discovery surface."""

    universe_id: int
    place_id: int
    name: str
    developer: str
    game_url: str
    surface: str
    surface_scope: str
    rank: int
    concurrent_players: int | None
    visits: int | None
    favorites: int | None
    observed_at: datetime
    evidence_url: str

    def __post_init__(self) -> None:
        universe_id = _positive_id(self.universe_id, "universe_id")
        place_id = _positive_id(self.place_id, "place_id")
        object.__setattr__(self, "universe_id", universe_id)
        object.__setattr__(self, "place_id", place_id)
        object.__setattr__(self, "name", _visible_text(self.name, "name", 256))
        object.__setattr__(
            self,
            "developer",
            _visible_text(self.developer, "developer", 256),
        )
        object.__setattr__(self, "game_url", _game_url(self.game_url, place_id))
        surface = _literal(self.surface, "surface", _SURFACES)
        object.__setattr__(self, "surface", surface)
        object.__setattr__(
            self,
            "surface_scope",
            _literal(self.surface_scope, "surface_scope", _SURFACE_SCOPES),
        )
        object.__setattr__(
            self,
            "rank",
            _integer(self.rank, "rank", minimum=1, maximum=MAX_ROWS),
        )
        object.__setattr__(
            self,
            "concurrent_players",
            _metric(self.concurrent_players, "concurrent_players"),
        )
        object.__setattr__(self, "visits", _metric(self.visits, "visits"))
        object.__setattr__(self, "favorites", _metric(self.favorites, "favorites"))
        object.__setattr__(
            self,
            "observed_at",
            _utc_datetime(self.observed_at, "observed_at"),
        )
        object.__setattr__(
            self,
            "evidence_url",
            _evidence_url(self.evidence_url, surface),
        )


@dataclass(frozen=True)
class RobloxBrowserEnvelope:
    """Validated Agent payload for one Roblox collection timestamp."""

    schema_version: int
    run_id: str
    collector: str
    geo: str
    locale: str
    metric_definition_version: int
    observed_at: datetime
    rows: tuple[RobloxBrowserRow, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise _invalid("schema_version must be 1")
        object.__setattr__(self, "run_id", _text(self.run_id, "run_id", 50))
        if self.collector != "roblox":
            raise _invalid("collector must be roblox")
        if not isinstance(self.geo, str) or _COUNTRY.fullmatch(self.geo) is None:
            raise _invalid("geo must be a two-letter uppercase country code")
        if not isinstance(self.locale, str) or _LOCALE.fullmatch(self.locale) is None:
            raise _invalid("locale is invalid")
        if (
            type(self.metric_definition_version) is not int
            or self.metric_definition_version != 1
        ):
            raise _invalid("metric_definition_version must be 1")
        object.__setattr__(
            self,
            "observed_at",
            _utc_datetime(self.observed_at, "observed_at"),
        )
        if not isinstance(self.rows, tuple) or any(
            not isinstance(row, RobloxBrowserRow) for row in self.rows
        ):
            raise _invalid("rows must contain only RobloxBrowserRow records")
        if len(self.rows) > MAX_ROWS:
            raise _invalid(f"rows must not contain more than {MAX_ROWS} records")
        for row in self.rows:
            if row.observed_at != self.observed_at:
                raise _invalid("row observed_at must match envelope observed_at")


def _parse_row(value: object) -> RobloxBrowserRow:
    row = _exact_keys(value, _ROW_KEYS, "Roblox row")
    place_id = _positive_id(row["place_id"], "place_id")
    return RobloxBrowserRow(
        universe_id=row["universe_id"],  # type: ignore[arg-type]
        place_id=place_id,
        name=row["name"],  # type: ignore[arg-type]
        developer=row["developer"],  # type: ignore[arg-type]
        game_url=row["game_url"],  # type: ignore[arg-type]
        surface=row["surface"],  # type: ignore[arg-type]
        surface_scope=row["surface_scope"],  # type: ignore[arg-type]
        rank=row["rank"],  # type: ignore[arg-type]
        concurrent_players=row["concurrent_players"],  # type: ignore[arg-type]
        visits=row["visits"],  # type: ignore[arg-type]
        favorites=row["favorites"],  # type: ignore[arg-type]
        observed_at=_utc_seconds(row["observed_at"], "row observed_at"),
        evidence_url=row["evidence_url"],  # type: ignore[arg-type]
    )


def _validated_row(row: RobloxBrowserRow) -> RobloxBrowserRow:
    """Return a newly validated copy so frozen-object tampering is rejected."""

    if not isinstance(row, RobloxBrowserRow):
        raise _invalid("rows must contain only RobloxBrowserRow records")
    return RobloxBrowserRow(
        universe_id=row.universe_id,
        place_id=row.place_id,
        name=row.name,
        developer=row.developer,
        game_url=row.game_url,
        surface=row.surface,
        surface_scope=row.surface_scope,
        rank=row.rank,
        concurrent_players=row.concurrent_players,
        visits=row.visits,
        favorites=row.favorites,
        observed_at=row.observed_at,
        evidence_url=row.evidence_url,
    )


def _deduplicate_rows(
    rows: Sequence[RobloxBrowserRow],
) -> tuple[RobloxBrowserRow, ...]:
    unique: list[RobloxBrowserRow] = []
    seen: dict[tuple[int, str, datetime], RobloxBrowserRow] = {}
    place_owners: dict[int, int] = {}
    universe_places: dict[int, int] = {}
    occupied_ranks: set[tuple[str, str, int, datetime]] = set()
    for row in rows:
        prior_owner = place_owners.setdefault(row.place_id, row.universe_id)
        if prior_owner != row.universe_id:
            raise _invalid("one Roblox place_id cannot belong to multiple universe IDs")
        prior_place = universe_places.setdefault(row.universe_id, row.place_id)
        if prior_place != row.place_id:
            raise _invalid("one Roblox universe_id cannot use multiple place IDs in a snapshot")

        key = (row.universe_id, row.surface, row.observed_at)
        prior = seen.get(key)
        if prior is not None:
            if prior != row:
                raise _invalid(
                    "conflicting duplicate Roblox row for universe ID, surface, and timestamp"
                )
            continue

        rank_key = (row.surface, row.surface_scope, row.rank, row.observed_at)
        if rank_key in occupied_ranks:
            raise _invalid("Roblox surface ranks must be unique within one scope")
        occupied_ranks.add(rank_key)
        seen[key] = row
        unique.append(row)
    return tuple(unique)


def _validated_envelope(
    run: RadarRun,
    envelope: RobloxBrowserEnvelope,
) -> RobloxBrowserEnvelope:
    """Revalidate all typed values at the run-bound trust boundary."""

    if not isinstance(run, RadarRun):
        raise _invalid("run must be a RadarRun")
    if not isinstance(envelope, RobloxBrowserEnvelope):
        raise _invalid("envelope must be a RobloxBrowserEnvelope")
    if "roblox" not in run.platforms:
        raise _invalid("originating run did not select roblox")
    if not isinstance(envelope.rows, tuple):
        raise _invalid("rows must be an immutable tuple")

    rows = tuple(_validated_row(row) for row in envelope.rows)
    rebuilt = RobloxBrowserEnvelope(
        schema_version=envelope.schema_version,
        run_id=envelope.run_id,
        collector=envelope.collector,
        geo=envelope.geo,
        locale=envelope.locale,
        metric_definition_version=envelope.metric_definition_version,
        observed_at=envelope.observed_at,
        rows=rows,
    )
    if rebuilt.run_id != run.run_id:
        raise _invalid("Roblox envelope run_id must match the originating run")
    if rebuilt.observed_at < run.started_at:
        raise _invalid("Roblox envelope observed_at must not precede run started_at")

    deduplicated = _deduplicate_rows(rebuilt.rows)
    if deduplicated != rebuilt.rows:
        rebuilt = replace(rebuilt, rows=deduplicated)
    return rebuilt


def parse_roblox_envelope(
    value: object,
    run: RadarRun,
) -> RobloxBrowserEnvelope:
    """Parse an untrusted Roblox browser envelope for its originating run."""

    if not isinstance(run, RadarRun):
        raise _invalid("run must be a RadarRun")
    if "roblox" not in run.platforms:
        raise _invalid("originating run did not select roblox")
    payload = _exact_keys(
        _materialize_payload(value),
        _ENVELOPE_KEYS,
        "Roblox envelope",
    )
    if payload["run_id"] != run.run_id:
        raise _invalid("Roblox envelope run_id must match the originating run")
    if not isinstance(payload["rows"], list):
        raise _invalid("rows must be an array")
    if len(payload["rows"]) > MAX_ROWS:
        raise _invalid(f"rows must not contain more than {MAX_ROWS} records")

    envelope = RobloxBrowserEnvelope(
        schema_version=payload["schema_version"],  # type: ignore[arg-type]
        run_id=payload["run_id"],  # type: ignore[arg-type]
        collector=payload["collector"],  # type: ignore[arg-type]
        geo=payload["geo"],  # type: ignore[arg-type]
        locale=payload["locale"],  # type: ignore[arg-type]
        metric_definition_version=payload["metric_definition_version"],  # type: ignore[arg-type]
        observed_at=_utc_seconds(payload["observed_at"], "envelope observed_at"),
        rows=tuple(_parse_row(item) for item in payload["rows"]),
    )
    return _validated_envelope(run, envelope)


def _compact_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_roblox_observations(
    run: RadarRun,
    envelope: RobloxBrowserEnvelope,
) -> tuple[PlatformObservation, ...]:
    """Build deterministic Roblox observations without calculated metrics."""

    validated = _validated_envelope(run, envelope)
    observations: list[PlatformObservation] = []
    for row in validated.rows:
        platform_id = str(row.universe_id)
        observation_id = (
            f"roblox:{platform_id}:{row.surface}:"
            f"{_compact_timestamp(row.observed_at)}"
        )
        global_eligible = row.surface_scope == "global"
        cohort_surface = (
            "roblox_global" if global_eligible else "roblox_personalized"
        )
        observations.append(
            PlatformObservation(
                schema_version=1,
                observation_id=observation_id,
                run_id=run.run_id,
                platform="roblox",
                platform_id=platform_id,
                provider="roblox_agent_browser",
                surface=row.surface,
                geo=validated.geo,
                locale=validated.locale,
                query_parameters={
                    "surface_scope": row.surface_scope,
                    "cohort_surface": cohort_surface,
                },
                metric_definition_version=validated.metric_definition_version,
                observed_at=row.observed_at,
                release_at=None,
                source_rank=row.rank,
                raw_metrics={
                    "universe_id": row.universe_id,
                    "place_id": row.place_id,
                    "name": row.name,
                    "developer": row.developer,
                    "game_url": row.game_url,
                    "concurrent_players": row.concurrent_players,
                    "visits": row.visits,
                    "favorites": row.favorites,
                    "global_cohort_eligible": global_eligible,
                },
                evidence_urls=tuple(
                    dict.fromkeys((row.evidence_url, row.game_url))
                ),
            )
        )
    return tuple(observations)


__all__ = [
    "MAX_ENVELOPE_BYTES",
    "MAX_ROWS",
    "MAX_SAFE_INTEGER",
    "RobloxBrowserEnvelope",
    "RobloxBrowserRow",
    "build_roblox_observations",
    "parse_roblox_envelope",
]
