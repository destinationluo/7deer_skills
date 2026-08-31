"""Conservative, deterministic linking for cross-platform game identities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import ipaddress
import re
import unicodedata

from .config import IdentityAlias
from .errors import InputValidationError
from .platform_keys import canonical_platform_key, validate_platform_key
from .schemas import GameIdentity, PlatformRecord


_MAX_NAME_LENGTH = 512
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
    if not normalized or not any(
        unicodedata.category(character)[0] in {"L", "N"}
        for character in normalized
    ):
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
    """Normalize a bare domain with the stdlib IDNA 2003 codec.

    Version 1 is intentionally standard-library-only.  Labels that do not
    round-trip under Python's IDNA 2003 codec are rejected instead of being
    assigned a second, incompatible modern-IDNA key.
    """

    if value is None:
        return None
    raw = _validated_text(value, "official_domain", 253).translate(
        str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."})
    )
    raw = unicodedata.normalize(
        "NFKC",
        raw,
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
    labels = tuple(_canonical_idna_label(label) for label in unicode_labels)

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


def _canonical_idna_label(label: str) -> str:
    """Return one lowercase ASCII label after an IDNA 2003 round trip."""

    try:
        try:
            ascii_label = label.encode("ascii").decode("ascii").lower()
        except UnicodeEncodeError:
            ascii_label = label.encode("idna").decode("ascii").lower()
            decoded = ascii_label.encode("ascii").decode("idna")
            if decoded.encode("idna").decode("ascii").lower() != ascii_label:
                raise UnicodeError("IDNA label does not round-trip")
            return ascii_label

        if ascii_label.startswith("xn--"):
            decoded = ascii_label.encode("ascii").decode("idna")
            if decoded.encode("idna").decode("ascii").lower() != ascii_label:
                raise UnicodeError("A-label does not round-trip")
        return ascii_label
    except UnicodeError as error:
        raise _invalid("official_domain contains an invalid IDNA label") from error


def platform_key(record: PlatformRecord) -> str:
    """Return the sole canonical key used by identity linking and collectors."""

    if not isinstance(record, PlatformRecord):
        raise _invalid("record must be a PlatformRecord")
    return canonical_platform_key(record.platform, record.platform_id)


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
        source = validate_platform_key(alias.source, "alias source")
        target = validate_platform_key(alias.target, "alias target")
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
