"""Strict local import for manually exported SteamDB trend data."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import io
import json
import math
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from .errors import InputValidationError
from .schemas import GameRecord, MetricObservation, RejectedRow


_MAX_INPUT_BYTES = 5 * 1024 * 1024
_MAX_JSON_DEPTH = 256
_MAX_NUMBER_DIGITS = 64
_ALLOWED_VIEWS = {
    "trending_games",
    "wishlist_activity",
    "trending_followers",
    "recent_releases",
}
_ALIASES = {
    "appid": ("appid", "app_id", "steam_appid"),
    "url": ("url", "steamdb_url", "app_url"),
    "name": ("name", "game", "title"),
    "rank": ("rank", "#"),
    "current_players": ("players_now", "current", "online"),
    "peak_players": ("peak", "24h_peak"),
    "followers": ("followers", "follows"),
    "follower_gain_7d": ("7d_gain", "followers_7d_gain"),
    "wishlist_gain_7d": ("wishlist_7d_gain", "wishlists_7d_gain"),
    "rating_percent": ("rating", "rating_percent"),
    "release_date": ("release", "release_date"),
}
_KNOWN_ALIASES = {
    alias
    for aliases in _ALIASES.values()
    for alias in aliases
}
_NUMBER = re.compile(
    r"^(?P<sign>\+)?(?P<number>(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?)(?P<suffix>[KkMm])?(?P<percent>%)?$",
    flags=re.ASCII,
)
_ISO_DATE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$", flags=re.ASCII)
_ENGLISH_DATE = re.compile(
    r"^([0-9]{2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) ([0-9]{4})$",
    flags=re.ASCII,
)
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
_APP_PATH = re.compile(r"/app/([0-9]+)(?=/|[?#]|$)", flags=re.ASCII)


@dataclass(frozen=True)
class ImportResult:
    """Copy-isolated outcome of one local manual import."""

    records: Sequence[GameRecord]
    rejected_rows: Sequence[RejectedRow]
    raw_canonical: object
    view: str

    def __post_init__(self) -> None:
        if not isinstance(self.view, str) or self.view not in _ALLOWED_VIEWS:
            raise InputValidationError("invalid SteamDB view")
        records = tuple(self.records)
        rejected_rows = tuple(self.rejected_rows)
        if not all(isinstance(record, GameRecord) for record in records):
            raise InputValidationError("records must contain GameRecord values")
        if not all(isinstance(row, RejectedRow) for row in rejected_rows):
            raise InputValidationError("rejected_rows must contain RejectedRow values")
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "rejected_rows", rejected_rows)
        try:
            frozen_raw = _freeze_json(self.raw_canonical)
        except RecursionError as error:
            raise InputValidationError("JSON nesting is too deep") from error
        object.__setattr__(self, "raw_canonical", frozen_raw)

    def raw_to_dict(self) -> object:
        """Return a fresh JSON-native export for raw artifact persistence."""

        try:
            return _thaw_json(self.raw_canonical)
        except RecursionError as error:
            raise InputValidationError("JSON nesting is too deep") from error


def parse_number(value: object) -> int | float | None:
    """Parse a non-negative SteamDB display number into a canonical value."""

    if value is None:
        return None
    if isinstance(value, bool):
        raise InputValidationError("number must not be boolean")
    if isinstance(value, int):
        if value < 0:
            raise InputValidationError("number must not be negative")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            raise InputValidationError("number must be finite and non-negative")
        return int(value) if value.is_integer() else value
    if not isinstance(value, str):
        raise InputValidationError("number has an unsupported type")

    stripped = value.strip()
    if not stripped or stripped == "—":
        return None
    match = _NUMBER.fullmatch(stripped)
    if match is None:
        raise InputValidationError("number has an invalid format")
    number_text = match.group("number").replace(",", "")
    if sum(character.isdigit() for character in number_text) > _MAX_NUMBER_DIGITS:
        raise InputValidationError("number has too many digits")
    try:
        with localcontext() as context:
            context.prec = _MAX_NUMBER_DIGITS + 6
            parsed = Decimal(number_text)
            suffix = match.group("suffix")
            if suffix is not None:
                parsed *= Decimal(
                    1_000 if suffix.lower() == "k" else 1_000_000
                )
    except InvalidOperation as error:
        raise InputValidationError("number has an invalid format") from error
    if not parsed.is_finite() or parsed < 0:
        raise InputValidationError("number must be finite and non-negative")
    if parsed == parsed.to_integral_value():
        return int(parsed)
    as_float = float(parsed)
    if not math.isfinite(as_float):
        raise InputValidationError("number must be finite and non-negative")
    return as_float


def parse_release_date(value: object) -> str | None:
    """Normalize a SteamDB calendar date without applying timezone shifts."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise InputValidationError("release date must be a string")
    stripped = value.strip()
    if not stripped or stripped == "—":
        return None

    iso_match = _ISO_DATE.fullmatch(stripped)
    english_match = _ENGLISH_DATE.fullmatch(stripped)
    try:
        if iso_match is not None:
            parsed = date(
                int(iso_match.group(1)),
                int(iso_match.group(2)),
                int(iso_match.group(3)),
            )
        elif english_match is not None:
            parsed = date(
                int(english_match.group(3)),
                _MONTHS[english_match.group(2)],
                int(english_match.group(1)),
            )
        else:
            raise InputValidationError("release date has an invalid format")
    except ValueError as error:
        raise InputValidationError("release date is not a valid calendar date") from error
    return parsed.isoformat()


def extract_appid(row: Mapping[str, object]) -> int:
    """Extract one unambiguous positive AppID from aliases or app URLs."""

    normalized = _normalize_row_mapping(row)
    appid_values = _alias_values(normalized, "appid")
    url_values = _alias_values(normalized, "url")

    appids = [_parse_appid_value(value) for _, value in appid_values]
    urls = [_parse_url(value) for _, value in url_values]
    _require_one_canonical_value(appids, "appid")

    url_appids: list[int] = []
    for _, identifiers in urls:
        if not identifiers:
            raise InputValidationError("URL does not contain an AppID path")
        if len(identifiers) != 1:
            raise InputValidationError("URL contains ambiguous AppIDs")
        url_appids.append(identifiers[0])
    _require_one_canonical_value(url_appids, "URL AppID")

    candidates = appids + url_appids
    if not candidates:
        raise InputValidationError("row is missing a positive AppID")
    if len(set(candidates)) != 1:
        raise InputValidationError("AppID aliases and URL disagree")
    return candidates[0]


def import_steamdb(
    path: Path,
    view: str | None,
    observed_at: str,
) -> ImportResult:
    """Import a local SteamDB CSV or JSON export without network access."""

    try:
        return _import_steamdb(path, view, observed_at)
    except RecursionError as error:
        raise InputValidationError("JSON nesting is too deep") from error


def _import_steamdb(
    path: Path,
    view: str | None,
    observed_at: str,
) -> ImportResult:
    """Implementation boundary kept behind recursion normalization."""

    selected_view = _optional_view(view)
    _validate_observed_at(observed_at)
    source_path = Path(path)
    text = _read_local_text(source_path)
    suffix = source_path.suffix.lower()

    if suffix == ".csv":
        if selected_view is None:
            raise InputValidationError("CSV import requires an explicit view")
        rows = _parse_csv(text)
    elif suffix == ".json":
        rows, selected_view = _parse_json(text, selected_view)
    else:
        raise InputValidationError("input must use a .csv or .json suffix")

    if selected_view is None:
        raise InputValidationError("import requires a SteamDB view")
    raw = {
        "schema_version": 1,
        "view": selected_view,
        "rows": rows,
    }

    known_appids: list[int | None] = []
    for row in rows:
        try:
            known_appids.append(extract_appid(row))
        except InputValidationError:
            known_appids.append(None)
    counts: dict[int, int] = {}
    for appid in known_appids:
        if appid is not None:
            counts[appid] = counts.get(appid, 0) + 1
    duplicates = {appid for appid, count in counts.items() if count > 1}

    records: list[GameRecord] = []
    rejected: list[RejectedRow] = []
    for row_number, (row, known_appid) in enumerate(
        zip(rows, known_appids),
        start=1,
    ):
        if known_appid in duplicates:
            rejected.append(
                RejectedRow(
                    row_number=row_number,
                    code="steamdb_duplicate_appid",
                    message="duplicate AppID in import",
                    appid=known_appid,
                )
            )
            continue
        try:
            records.append(
                _record_from_row(row, selected_view, observed_at)
            )
        except InputValidationError:
            rejected.append(
                RejectedRow(
                    row_number=row_number,
                    code="steamdb_row_invalid",
                    message="invalid SteamDB row",
                    appid=known_appid,
                )
            )

    return ImportResult(
        records=records,
        rejected_rows=rejected,
        raw_canonical=raw,
        view=selected_view,
    )


def _record_from_row(
    row: Mapping[str, object],
    view: str,
    observed_at: str,
) -> GameRecord:
    appid = extract_appid(row)
    _validate_reserved_view(row, view)
    name = _canonical_alias(
        row,
        "name",
        _parse_name,
        required=True,
    )
    metrics: dict[str, object] = {}
    for metric_name in (
        "rank",
        "current_players",
        "peak_players",
        "followers",
        "follower_gain_7d",
        "wishlist_gain_7d",
        "rating_percent",
        "release_date",
    ):
        parser: Callable[[object], object]
        if metric_name == "rank":
            parser = _parse_rank
        elif metric_name == "rating_percent":
            parser = _parse_rating
        elif metric_name == "release_date":
            parser = parse_release_date
        else:
            parser = _parse_count
        parsed = _canonical_alias(row, metric_name, parser)
        if parsed is not None:
            metrics[metric_name] = parsed

    _validate_view_requirements(view, metrics)
    observations = {
        metric_name: MetricObservation(
            value=value,
            source_id=f"steamdb_{view}_{metric_name}",
            source_kind="steamdb_manual_import",
            observed_at=observed_at,
        )
        for metric_name, value in metrics.items()
    }
    extra = {
        key: value
        for key, value in row.items()
        if key.strip().casefold() not in _KNOWN_ALIASES
        and key.strip().casefold() != "steamdb_view"
    }
    extra["steamdb_view"] = view
    return GameRecord(
        schema_version=1,
        appid=appid,
        name=name,
        release_status=(
            "released"
            if view in {"trending_games", "recent_releases"}
            else "unreleased"
        ),
        store_url=f"https://store.steampowered.com/app/{appid}/",
        metrics=observations,
        source_extra=extra,
    )


def _validate_view_requirements(view: str, metrics: Mapping[str, object]) -> None:
    if view == "trending_games":
        if "rank" not in metrics and "current_players" not in metrics:
            raise InputValidationError("trending_games requires rank or current_players")
    elif view == "wishlist_activity":
        if "wishlist_gain_7d" not in metrics and "follower_gain_7d" not in metrics:
            raise InputValidationError(
                "wishlist_activity requires wishlist or follower gain"
            )
    elif view == "trending_followers":
        if "follower_gain_7d" not in metrics:
            raise InputValidationError("trending_followers requires follower gain")
    elif view == "recent_releases":
        if "release_date" not in metrics:
            raise InputValidationError("recent_releases requires release date")
        if "current_players" not in metrics and "peak_players" not in metrics:
            raise InputValidationError("recent_releases requires player counts")


def _canonical_alias(
    row: Mapping[str, object],
    field: str,
    parser: Callable[[object], object],
    *,
    required: bool = False,
) -> object:
    parsed_values = [
        parser(value)
        for _, value in _alias_values(row, field)
        if not _is_missing(value)
    ]
    if not parsed_values:
        if required:
            raise InputValidationError(f"row requires {field}")
        return None
    _require_one_canonical_value(parsed_values, field)
    return parsed_values[0]


def _alias_values(
    row: Mapping[str, object],
    field: str,
) -> list[tuple[str, object]]:
    aliases = set(_ALIASES[field])
    return [
        (key, value)
        for key, value in row.items()
        if key.strip().casefold() in aliases and not _is_missing(value)
    ]


def _require_one_canonical_value(values: Sequence[object], field: str) -> None:
    if values and any(value != values[0] for value in values[1:]):
        raise InputValidationError(f"conflicting aliases for {field}")


def _parse_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputValidationError("name must be a non-empty string")
    return value.strip()


def _parse_appid_value(value: object) -> int:
    if isinstance(value, bool):
        raise InputValidationError("AppID must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        parsed = int(value)
    elif isinstance(value, str) and re.fullmatch(r"\+?[0-9]+", value.strip(), re.ASCII):
        digits = value.strip().lstrip("+")
        if len(digits) > _MAX_NUMBER_DIGITS:
            raise InputValidationError("AppID has too many digits")
        try:
            parsed = int(digits)
        except (ValueError, OverflowError) as error:
            raise InputValidationError("AppID must be a positive integer") from error
    else:
        raise InputValidationError("AppID must be a positive integer")
    if parsed <= 0:
        raise InputValidationError("AppID must be a positive integer")
    return parsed


def _parse_url(value: object) -> tuple[str, list[int]]:
    if not isinstance(value, str) or not value.strip():
        raise InputValidationError("URL must be a non-empty string")
    normalized = value.strip()
    identifiers: list[int] = []
    for matched_digits in _APP_PATH.findall(normalized):
        if len(matched_digits) > _MAX_NUMBER_DIGITS:
            raise InputValidationError("URL AppID has too many digits")
        try:
            identifiers.append(int(matched_digits))
        except (ValueError, OverflowError) as error:
            raise InputValidationError("URL AppID must be an integer") from error
    if any(appid <= 0 for appid in identifiers):
        raise InputValidationError("URL AppID must be positive")
    return normalized, identifiers


def _parse_rank(value: object) -> int | None:
    _reject_percent_syntax(value, "rank")
    parsed = parse_number(value)
    if parsed is None:
        return None
    if isinstance(parsed, float) or parsed <= 0:
        raise InputValidationError("rank must be a positive integer")
    return parsed


def _parse_count(value: object) -> int | None:
    _reject_percent_syntax(value, "count")
    parsed = parse_number(value)
    if parsed is None:
        return None
    if isinstance(parsed, float):
        raise InputValidationError("count must be an integer")
    return parsed


def _parse_rating(value: object) -> int | float | None:
    if isinstance(value, str):
        stripped = value.strip()
        without_percent = stripped[:-1] if stripped.endswith("%") else stripped
        if without_percent.lower().endswith(("k", "m")):
            raise InputValidationError("rating_percent must not use K/M suffixes")
    parsed = parse_number(value)
    if parsed is not None and parsed > 100:
        raise InputValidationError("rating_percent must be between 0 and 100")
    return parsed


def _reject_percent_syntax(value: object, field: str) -> None:
    if isinstance(value, str) and value.strip().endswith("%"):
        raise InputValidationError(f"{field} must not use percentage syntax")


def _validate_reserved_view(row: Mapping[str, object], view: str) -> None:
    values = [
        value
        for key, value in row.items()
        if key.strip().casefold() == "steamdb_view"
    ]
    for value in values:
        if not isinstance(value, str) or value.strip() != view:
            raise InputValidationError("steamdb_view metadata conflicts with import view")


def _is_missing(value: object) -> bool:
    return value is None or (
        isinstance(value, str)
        and (not value.strip() or value.strip() == "—")
    )


def _optional_view(view: str | None) -> str | None:
    if view is None:
        return None
    if not isinstance(view, str) or view not in _ALLOWED_VIEWS:
        raise InputValidationError("invalid SteamDB view")
    return view


def _validate_observed_at(observed_at: str) -> None:
    MetricObservation(
        value=0,
        source_id="steamdb_import_validation",
        source_kind="steamdb_manual_import",
        observed_at=observed_at,
    )


def _read_local_text(path: Path) -> str:
    try:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        before_open = None
        if not nofollow:
            before_open = path.lstat()
            if stat.S_ISLNK(before_open.st_mode) or not stat.S_ISREG(
                before_open.st_mode
            ):
                raise InputValidationError(
                    "input must be a regular non-symlink file"
                )

        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= nofollow
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise InputValidationError(
                    "input must be a regular non-symlink file"
                )
            if before_open is not None and (
                before_open.st_dev != opened.st_dev
                or before_open.st_ino != opened.st_ino
            ):
                raise InputValidationError("input changed while being opened")
            if before_open is not None:
                after_open = path.lstat()
                if (
                    stat.S_ISLNK(after_open.st_mode)
                    or not stat.S_ISREG(after_open.st_mode)
                    or after_open.st_dev != opened.st_dev
                    or after_open.st_ino != opened.st_ino
                ):
                    raise InputValidationError("input changed while being opened")
            if opened.st_size > _MAX_INPUT_BYTES:
                raise InputValidationError("input exceeds the 5 MiB limit")

            raw = bytearray()
            while len(raw) <= _MAX_INPUT_BYTES:
                remaining = _MAX_INPUT_BYTES + 1 - len(raw)
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > _MAX_INPUT_BYTES:
                raise InputValidationError("input exceeds the 5 MiB limit")
            return bytes(raw).decode("utf-8-sig", errors="strict")
        finally:
            os.close(descriptor)
    except InputValidationError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise InputValidationError("unable to read strict UTF-8 input") from error


def _parse_csv(text: str) -> list[dict[str, object]]:
    try:
        parsed = list(csv.reader(io.StringIO(text), strict=True))
    except csv.Error as error:
        raise InputValidationError("invalid CSV input") from error
    if not parsed or not parsed[0]:
        raise InputValidationError("CSV input requires a header")
    headers = [header.strip() for header in parsed[0]]
    if any(not header for header in headers):
        raise InputValidationError("CSV headers must be non-empty")
    if len(set(headers)) != len(headers):
        raise InputValidationError("CSV headers must be unique")
    rows: list[dict[str, object]] = []
    for values in parsed[1:]:
        if len(values) != len(headers):
            raise InputValidationError("CSV rows must match the header width")
        rows.append(dict(zip(headers, values)))
    return rows


def _parse_json(
    text: str,
    explicit_view: str | None,
) -> tuple[list[dict[str, object]], str]:
    try:
        parsed = json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_json_object,
        )
    except InputValidationError:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeError) as error:
        raise InputValidationError("invalid JSON input") from error

    selected_view = explicit_view
    if isinstance(parsed, list):
        if selected_view is None:
            raise InputValidationError("JSON row arrays require an explicit view")
        rows_value = parsed
    elif isinstance(parsed, dict):
        if set(parsed) != {"schema_version", "view", "rows"}:
            raise InputValidationError("JSON wrapper has invalid fields")
        version = parsed["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise InputValidationError("JSON wrapper schema_version must be exactly 1")
        wrapper_view = _optional_view(parsed["view"])
        if wrapper_view is None:
            raise InputValidationError("JSON wrapper requires a view")
        if selected_view is not None and selected_view != wrapper_view:
            raise InputValidationError("explicit view does not match JSON wrapper")
        selected_view = wrapper_view
        rows_value = parsed["rows"]
        if not isinstance(rows_value, list):
            raise InputValidationError("JSON wrapper rows must be an array")
    else:
        raise InputValidationError("JSON input must be a row array or wrapper")

    rows: list[dict[str, object]] = []
    for value in rows_value:
        if not isinstance(value, Mapping):
            raise InputValidationError("JSON rows must be mappings")
        rows.append(_normalize_row_mapping(value))
    if selected_view is None:
        raise InputValidationError("JSON import requires a view")
    return rows, selected_view


def _reject_json_constant(value: str) -> object:
    del value
    raise InputValidationError("JSON numbers must be finite")


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InputValidationError("JSON object keys must be unique")
        result[key] = value
    return result


def _normalize_row_mapping(row: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(row, Mapping):
        raise InputValidationError("row must be a mapping")
    normalized: dict[str, object] = {}
    for key, value in row.items():
        if not isinstance(key, str):
            raise InputValidationError("row keys must be strings")
        trimmed = key.strip()
        if not trimmed or trimmed in normalized:
            raise InputValidationError("row keys must be unique and non-empty")
        normalized[trimmed] = _copy_json(value)
    return normalized


def _copy_json(
    value: object,
    *,
    depth: int = 0,
    active_containers: set[int] | None = None,
) -> object:
    if depth > _MAX_JSON_DEPTH:
        raise InputValidationError("JSON nesting is too deep")
    if active_containers is None:
        active_containers = set()
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError as error:
            raise InputValidationError("JSON strings must be valid UTF-8") from error
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InputValidationError("JSON numbers must be finite")
        return value
    if isinstance(value, list):
        identity = _enter_json_container(value, active_containers)
        try:
            return [
                _copy_json(
                    item,
                    depth=depth + 1,
                    active_containers=active_containers,
                )
                for item in value
            ]
        finally:
            active_containers.remove(identity)
    if isinstance(value, Mapping):
        identity = _enter_json_container(value, active_containers)
        try:
            copied: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise InputValidationError("JSON mapping keys must be strings")
                try:
                    key.encode("utf-8")
                except UnicodeError as error:
                    raise InputValidationError(
                        "JSON mapping keys must be valid UTF-8"
                    ) from error
                copied[key] = _copy_json(
                    item,
                    depth=depth + 1,
                    active_containers=active_containers,
                )
            return copied
        finally:
            active_containers.remove(identity)
    raise InputValidationError("value must contain only JSON-native data")


def _freeze_json(value: object) -> object:
    copied = _copy_json(value)
    return _freeze_copied_json(copied)


def _freeze_copied_json(value: object, depth: int = 0) -> object:
    if depth > _MAX_JSON_DEPTH:
        raise InputValidationError("JSON nesting is too deep")
    if isinstance(value, dict):
        return MappingProxyType(
            {
                key: _freeze_copied_json(item, depth + 1)
                for key, item in value.items()
            }
        )
    if isinstance(value, list):
        return tuple(_freeze_copied_json(item, depth + 1) for item in value)
    return value


def _enter_json_container(value: object, active_containers: set[int]) -> int:
    identity = id(value)
    if identity in active_containers:
        raise InputValidationError("JSON values must not contain cycles")
    active_containers.add(identity)
    return identity


def _thaw_json(value: object, depth: int = 0) -> object:
    if depth > _MAX_JSON_DEPTH:
        raise InputValidationError("JSON nesting is too deep")
    if isinstance(value, Mapping):
        return {
            key: _thaw_json(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_thaw_json(item, depth + 1) for item in value]
    return value
