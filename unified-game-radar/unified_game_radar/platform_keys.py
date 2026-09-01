"""Canonical platform identifiers shared by configuration and wire schemas."""

from __future__ import annotations

import re

from .errors import InputValidationError


MAX_SAFE_PLATFORM_ID = 2**53 - 1
SUPPORTED_PLATFORMS = frozenset({"itch", "steam", "roblox"})
_MAX_SAFE_PLATFORM_ID_TEXT = str(MAX_SAFE_PLATFORM_ID)

_NUMERIC_ID = re.compile(r"[1-9][0-9]*\Z", flags=re.ASCII)
_ITCH_ID = re.compile(
    r"[a-z0-9]+(?:[-_.][a-z0-9]+)*\Z",
    flags=re.ASCII,
)


def _invalid(message: str) -> InputValidationError:
    return InputValidationError(message)


def validate_platform(value: object, name: str = "platform") -> str:
    """Return an exact supported platform name."""

    if not isinstance(value, str) or value not in SUPPORTED_PLATFORMS:
        raise _invalid(f"{name} must be itch, steam, or roblox")
    return value


def validate_platform_id(
    platform: object,
    platform_id: object,
    name: str = "platform_id",
) -> str:
    """Return an exact ID that is canonical for the selected platform."""

    parsed_platform = validate_platform(platform)
    if not isinstance(platform_id, str) or not platform_id:
        raise _invalid(f"{name} must be nonempty text")

    if parsed_platform in {"steam", "roblox"}:
        if _NUMERIC_ID.fullmatch(platform_id) is None:
            raise _invalid(
                f"{parsed_platform} {name} must be a positive ASCII integer"
            )
        if (
            len(platform_id) > len(_MAX_SAFE_PLATFORM_ID_TEXT)
            or (
                len(platform_id) == len(_MAX_SAFE_PLATFORM_ID_TEXT)
                and platform_id > _MAX_SAFE_PLATFORM_ID_TEXT
            )
        ):
            raise _invalid(
                f"{parsed_platform} {name} exceeds the JSON safe integer limit"
            )
        return platform_id

    if len(platform_id) > 128 or _ITCH_ID.fullmatch(platform_id) is None:
        raise _invalid(
            "itch platform_id must be a 1-128 character lowercase slug "
            "with single internal -, _, or . separators"
        )
    return platform_id


def canonical_platform_key(platform: object, platform_id: object) -> str:
    """Validate a platform/ID pair and return its canonical key."""

    parsed_platform = validate_platform(platform)
    parsed_id = validate_platform_id(parsed_platform, platform_id)
    return f"{parsed_platform}:{parsed_id}"


def parse_platform_key(
    value: object,
    name: str = "platform_key",
) -> tuple[str, str]:
    """Validate exact canonical key text and return ``(platform, ID)``."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise _invalid(f"{name} must be an exact platform key")
    platform, separator, platform_id = value.partition(":")
    if not separator or not platform_id or ":" in platform_id:
        raise _invalid(f"{name} must be an exact platform key")
    canonical = canonical_platform_key(platform, platform_id)
    if value != canonical:
        raise _invalid(f"{name} must be an exact platform key")
    return platform, platform_id


def validate_platform_key(value: object, name: str = "platform_key") -> str:
    """Return exact canonical platform-key text."""

    platform, platform_id = parse_platform_key(value, name)
    return f"{platform}:{platform_id}"


__all__ = [
    "MAX_SAFE_PLATFORM_ID",
    "SUPPORTED_PLATFORMS",
    "canonical_platform_key",
    "parse_platform_key",
    "validate_platform",
    "validate_platform_id",
    "validate_platform_key",
]
