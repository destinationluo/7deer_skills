"""Restricted JSON HTTP transport for official Steam endpoints."""

from __future__ import annotations

import http.client
import json
import socket
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request

from .config import RadarConfig
from .errors import InputValidationError, ProviderUnavailableError


ALLOWED_HOSTS = frozenset(
    {"api.steampowered.com", "store.steampowered.com"}
)
USER_AGENT = (
    "7deer-steam-game-radar/1 "
    "(+https://github.com/destinationluo/7deer_skills)"
)


def validate_steam_url(url: str) -> urllib.parse.SplitResult:
    """Validate a URL against the official Steam transport allowlist."""

    if not isinstance(url, str) or not url:
        raise InputValidationError("Steam URL must be a non-empty string")
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        explicit_port = parsed.port is not None
    except ValueError as error:
        raise InputValidationError("invalid Steam URL") from error

    if parsed.scheme.lower() != "https":
        raise InputValidationError("Steam URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise InputValidationError("Steam URL must not contain user information")
    if hostname is None or hostname.lower() not in ALLOWED_HOSTS:
        raise InputValidationError("Steam URL host is not allowed")
    if explicit_port or parsed.netloc.lower() != hostname.lower():
        raise InputValidationError("Steam URL must not contain an explicit port")
    if parsed.fragment:
        raise InputValidationError("Steam URL must not contain a fragment")
    return parsed


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disable urllib's automatic redirect following."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


class JsonHttpClient:
    """Fetch JSON with bounded retries, rate limiting, and no redirects."""

    def __init__(
        self,
        config: RadarConfig,
        *,
        opener: object | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._opener = (
            urllib.request.build_opener(NoRedirectHandler())
            if opener is None
            else opener
        )
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request_started: float | None = None

    def get_json(self, url: str) -> object:
        """Fetch and parse one allowlisted HTTPS JSON document."""

        validate_steam_url(url)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT},
            method="GET",
        )
        attempts = 1 + self._config.max_retries
        for attempt in range(attempts):
            self._wait_for_request_slot()
            try:
                return self._open_and_decode(request)
            except urllib.error.HTTPError as error:
                retryable = error.code == 429 or 500 <= error.code <= 599
                _close_quietly(error)
                if not retryable:
                    raise ProviderUnavailableError(
                        f"Steam provider returned terminal HTTP status {error.code}"
                    ) from error
                if attempt == attempts - 1:
                    raise ProviderUnavailableError(
                        f"Steam provider failed after {attempts} attempts"
                    ) from error
            except (socket.timeout, TimeoutError) as error:
                if attempt == attempts - 1:
                    raise ProviderUnavailableError(
                        f"Steam provider timed out after {attempts} attempts"
                    ) from error
            except urllib.error.URLError as error:
                if not _url_error_is_timeout(error):
                    raise ProviderUnavailableError(
                        "Steam provider request failed"
                    ) from error
                if attempt == attempts - 1:
                    raise ProviderUnavailableError(
                        f"Steam provider timed out after {attempts} attempts"
                    ) from error
            except OSError as error:
                raise ProviderUnavailableError(
                    "Steam provider request failed"
                ) from error
            except http.client.HTTPException as error:
                raise ProviderUnavailableError(
                    "Steam provider response could not be read"
                ) from error
            self._sleep(2**attempt)

        raise AssertionError("unreachable retry state")

    def _wait_for_request_slot(self) -> None:
        now = self._monotonic()
        if self._last_request_started is not None:
            wait_seconds = (
                self._last_request_started
                + self._config.minimum_request_interval_seconds
                - now
            )
            if wait_seconds > 0:
                self._sleep(wait_seconds)
                now = self._monotonic()
        self._last_request_started = now

    def _open_and_decode(self, request: urllib.request.Request) -> object:
        with self._opener.open(
            request,
            timeout=self._config.request_timeout_seconds,
        ) as response:
            status = response.getcode()
            if status is not None and 300 <= status <= 399:
                raise ProviderUnavailableError(
                    f"Steam provider returned terminal HTTP status {status}"
                )
            body = response.read(self._config.raw_max_bytes_per_provider + 1)
            if len(body) > self._config.raw_max_bytes_per_provider:
                raise ProviderUnavailableError(
                    "Steam provider response exceeds configured size"
                )
            try:
                text = body.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise ProviderUnavailableError(
                    "Steam provider response is not valid UTF-8"
                ) from error
            try:
                return json.loads(text, parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, ValueError, RecursionError) as error:
                raise ProviderUnavailableError(
                    "Steam provider response is not valid JSON"
                ) from error


def _url_error_is_timeout(error: urllib.error.URLError) -> bool:
    return isinstance(error.reason, (socket.timeout, TimeoutError))


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("non-standard JSON constant")


def _close_quietly(value: object) -> None:
    close = getattr(value, "close", None)
    if close is None:
        return
    try:
        close()
    except OSError:
        pass
