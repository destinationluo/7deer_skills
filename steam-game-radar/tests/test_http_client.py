from __future__ import annotations

from email.message import Message
import io
import json
from pathlib import Path
import socket
import sys
import unittest
import urllib.error
import urllib.request


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from steam_game_radar.config import RadarConfig
from steam_game_radar.errors import InputValidationError, ProviderUnavailableError
from steam_game_radar.http_client import (
    ALLOWED_HOSTS,
    JsonHttpClient,
    NoRedirectHandler,
    validate_steam_url,
)


USER_AGENT = (
    "7deer-steam-game-radar/1 "
    "(+https://github.com/destinationluo/7deer_skills)"
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.headers = Message()
        self.url = "https://api.steampowered.com/example"
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        return self.body if amount < 0 else self.body[:amount]

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class FakeOpener:
    def __init__(self, outcomes: list[object], clock: FakeClock | None = None) -> None:
        self.outcomes = list(outcomes)
        self.clock = clock
        self.calls: list[tuple[object, float, float | None]] = []

    def open(self, request: object, *, timeout: float) -> FakeResponse:
        started_at = None if self.clock is None else self.clock.monotonic()
        self.calls.append((request, timeout, started_at))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if not isinstance(outcome, FakeResponse):
            raise TypeError("fake outcome must be a response or exception")
        return outcome


def http_error(url: str, status: int, *, location: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if location is not None:
        headers["Location"] = location
    return urllib.error.HTTPError(
        url,
        status,
        f"HTTP {status}",
        headers,
        io.BytesIO(b'{"error":"do not leak this body"}'),
    )


def client_for(
    opener: FakeOpener,
    *,
    clock: FakeClock | None = None,
    max_retries: int = 3,
    max_bytes: int = 1024,
    interval: float = 1.0,
) -> JsonHttpClient:
    active_clock = FakeClock() if clock is None else clock
    config = RadarConfig(
        max_retries=max_retries,
        raw_max_bytes_per_provider=max_bytes,
        minimum_request_interval_seconds=interval,
        request_timeout_seconds=7.5,
    )
    return JsonHttpClient(
        config,
        opener=opener,
        monotonic=active_clock.monotonic,
        sleep=active_clock.sleep,
    )


class HttpClientTests(unittest.TestCase):
    def test_url_validation_allows_only_exact_https_hosts_without_explicit_port(self) -> None:
        self.assertEqual(
            ALLOWED_HOSTS,
            frozenset({"api.steampowered.com", "store.steampowered.com"}),
        )
        for url in (
            "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/",
            "https://STORE.STEAMPOWERED.COM/api/appdetails?appids=10",
        ):
            with self.subTest(valid=url):
                self.assertIn(validate_steam_url(url).hostname, ALLOWED_HOSTS)

        invalid_urls = (
            "http://api.steampowered.com/example",
            "https://user@api.steampowered.com/example",
            "https://api.steampowered.com:443/example",
            "https://store.steampowered.com:8443/example",
            "https://example.com/",
            "https://steamdb.info/charts/",
            "https://api.steampowered.com/example#fragment",
        )
        for url in invalid_urls:
            with self.subTest(invalid=url), self.assertRaises(InputValidationError):
                validate_steam_url(url)

    def test_redirect_to_steamdb_is_terminal_and_redirect_handler_never_follows(self) -> None:
        url = "https://api.steampowered.com/example"
        opener = FakeOpener(
            [http_error(url, 302, location="https://steamdb.info/charts/")]
        )
        client = client_for(opener, max_retries=4)

        with self.assertRaises(ProviderUnavailableError):
            client.get_json(url)

        self.assertEqual(len(opener.calls), 1)
        handler = NoRedirectHandler()
        request = urllib.request.Request(url)
        self.assertIsNone(
            handler.redirect_request(
                request,
                io.BytesIO(b""),
                302,
                "Found",
                {"Location": "https://steamdb.info/charts/"},
                "https://steamdb.info/charts/",
            )
        )

    def test_timeout_failures_retry_and_can_recover(self) -> None:
        url = "https://api.steampowered.com/example"
        opener = FakeOpener(
            [
                socket.timeout("timed out"),
                urllib.error.URLError(TimeoutError("timed out")),
                FakeResponse(b'{"ok":true}'),
            ]
        )
        client = client_for(opener, max_retries=2)

        self.assertEqual(client.get_json(url), {"ok": True})
        self.assertEqual(len(opener.calls), 3)

    def test_http_429_and_5xx_are_retryable(self) -> None:
        url = "https://api.steampowered.com/example"
        for status in (429, 500, 503):
            with self.subTest(status=status):
                opener = FakeOpener(
                    [http_error(url, status), FakeResponse(b'{"ok":true}')]
                )
                client = client_for(opener, max_retries=1)

                self.assertEqual(client.get_json(url), {"ok": True})
                self.assertEqual(len(opener.calls), 2)

    def test_http_400_is_terminal_without_retry(self) -> None:
        url = "https://api.steampowered.com/example"
        opener = FakeOpener(
            [http_error(url, 400), FakeResponse(b'{"unexpected":true}')]
        )
        client = client_for(opener, max_retries=3)

        with self.assertRaises(ProviderUnavailableError) as captured:
            client.get_json(url)

        self.assertEqual(len(opener.calls), 1)
        self.assertNotIn("do not leak", str(captured.exception))

        connection_error = ConnectionError("private connection detail")
        opener = FakeOpener(
            [connection_error, FakeResponse(b'{"unexpected":true}')]
        )
        client = client_for(opener, max_retries=3)
        with self.assertRaises(ProviderUnavailableError) as captured:
            client.get_json(url)
        self.assertEqual(len(opener.calls), 1)
        self.assertIs(captured.exception.__cause__, connection_error)
        self.assertNotIn("private connection detail", str(captured.exception))

    def test_retry_exhaustion_uses_exactly_initial_attempt_plus_max_retries(self) -> None:
        url = "https://api.steampowered.com/example"
        clock = FakeClock()
        final_timeout = socket.timeout("private timeout detail")
        opener = FakeOpener(
            [
                socket.timeout("one"),
                socket.timeout("two"),
                socket.timeout("three"),
                final_timeout,
            ],
            clock,
        )
        client = client_for(
            opener,
            clock=clock,
            max_retries=3,
            interval=1.5,
        )

        with self.assertRaises(ProviderUnavailableError) as captured:
            client.get_json(url)

        self.assertEqual(len(opener.calls), 4)
        self.assertEqual(
            [call[2] for call in opener.calls],
            [100.0, 101.5, 103.5, 107.5],
        )
        self.assertEqual(clock.sleeps, [1, 0.5, 2, 4])
        self.assertIs(captured.exception.__cause__, final_timeout)
        self.assertNotIn("private timeout detail", str(captured.exception))

    def test_request_starts_are_spaced_by_configured_interval_across_calls(self) -> None:
        clock = FakeClock()
        opener = FakeOpener(
            [FakeResponse(b'{"call":1}'), FakeResponse(b'{"call":2}')],
            clock,
        )
        client = client_for(opener, clock=clock, interval=1.0)
        url = "https://store.steampowered.com/api/appdetails?appids=10"

        self.assertEqual(client.get_json(url), {"call": 1})
        self.assertEqual(client.get_json(url), {"call": 2})

        self.assertEqual([call[2] for call in opener.calls], [100.0, 101.0])
        self.assertEqual(clock.sleeps, [1.0])

    def test_request_uses_exact_user_agent_and_configured_timeout(self) -> None:
        opener = FakeOpener([FakeResponse(b'{"players":42}')])
        client = client_for(opener)

        self.assertEqual(
            client.get_json("https://api.steampowered.com/example"),
            {"players": 42},
        )

        request, timeout, _ = opener.calls[0]
        self.assertEqual(request.full_url, "https://api.steampowered.com/example")
        self.assertEqual(request.get_header("User-agent"), USER_AGENT)
        self.assertEqual(timeout, 7.5)

    def test_response_size_utf8_and_json_errors_are_terminal(self) -> None:
        url = "https://api.steampowered.com/example"
        cases = (
            (FakeResponse(b"123456"), 5),
            (FakeResponse(b'"\xff"'), 1024),
            (FakeResponse(b'{"broken":'), 1024),
            (FakeResponse(b"NaN"), 1024),
            (FakeResponse(b"Infinity"), 1024),
            (FakeResponse(b"-Infinity"), 1024),
        )
        for response, max_bytes in cases:
            with self.subTest(body=response.body, max_bytes=max_bytes):
                opener = FakeOpener(
                    [response, FakeResponse(b'{"unexpected":true}')]
                )
                client = client_for(opener, max_retries=3, max_bytes=max_bytes)

                with self.assertRaises(ProviderUnavailableError):
                    client.get_json(url)

                self.assertEqual(len(opener.calls), 1)
                self.assertTrue(response.closed)

    def test_json_response_is_parsed_from_utf8(self) -> None:
        payload = {"name": "游戏", "rank": [1, 2]}
        response = FakeResponse(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        opener = FakeOpener([response])

        self.assertEqual(
            client_for(opener).get_json(
                "https://store.steampowered.com/api/appdetails?appids=10"
            ),
            payload,
        )
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
