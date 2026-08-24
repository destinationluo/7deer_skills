"""Versioned, local-only SEO and community enrichment records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Literal, Mapping, Sequence, cast
from urllib.parse import urlsplit

from .errors import InputValidationError
from .schemas import MAX_JSON_SAFE_INTEGER, MAX_STEAM_APPID, MIN_JSON_SAFE_INTEGER


EvidenceSource = Literal["google", "youtube", "reddit"]

_ALLOWED_EVIDENCE_SOURCES = {"google", "youtube", "reddit"}
_MAX_INPUT_BYTES = 5 * 1024 * 1024
_MAX_JSON_DEPTH = 256
_MAX_SAFE_TEXT = str(MAX_JSON_SAFE_INTEGER)
_MIN_SAFE_MAGNITUDE_TEXT = str(abs(MIN_JSON_SAFE_INTEGER))
_RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", flags=re.ASCII)
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z",
    flags=re.ASCII,
)
_CONTROL_OR_SPACE = re.compile(r"[\x00-\x20\x7f]")


@dataclass(frozen=True)
class Evidence:
    """One typed HTTPS evidence URL."""

    source: EvidenceSource
    url: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or self.source not in _ALLOWED_EVIDENCE_SOURCES:
            raise InputValidationError(f"unknown evidence source: {self.source!r}")
        _https_url(self.url)


@dataclass(frozen=True)
class EnrichmentRecord:
    """Validated SEO and community observations for one Steam AppID."""

    appid: int
    google_competition_gap_score: int
    expandable_queries: Sequence[str]
    youtube_relevant_7d: int | None
    reddit_relevant_7d: int | None
    reddit_upvotes_7d: int | None
    evidence: Sequence[Evidence]

    def __post_init__(self) -> None:
        _appid(self.appid)
        _score(self.google_competition_gap_score)
        if isinstance(self.expandable_queries, (str, bytes)) or not isinstance(
            self.expandable_queries, Sequence
        ):
            raise InputValidationError("expandable_queries must be an array")
        queries = tuple(self.expandable_queries)
        if any(not isinstance(query, str) or not query.strip() for query in queries):
            raise InputValidationError(
                "expandable_queries must contain non-empty strings"
            )
        _optional_count(self.youtube_relevant_7d, "youtube_relevant_7d")
        _optional_count(self.reddit_relevant_7d, "reddit_relevant_7d")
        _optional_count(self.reddit_upvotes_7d, "reddit_upvotes_7d")
        if isinstance(self.evidence, (str, bytes)) or not isinstance(
            self.evidence, Sequence
        ):
            raise InputValidationError("evidence must be an array")
        evidence = tuple(self.evidence)
        if not all(isinstance(item, Evidence) for item in evidence):
            raise InputValidationError("evidence must contain Evidence values")
        sources = {item.source for item in evidence}
        if "google" not in sources:
            raise InputValidationError("every game requires Google evidence")
        if self.youtube_relevant_7d is not None and "youtube" not in sources:
            raise InputValidationError(
                "YouTube signals require matching YouTube evidence"
            )
        if (
            self.reddit_relevant_7d is not None
            or self.reddit_upvotes_7d is not None
        ) and "reddit" not in sources:
            raise InputValidationError(
                "Reddit signals require matching Reddit evidence"
            )
        object.__setattr__(self, "expandable_queries", queries)
        object.__setattr__(self, "evidence", evidence)


@dataclass(frozen=True)
class EnrichmentBundle:
    """Immutable, run-bound collection of enrichment records."""

    schema_version: int
    run_id: str
    observed_at: str
    games: Mapping[int, EnrichmentRecord]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InputValidationError("schema_version must be exactly 1")
        _run_id(self.run_id)
        _utc_timestamp(self.observed_at)
        if not isinstance(self.games, Mapping):
            raise InputValidationError("games must be an AppID mapping")
        frozen: dict[int, EnrichmentRecord] = {}
        for key, record in self.games.items():
            appid = _appid(key)
            if not isinstance(record, EnrichmentRecord) or record.appid != appid:
                raise InputValidationError("game mapping keys must match record AppIDs")
            if appid in frozen:
                raise InputValidationError("games must use unique AppIDs")
            frozen[appid] = record
        object.__setattr__(self, "games", MappingProxyType(frozen))


def load_enrichment(path: Path, expected_run_id: str) -> EnrichmentBundle:
    """Safely read and validate one local enrichment JSON file.

    This function intentionally performs no network access. The run ID is
    validated independently and must exactly match the file envelope.
    """

    _run_id(expected_run_id)
    try:
        parsed = _parse_json(_read_local_text(Path(path)))
        return _bundle_from_json(parsed, expected_run_id)
    except InputValidationError:
        raise
    except RecursionError as error:
        raise InputValidationError("enrichment JSON nesting is too deep") from error


def _bundle_from_json(value: object, expected_run_id: str) -> EnrichmentBundle:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "run_id",
        "observed_at",
        "games",
    }:
        raise InputValidationError("enrichment has an invalid top-level schema")
    if value["run_id"] != expected_run_id:
        raise InputValidationError("enrichment run_id does not match expected run")
    games_value = value["games"]
    if not isinstance(games_value, list):
        raise InputValidationError("enrichment games must be an array")
    games: dict[int, EnrichmentRecord] = {}
    for item in games_value:
        record = _record_from_json(item)
        if record.appid in games:
            raise InputValidationError("enrichment contains a duplicate AppID")
        games[record.appid] = record
    return EnrichmentBundle(
        schema_version=value["schema_version"],
        run_id=value["run_id"],
        observed_at=value["observed_at"],
        games=games,
    )


def _record_from_json(value: object) -> EnrichmentRecord:
    fields = {
        "appid",
        "google_competition_gap_score",
        "expandable_queries",
        "youtube_relevant_7d",
        "reddit_relevant_7d",
        "reddit_upvotes_7d",
        "evidence",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise InputValidationError("enrichment game has invalid fields")
    evidence_value = value["evidence"]
    if not isinstance(evidence_value, list):
        raise InputValidationError("evidence must be an array")
    evidence = tuple(_evidence_from_json(item) for item in evidence_value)
    return EnrichmentRecord(
        appid=value["appid"],
        google_competition_gap_score=value["google_competition_gap_score"],
        expandable_queries=value["expandable_queries"],
        youtube_relevant_7d=value["youtube_relevant_7d"],
        reddit_relevant_7d=value["reddit_relevant_7d"],
        reddit_upvotes_7d=value["reddit_upvotes_7d"],
        evidence=evidence,
    )


def _evidence_from_json(value: object) -> Evidence:
    if not isinstance(value, Mapping) or set(value) != {"source", "url"}:
        raise InputValidationError("evidence has invalid fields")
    return Evidence(source=cast(EvidenceSource, value["source"]), url=value["url"])


def _read_local_text(path: Path) -> str:
    descriptor: int | None = None
    parent_descriptor: int | None = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        parent_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        parent_flags |= getattr(os, "O_DIRECTORY", 0) | nofollow
        parent_descriptor = os.open(path.parent, parent_flags)
        opened_parent = os.fstat(parent_descriptor)
        named_parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or stat.S_ISLNK(named_parent.st_mode)
            or not stat.S_ISDIR(named_parent.st_mode)
            or _directory_identity(named_parent)
            != _directory_identity(opened_parent)
        ):
            raise InputValidationError("enrichment parent directory changed")
        before = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise InputValidationError(
                "enrichment input must be a regular non-symlink file"
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise InputValidationError(
                "enrichment input must be a regular non-symlink file"
            )
        expected = _file_fingerprint(opened)
        if _file_fingerprint(before) != expected:
            raise InputValidationError("enrichment input changed while opening")
        if opened.st_size > _MAX_INPUT_BYTES:
            raise InputValidationError("enrichment input exceeds the 5 MiB limit")
        raw = bytearray()
        while len(raw) < opened.st_size:
            remaining = opened.st_size - len(raw)
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                raise InputValidationError("enrichment input changed while reading")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise InputValidationError("enrichment input changed while reading")
        after = os.fstat(descriptor)
        name_after = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        parent_after = path.parent.lstat()
        if (
            not stat.S_ISREG(name_after.st_mode)
            or stat.S_ISLNK(parent_after.st_mode)
            or not stat.S_ISDIR(parent_after.st_mode)
            or _directory_identity(parent_after)
            != _directory_identity(opened_parent)
            or _file_fingerprint(after) != expected
            or _file_fingerprint(name_after) != expected
            or len(raw) != opened.st_size
        ):
            raise InputValidationError("enrichment input changed while reading")
        return bytes(raw).decode("utf-8-sig", errors="strict")
    except InputValidationError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise InputValidationError("unable to read strict UTF-8 enrichment input") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if parent_descriptor is not None:
            try:
                os.close(parent_descriptor)
            except OSError:
                pass


def _file_fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_identity(value: os.stat_result) -> tuple[int, int]:
    return (value.st_dev, value.st_ino)


def _parse_json(text: str) -> object:
    try:
        parsed = json.loads(
            text,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
            parse_int=_parse_json_integer,
            object_pairs_hook=_unique_object,
        )
        _validate_depth(parsed)
        return parsed
    except InputValidationError:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeError) as error:
        raise InputValidationError("invalid enrichment JSON input") from error


def _reject_json_constant(value: str) -> object:
    del value
    raise InputValidationError("enrichment JSON numbers must be finite")


def _parse_json_integer(value: str) -> int:
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    maximum = _MIN_SAFE_MAGNITUDE_TEXT if negative else _MAX_SAFE_TEXT
    if re.fullmatch(r"[0-9]+", digits, re.ASCII) is None:
        raise InputValidationError("enrichment JSON integer is invalid")
    significant = digits.lstrip("0") or "0"
    if len(significant) > len(maximum) or (
        len(significant) == len(maximum) and significant > maximum
    ):
        raise InputValidationError("enrichment JSON integer is outside the safe range")
    parsed = int(significant)
    return -parsed if negative else parsed


def _parse_json_float(value: str) -> float:
    try:
        parsed = float(value)
    except (ValueError, OverflowError) as error:
        raise InputValidationError("enrichment JSON float is invalid") from error
    if not math.isfinite(parsed):
        raise InputValidationError("enrichment JSON numbers must be finite")
    return parsed


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InputValidationError("enrichment JSON keys must be unique")
        result[key] = value
    return result


def _validate_depth(value: object, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise InputValidationError("enrichment JSON nesting is too deep")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError as error:
            raise InputValidationError(
                "enrichment JSON strings must be valid UTF-8"
            ) from error
        return
    if isinstance(value, list):
        for item in value:
            _validate_depth(item, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            try:
                key.encode("utf-8")
            except UnicodeError as error:
                raise InputValidationError(
                    "enrichment JSON keys must be valid UTF-8"
                ) from error
            _validate_depth(item, depth + 1)


def _appid(value: object) -> int:
    if type(value) is not int or value < 1 or value > MAX_STEAM_APPID:
        raise InputValidationError(
            f"appid must be an integer from 1 through {MAX_STEAM_APPID}"
        )
    return value


def _score(value: object) -> int:
    if type(value) is not int or value < 0 or value > 100:
        raise InputValidationError(
            "Google competition gap score must be an integer from 0 through 100"
        )
    return value


def _optional_count(value: object, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0 or value > MAX_JSON_SAFE_INTEGER:
        raise InputValidationError(
            f"{name} must be a non-negative JSON-safe integer or null"
        )
    return value


def _run_id(value: object) -> str:
    if not isinstance(value, str) or _RUN_ID.fullmatch(value) is None:
        raise InputValidationError("run_id must use the canonical UTC format")
    try:
        datetime.strptime(value[:16], "%Y%m%dT%H%M%SZ")
    except ValueError as error:
        raise InputValidationError("run_id must contain a valid UTC timestamp") from error
    return value


def _utc_timestamp(value: object) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise InputValidationError("observed_at must be an ISO-8601 UTC timestamp")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise InputValidationError("observed_at must be an ISO-8601 UTC timestamp") from error
    return value


def _https_url(value: object) -> str:
    if not isinstance(value, str) or not value or _CONTROL_OR_SPACE.search(value) or "\\" in value:
        raise InputValidationError("evidence URL must be a valid HTTPS URL")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as error:
        raise InputValidationError("evidence URL must be a valid HTTPS URL") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise InputValidationError(
            "evidence URL must use HTTPS without credentials or fragments"
        )
    authority = parsed.netloc
    if authority.startswith("["):
        closing = authority.find("]")
        if closing <= 1:
            raise InputValidationError("evidence URL has an invalid IPv6 authority")
        suffix = authority[closing + 1 :]
        if suffix not in {"", ":443"}:
            raise InputValidationError(
                "evidence URL explicit port must be exactly 443"
            )
    else:
        if authority.count(":") > 1:
            raise InputValidationError("evidence URL IPv6 hosts must use brackets")
        if ":" in authority and authority.rsplit(":", 1)[1] != "443":
            raise InputValidationError(
                "evidence URL explicit port must be exactly 443"
            )
    return value
