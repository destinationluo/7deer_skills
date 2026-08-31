"""Conservative, deterministic linking for cross-platform game identities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import ipaddress
import re
import unicodedata

from .config import IdentityAlias
from .errors import InputValidationError
from .schemas import GameIdentity, PlatformRecord


_MAX_NAME_LENGTH = 512
_MAX_PLATFORM_ID_LENGTH = 128
_MAX_SAFE_INTEGER = 2**53 - 1
_NUMERIC_ID = re.compile(r"[1-9][0-9]*\Z", flags=re.ASCII)
_ITCH_ID = re.compile(
    r"[a-z0-9]+(?:[-_.][a-z0-9]+)*\Z",
    flags=re.ASCII,
)
_ASCII_DOMAIN_LABEL = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z",
    flags=re.ASCII,
)


def _invalid(message: str) -> InputValidationError:
    return InputValidationError(message)


def _validated_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _invalid(f"{name} must be nonempty text of at most {maximum} characters")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise _invalid(f"{name} must not contain control or format characters")
    return value


def _normalize(value: object, name: str) -> str:
    text = unicodedata.normalize(
        "NFKC",
        _validated_text(value, name, _MAX_NAME_LENGTH),
    ).casefold()
    folded = "".join(
        " " if unicodedata.category(character)[0] in {"P", "S"} else character
        for character in text
    )
    normalized = " ".join(folded.split())
    if not normalized:
        raise _invalid(f"{name} must contain letters or numbers after normalization")
    if len(normalized) > _MAX_NAME_LENGTH:
        raise _invalid(f"normalized {name} is too long")
    return normalized


def normalize_name(value: str) -> str:
    """Return a bounded NFKC/casefolded game title for exact comparison."""

    return _normalize(value, "name")


def normalize_developer(value: str) -> str:
    """Return a title-compatible normalized developer name."""

    return _normalize(value, "developer")


def canonical_domain(value: str | None) -> str | None:
    """Normalize a bare official domain, rejecting URL-like or unsafe input."""

    if value is None:
        return None
    raw = unicodedata.normalize(
        "NFKC",
        _validated_text(value, "official_domain", 253),
    )
    if any(character.isspace() for character in raw):
        raise _invalid("official_domain must not contain whitespace")
    if any(marker in raw for marker in ("/", "\\", "@", ":", "?", "#")):
        raise _invalid("official_domain must be a bare domain, not a URL")

    if raw.endswith("."):
        raw = raw[:-1]
    if raw.casefold().startswith("www."):
        raw = raw[4:]
    if not raw or raw.startswith(".") or raw.endswith("."):
        raise _invalid("official_domain is invalid")

    unicode_labels = raw.split(".")
    if len(unicode_labels) < 2 or any(not label for label in unicode_labels):
        raise _invalid("official_domain must contain a registrable-looking domain")
    try:
        labels = tuple(
            label.encode("idna").decode("ascii").lower()
            for label in unicode_labels
        )
    except (UnicodeError, UnicodeDecodeError) as error:
        raise _invalid("official_domain contains an invalid IDNA label") from error

    if any(_ASCII_DOMAIN_LABEL.fullmatch(label) is None for label in labels):
        raise _invalid("official_domain contains an invalid label")
    domain = ".".join(labels)
    if len(domain) > 253:
        raise _invalid("official_domain exceeds the DNS length limit")
    terminal = labels[-1]
    if not (terminal.startswith("xn--") or terminal.isalpha()):
        raise _invalid("official_domain must end in an alphabetic or IDNA label")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise _invalid("official_domain must not be an IP literal")


def _canonical_key(platform: object, platform_id: object) -> str:
    if platform not in {"itch", "steam", "roblox"}:
        raise _invalid("platform must be itch, steam, or roblox")
    if not isinstance(platform_id, str) or not platform_id:
        raise _invalid("platform_id must be nonempty text")
    if len(platform_id) > _MAX_PLATFORM_ID_LENGTH:
        raise _invalid("platform_id is too long")

    if platform in {"steam", "roblox"}:
        if _NUMERIC_ID.fullmatch(platform_id) is None:
            raise _invalid(f"{platform} platform_id must be a positive ASCII integer")
        if int(platform_id) > _MAX_SAFE_INTEGER:
            raise _invalid(f"{platform} platform_id exceeds the JSON safe integer limit")
    elif _ITCH_ID.fullmatch(platform_id) is None:
        raise _invalid(
            "itch platform_id must be a lowercase safe slug without repeated separators"
        )
    return f"{platform}:{platform_id}"


def _canonical_key_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _invalid(f"{name} must be an exact platform key")
    platform, separator, platform_id = value.partition(":")
    if not separator or not platform_id or ":" in platform_id:
        raise _invalid(f"{name} must be an exact platform key")
    return _canonical_key(platform, platform_id)


def platform_key(record: PlatformRecord) -> str:
    """Return the sole canonical key used by identity linking and collectors."""

    if not isinstance(record, PlatformRecord):
        raise _invalid("record must be a PlatformRecord")
    return _canonical_key(record.platform, record.platform_id)


def _validated_aliases(
    aliases: Sequence[IdentityAlias] | Mapping[object, object],
) -> tuple[tuple[str, str], ...]:
    if isinstance(aliases, Mapping):
        if aliases:
            raise _invalid("aliases must be a sequence of IdentityAlias objects")
        return ()
    if isinstance(aliases, (str, bytes)) or not isinstance(aliases, Sequence):
        raise _invalid("aliases must be a sequence of IdentityAlias objects")

    links: list[tuple[str, str]] = []
    for alias in aliases:
        if not isinstance(alias, IdentityAlias) or alias.schema_version != 1:
            raise _invalid("aliases must contain version-1 IdentityAlias objects")
        source = _canonical_key_text(alias.source, "alias source")
        target = _canonical_key_text(alias.target, "alias target")
        links.append((source, target))
    return tuple(links)


def _validated_identities(
    identities: Sequence[GameIdentity],
) -> tuple[tuple[GameIdentity, frozenset[str]], ...]:
    if isinstance(identities, (str, bytes)) or not isinstance(identities, Sequence):
        raise _invalid("identities must be a sequence of GameIdentity objects")
    validated: list[tuple[GameIdentity, frozenset[str]]] = []
    for identity in identities:
        if not isinstance(identity, GameIdentity):
            raise _invalid("identities must contain GameIdentity objects")
        keys = frozenset(platform_key(record) for record in identity.platform_records)
        validated.append((identity, keys))
    return tuple(validated)


def _unique_match(opportunity_ids: set[str]) -> str | None:
    if len(opportunity_ids) == 1:
        return next(iter(opportunity_ids))
    return None


def match_identity(
    candidate: PlatformRecord,
    identities: Sequence[GameIdentity],
    aliases: Sequence[IdentityAlias] | Mapping[object, object] = (),
) -> str | None:
    """Return one unambiguous existing opportunity ID without creating state."""

    candidate_key = platform_key(candidate)
    existing = _validated_identities(identities)
    alias_links = _validated_aliases(aliases)

    exact = {
        identity.opportunity_id
        for identity, keys in existing
        if candidate_key in keys
    }
    if exact:
        return _unique_match(exact)

    aliased_keys = {
        target if source == candidate_key else source
        for source, target in alias_links
        if candidate_key == source or candidate_key == target
    }
    aliased = {
        identity.opportunity_id
        for identity, keys in existing
        if keys.intersection(aliased_keys)
    }
    if aliased:
        return _unique_match(aliased)

    candidate_name = normalize_name(candidate.name)
    candidate_developer = (
        normalize_developer(candidate.developer)
        if candidate.developer is not None
        else None
    )
    candidate_domain = canonical_domain(candidate.official_domain)
    metadata: set[str] = set()
    for identity, keys in existing:
        if any(key.startswith(f"{candidate.platform}:") for key in keys):
            continue
        if normalize_name(identity.name) != candidate_name:
            continue
        developer_matches = (
            candidate_developer is not None
            and identity.developer is not None
            and normalize_developer(identity.developer) == candidate_developer
        )
        domain_matches = (
            candidate_domain is not None
            and identity.official_domain is not None
            and canonical_domain(identity.official_domain) == candidate_domain
        )
        if developer_matches or domain_matches:
            metadata.add(identity.opportunity_id)
    return _unique_match(metadata)


__all__ = [
    "canonical_domain",
    "match_identity",
    "normalize_developer",
    "normalize_name",
    "platform_key",
]
