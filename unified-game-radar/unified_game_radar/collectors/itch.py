"""Strict ingestion for Agent-collected itch.io discovery rows.

The browser is deliberately outside this module.  This boundary accepts only
visible page facts, validates their provenance, and turns them into immutable
platform observations.  It never calculates heat, opportunity scores, or
actions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import re
from urllib.parse import urlsplit

from ..errors import InputValidationError
from ..platform_keys import validate_platform_id
from ..schemas import PlatformObservation, RadarRun


MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 200
MAX_AUTHOR_RELEASE_COUNT = 1_000_000

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
        "title",
        "developer",
        "game_url",
        "surface",
        "surface_scope",
        "rank",
        "browser_playable",
        "genre",
        "is_jam",
        "author_release_count",
        "originality",
        "observed_at",
        "evidence_url",
    }
)
_SURFACES = frozenset({"newest", "popular"})
_ORIGINALITY = frozenset(
    {
        "verified_original",
        "unknown",
        "known_reupload",
        "known_commercial_copy",
        "mass_reupload",
    }
)
_COUNTRY = re.compile(r"[A-Z]{2}\Z", flags=re.ASCII)
_LOCALE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*\Z")
_UTC_SECONDS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


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
            raise _invalid("itch envelope must be valid UTF-8") from error
    elif isinstance(value, Mapping):
        try:
            raw = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError, UnicodeError) as error:
            raise _invalid("itch envelope must contain JSON-compatible data") from error
    else:
        raise _invalid("itch envelope must be a JSON object, string, or bytes")

    if len(raw) > MAX_ENVELOPE_BYTES:
        raise _invalid(
            f"itch envelope must not exceed {MAX_ENVELOPE_BYTES} UTF-8 bytes"
        )
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(text, object_pairs_hook=_strict_json_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise _invalid("itch envelope must be valid UTF-8 JSON") from error
    if not isinstance(parsed, Mapping):
        raise _invalid("itch envelope must contain a JSON object")
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


def _optional_text(value: object, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, name, maximum)


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


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise _invalid(f"{name} must be a boolean")
    return value


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


def _itch_url(value: object, name: str) -> tuple[str, str]:
    parsed = _text(value, name, 8192)
    try:
        split = urlsplit(parsed)
        hostname = split.hostname
        port = split.port
    except ValueError as error:
        raise _invalid(f"{name} must be a valid itch.io HTTPS URL") from error
    if (
        split.scheme != "https"
        or not split.netloc
        or hostname is None
        or split.username is not None
        or split.password is not None
        or port is not None
        or split.netloc != split.netloc.lower()
        or not (hostname == "itch.io" or hostname.endswith(".itch.io"))
    ):
        raise _invalid(f"{name} must use HTTPS on itch.io or an itch.io subdomain")
    return parsed, hostname


def _game_url(value: object) -> tuple[str, str]:
    parsed, hostname = _itch_url(value, "game_url")
    split = urlsplit(parsed)
    host_parts = hostname.split(".")
    path_match = re.fullmatch(r"/([^/]+)/?", split.path)
    if (
        len(host_parts) != 3
        or host_parts[-2:] != ["itch", "io"]
        or host_parts[0] in {"www", "itch"}
        or path_match is None
        or split.query
        or split.fragment
        or "?" in parsed
        or "#" in parsed
    ):
        raise _invalid("game_url must be a canonical creator.itch.io/game-slug URL")
    path = path_match.group(1)
    platform_id = f"{host_parts[0]}.{path}"
    try:
        platform_id = validate_platform_id("itch", platform_id)
    except InputValidationError as error:
        raise _invalid("game_url must contain canonical lowercase creator and game slugs") from error
    return parsed, platform_id


def _evidence_url(value: object) -> str:
    parsed, _ = _itch_url(value, "evidence_url")
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


@dataclass(frozen=True)
class ItchBrowserRow:
    """One exact visible row collected from an itch.io discovery surface."""

    title: str
    developer: str
    game_url: str
    surface: str
    surface_scope: str
    rank: int
    browser_playable: bool
    genre: str | None
    is_jam: bool
    author_release_count: int
    originality: str
    observed_at: datetime
    evidence_url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _text(self.title, "title", 256))
        object.__setattr__(self, "developer", _text(self.developer, "developer", 256))
        game_url, _ = _game_url(self.game_url)
        object.__setattr__(self, "game_url", game_url)
        object.__setattr__(self, "surface", _literal(self.surface, "surface", _SURFACES))
        if self.surface_scope != "global":
            raise _invalid("surface_scope must be global for itch discovery")
        object.__setattr__(
            self,
            "rank",
            _integer(self.rank, "rank", minimum=1, maximum=MAX_ROWS),
        )
        object.__setattr__(
            self,
            "browser_playable",
            _boolean(self.browser_playable, "browser_playable"),
        )
        object.__setattr__(self, "genre", _optional_text(self.genre, "genre", 128))
        object.__setattr__(self, "is_jam", _boolean(self.is_jam, "is_jam"))
        object.__setattr__(
            self,
            "author_release_count",
            _integer(
                self.author_release_count,
                "author_release_count",
                minimum=0,
                maximum=MAX_AUTHOR_RELEASE_COUNT,
            ),
        )
        object.__setattr__(
            self,
            "originality",
            _literal(self.originality, "originality", _ORIGINALITY),
        )
        if not isinstance(self.observed_at, datetime):
            raise _invalid("observed_at must be a UTC datetime")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() != timezone.utc.utcoffset(self.observed_at):
            raise _invalid("observed_at must be a UTC datetime")
        if self.observed_at.microsecond:
            raise _invalid("observed_at must use second precision")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(timezone.utc))
        object.__setattr__(self, "evidence_url", _evidence_url(self.evidence_url))


@dataclass(frozen=True)
class ItchBrowserEnvelope:
    """Validated Agent payload for one itch collection timestamp."""

    schema_version: int
    run_id: str
    collector: str
    geo: str
    locale: str
    metric_definition_version: int
    observed_at: datetime
    rows: tuple[ItchBrowserRow, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1 or type(self.schema_version) is not int:
            raise _invalid("schema_version must be 1")
        _text(self.run_id, "run_id", 50)
        if self.collector != "itch":
            raise _invalid("collector must be itch")
        if not isinstance(self.geo, str) or _COUNTRY.fullmatch(self.geo) is None:
            raise _invalid("geo must be a two-letter uppercase country code")
        if not isinstance(self.locale, str) or _LOCALE.fullmatch(self.locale) is None:
            raise _invalid("locale is invalid")
        object.__setattr__(
            self,
            "metric_definition_version",
            _integer(
                self.metric_definition_version,
                "metric_definition_version",
                minimum=1,
                maximum=1_000_000,
            ),
        )
        if not isinstance(self.observed_at, datetime):
            raise _invalid("observed_at must be a UTC datetime")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() != timezone.utc.utcoffset(self.observed_at):
            raise _invalid("observed_at must be a UTC datetime")
        if self.observed_at.microsecond:
            raise _invalid("observed_at must use second precision")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(timezone.utc))
        if not isinstance(self.rows, tuple) or any(
            not isinstance(row, ItchBrowserRow) for row in self.rows
        ):
            raise _invalid("rows must contain only ItchBrowserRow records")
        if len(self.rows) > MAX_ROWS:
            raise _invalid(f"rows must not contain more than {MAX_ROWS} records")
        for row in self.rows:
            if row.observed_at != self.observed_at:
                raise _invalid("row observed_at must match envelope observed_at")


def _parse_row(value: object) -> ItchBrowserRow:
    row = _exact_keys(value, _ROW_KEYS, "itch row")
    game_url, _ = _game_url(row["game_url"])
    return ItchBrowserRow(
        title=row["title"],  # type: ignore[arg-type]
        developer=row["developer"],  # type: ignore[arg-type]
        game_url=game_url,
        surface=row["surface"],  # type: ignore[arg-type]
        surface_scope=row["surface_scope"],  # type: ignore[arg-type]
        rank=row["rank"],  # type: ignore[arg-type]
        browser_playable=row["browser_playable"],  # type: ignore[arg-type]
        genre=row["genre"],  # type: ignore[arg-type]
        is_jam=row["is_jam"],  # type: ignore[arg-type]
        author_release_count=row["author_release_count"],  # type: ignore[arg-type]
        originality=row["originality"],  # type: ignore[arg-type]
        observed_at=_utc_seconds(row["observed_at"], "row observed_at"),
        evidence_url=row["evidence_url"],  # type: ignore[arg-type]
    )


def _deduplicate_rows(rows: Sequence[ItchBrowserRow]) -> tuple[ItchBrowserRow, ...]:
    unique: list[ItchBrowserRow] = []
    seen: dict[tuple[str, str, datetime], ItchBrowserRow] = {}
    for row in rows:
        _, platform_id = _game_url(row.game_url)
        key = (platform_id, row.surface, row.observed_at)
        prior = seen.get(key)
        if prior is None:
            seen[key] = row
            unique.append(row)
        elif prior != row:
            raise _invalid(
                "conflicting duplicate itch row for platform ID, surface, and timestamp"
            )
    return tuple(unique)


def _validated_envelope_rows(
    run: RadarRun,
    envelope: ItchBrowserEnvelope,
) -> tuple[ItchBrowserRow, ...]:
    """Recheck run-bound invariants at every untrusted construction boundary."""

    if envelope.observed_at < run.started_at:
        raise _invalid("itch envelope observed_at must not precede run started_at")
    for row in envelope.rows:
        if row.observed_at != envelope.observed_at:
            raise _invalid("row observed_at must match envelope observed_at")
    return _deduplicate_rows(envelope.rows)


def parse_itch_envelope(
    value: object,
    run: RadarRun,
) -> ItchBrowserEnvelope:
    """Parse an untrusted browser envelope against its originating run."""

    if not isinstance(run, RadarRun):
        raise _invalid("run must be a RadarRun")
    payload = _exact_keys(
        _materialize_payload(value),
        _ENVELOPE_KEYS,
        "itch envelope",
    )
    if payload["run_id"] != run.run_id:
        raise _invalid("itch envelope run_id must match the originating run")
    if not isinstance(payload["rows"], list):
        raise _invalid("rows must be an array")
    if len(payload["rows"]) > MAX_ROWS:
        raise _invalid(f"rows must not contain more than {MAX_ROWS} records")
    rows = tuple(_parse_row(item) for item in payload["rows"])
    envelope = ItchBrowserEnvelope(
        schema_version=payload["schema_version"],  # type: ignore[arg-type]
        run_id=payload["run_id"],  # type: ignore[arg-type]
        collector=payload["collector"],  # type: ignore[arg-type]
        geo=payload["geo"],  # type: ignore[arg-type]
        locale=payload["locale"],  # type: ignore[arg-type]
        metric_definition_version=payload["metric_definition_version"],  # type: ignore[arg-type]
        observed_at=_utc_seconds(payload["observed_at"], "envelope observed_at"),
        rows=rows,
    )
    validated_rows = _validated_envelope_rows(run, envelope)
    if validated_rows != envelope.rows:
        envelope = replace(envelope, rows=validated_rows)
    return envelope


def _eligibility(row: ItchBrowserRow) -> tuple[bool, bool, tuple[str, ...]]:
    reasons: list[str] = []
    if row.is_jam:
        reasons.append("jam_only")
    originality_reason = {
        "known_reupload": "known_reupload",
        "known_commercial_copy": "commercial_copy",
        "mass_reupload": "mass_reupload",
    }.get(row.originality)
    if originality_reason is not None:
        reasons.append(originality_reason)
    if not row.browser_playable:
        reasons.append("not_browser_playable")
    author_non_spam = row.originality not in {
        "known_reupload",
        "known_commercial_copy",
        "mass_reupload",
    }
    return not reasons, author_non_spam, tuple(reasons)


def _compact_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_itch_observations(
    run: RadarRun,
    envelope: ItchBrowserEnvelope,
) -> tuple[PlatformObservation, ...]:
    """Build deterministic observations without performing any scoring."""

    if not isinstance(run, RadarRun):
        raise _invalid("run must be a RadarRun")
    if not isinstance(envelope, ItchBrowserEnvelope):
        raise _invalid("envelope must be an ItchBrowserEnvelope")
    if envelope.run_id != run.run_id:
        raise _invalid("itch envelope run_id must match the originating run")
    if envelope.collector != "itch":
        raise _invalid("collector must be itch")
    if "itch" not in run.platforms:
        raise _invalid("originating run did not select itch")

    rows = _validated_envelope_rows(run, envelope)
    observations: list[PlatformObservation] = []
    for row in rows:
        _, platform_id = _game_url(row.game_url)
        collector_eligible, author_non_spam, exclusion_reasons = _eligibility(row)
        observation_id = (
            f"itch:{platform_id}:{row.surface}:"
            f"{_compact_timestamp(row.observed_at)}"
        )
        evidence_urls = tuple(dict.fromkeys((row.evidence_url, row.game_url)))
        observations.append(
            PlatformObservation(
                schema_version=1,
                observation_id=observation_id,
                run_id=run.run_id,
                platform="itch",
                platform_id=platform_id,
                provider="itch_agent_browser",
                surface=row.surface,
                geo=envelope.geo,
                locale=envelope.locale,
                query_parameters={"surface_scope": row.surface_scope},
                metric_definition_version=envelope.metric_definition_version,
                observed_at=row.observed_at,
                release_at=None,
                source_rank=row.rank,
                raw_metrics={
                    "title": row.title,
                    "developer": row.developer,
                    "game_url": row.game_url,
                    "browser_playable": row.browser_playable,
                    "genre": row.genre,
                    "is_jam": row.is_jam,
                    "author_release_count": row.author_release_count,
                    "originality": row.originality,
                    "author_non_spam": author_non_spam,
                    "collector_eligible": collector_eligible,
                    "exclusion_reasons": exclusion_reasons,
                },
                evidence_urls=evidence_urls,
            )
        )
    return tuple(observations)


__all__ = [
    "MAX_AUTHOR_RELEASE_COUNT",
    "MAX_ENVELOPE_BYTES",
    "MAX_ROWS",
    "ItchBrowserEnvelope",
    "ItchBrowserRow",
    "build_itch_observations",
    "parse_itch_envelope",
]
