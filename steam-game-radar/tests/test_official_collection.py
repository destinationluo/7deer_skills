from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from steam_game_radar.artifacts import persist_raw
from steam_game_radar.config import RadarConfig
from steam_game_radar.errors import InputValidationError, ProviderUnavailableError
from steam_game_radar.official_provider import (
    CollectionResult,
    DiscoveryCandidate,
    build_released_candidates,
    build_unreleased_candidates,
    collect_official,
)
from steam_game_radar.schemas import (
    MAX_JSON_SAFE_INTEGER,
    MIN_JSON_SAFE_INTEGER,
    MetricObservation,
    WarningRecord,
)


OBSERVED_AT = "2026-08-24T03:00:00Z"
MOST_PLAYED_URL = (
    "https://api.steampowered.com/"
    "ISteamChartsService/GetMostPlayedGames/v1/"
)


def featured_url(config: RadarConfig) -> str:
    return (
        "https://store.steampowered.com/api/featuredcategories"
        f"?cc={config.country}&l={config.language}"
    )


def appdetails_url(appid: int, config: RadarConfig) -> str:
    return (
        "https://store.steampowered.com/api/appdetails"
        f"?appids={appid}&cc={config.country}&l={config.language}"
    )


def current_players_url(appid: int) -> str:
    return (
        "https://api.steampowered.com/"
        "ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
        f"?appid={appid}"
    )


def metric(value: int, source_id: str) -> MetricObservation:
    return MetricObservation(
        value=value,
        source_id=source_id,
        source_kind="steam_official",
        observed_at=OBSERVED_AT,
    )


def candidate(
    appid: int,
    source: str,
    rank: int,
    source_priority: int,
) -> DiscoveryCandidate:
    metric_name = f"{source}_rank"
    return DiscoveryCandidate(
        appid=appid,
        priority=(source_priority, rank, appid),
        source_ranks={metric_name: metric(rank, f"steam_{metric_name}")},
        source_names=(source,),
    )


def most_played_payload(*appids: int) -> dict[str, object]:
    return {
        "response": {
            "rollup_date": 1_787_529_600,
            "ranks": [
                {
                    "rank": rank,
                    "appid": appid,
                    "last_week_rank": rank + 1,
                    "peak_in_game": 10_000 - rank,
                }
                for rank, appid in enumerate(appids, start=1)
            ],
        }
    }


def featured_payload(
    *,
    top_sellers: tuple[int, ...] = (),
    new_releases: tuple[int, ...] = (),
    coming_soon: tuple[int, ...] = (),
) -> dict[str, object]:
    def category(name: str, appids: tuple[int, ...]) -> dict[str, object]:
        return {
            "id": f"cat_{name}",
            "name": name.replace("_", " ").title(),
            "items": [
                {
                    "id": appid,
                    "type": 0,
                    "name": f"Discovery name {appid}",
                    "discounted": False,
                    "discount_percent": 0,
                    "original_price": 0,
                    "final_price": 0,
                    "currency": "USD",
                    "large_capsule_image": (
                        "https://cdn.cloudflare.steamstatic.com/steam/apps/"
                        f"{appid}/capsule_616x353.jpg"
                    ),
                }
                for appid in appids
            ],
        }

    return {
        "top_sellers": category("top_sellers", top_sellers),
        "new_releases": category("new_releases", new_releases),
        "coming_soon": category("coming_soon", coming_soon),
    }


def appdetails_payload(
    appid: int,
    *,
    app_type: str = "game",
    name: str | None = None,
    coming_soon: bool = False,
    release_date: str = "2026-08-20",
    genres: tuple[str, ...] = ("Action",),
) -> dict[str, object]:
    return {
        str(appid): {
            "success": True,
            "data": {
                "type": app_type,
                "name": name or f"Canonical game {appid}",
                "steam_appid": appid,
                "required_age": 0,
                "is_free": False,
                "short_description": "Complete fake Steam app-details entry.",
                "developers": ["Example Studio"],
                "publishers": ["Example Publisher"],
                "genres": [
                    {"id": str(index), "description": genre}
                    for index, genre in enumerate(genres, start=1)
                ],
                "release_date": {
                    "coming_soon": coming_soon,
                    "date": release_date,
                },
            },
        }
    }


def current_players_payload(count: int | None) -> dict[str, object]:
    response: dict[str, object] = {"result": 1}
    if count is not None:
        response["player_count"] = count
    return {"response": response}


class FakeJsonHttpClient:
    """Network-boundary fake that returns complete response documents."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.real_network_calls = 0

    def get_json(self, url: str) -> object:
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, BaseException):
            raise response
        return response


class OfficialCollectionTests(unittest.TestCase):
    def test_candidate_builders_validate_sort_and_keep_source_priority(self) -> None:
        most_played = (
            candidate(30, "most_played", 2, 0),
            candidate(20, "most_played", 1, 0),
        )
        featured = {
            "top_sellers": (
                candidate(50, "top_seller", 2, 1),
                candidate(40, "top_seller", 1, 1),
            ),
            "new_releases": (
                candidate(70, "new_release", 2, 2),
                candidate(60, "new_release", 1, 2),
            ),
            "coming_soon": (
                candidate(90, "coming_soon", 2, 0),
                candidate(80, "coming_soon", 1, 0),
            ),
        }

        released = build_released_candidates(most_played, featured, 6)
        unreleased = build_unreleased_candidates(featured, 2)

        self.assertEqual([row.appid for row in released], [20, 30, 40, 50, 60, 70])
        self.assertEqual([row.appid for row in unreleased], [80, 90])
        self.assertIsInstance(released, tuple)
        self.assertIsInstance(unreleased, tuple)
        for invalid_limit in (True, 0, -1, 1.5):
            with self.subTest(invalid_limit=invalid_limit), self.assertRaises(
                InputValidationError
            ):
                build_released_candidates((), featured, invalid_limit)
        with self.assertRaises(InputValidationError):
            build_released_candidates("not candidates", featured, 1)
        with self.assertRaises(InputValidationError):
            build_unreleased_candidates({"coming_soon": "invalid"}, 1)

    def test_candidate_union_dedupes_merges_observations_then_applies_cap(self) -> None:
        identical = metric(7, "steam_shared")
        first = DiscoveryCandidate(
            appid=10,
            priority=(0, 2, 10),
            source_ranks={
                "most_played_rank": metric(5, "rank-zulu"),
                "peak_players": metric(100, "peak-zulu"),
                "custom_signal": metric(8, "zulu"),
                "shared_metric": identical,
            },
            source_names=("zeta", "coming_soon"),
        )
        reversed_first = DiscoveryCandidate(
            appid=10,
            priority=(0, 2, 10),
            source_ranks={
                "most_played_rank": metric(3, "rank-alpha"),
                "peak_players": metric(200, "peak-alpha"),
                "custom_signal": metric(9, "alpha"),
                "shared_metric": identical,
            },
            source_names=("most_played", "alpha"),
        )
        top_duplicate = DiscoveryCandidate(
            appid=10,
            priority=(1, 1, 10),
            source_ranks={"top_seller_rank": metric(1, "steam_top_seller_rank")},
            source_names=("top_sellers",),
        )
        new_duplicate = DiscoveryCandidate(
            appid=10,
            priority=(2, 3, 10),
            source_ranks={"new_release_rank": metric(3, "steam_new_release_rank")},
            source_names=("new_releases",),
        )
        featured = {
            "top_sellers": (candidate(11, "top_seller", 2, 1), top_duplicate),
            "new_releases": (candidate(12, "new_release", 1, 2), new_duplicate),
            "coming_soon": (),
        }

        rows = build_released_candidates((first, reversed_first), featured, 2)
        reversed_rows = build_released_candidates(
            (reversed_first, first),
            featured,
            2,
        )

        self.assertEqual(rows, reversed_rows)
        self.assertEqual([row.appid for row in rows], [10, 11])
        self.assertEqual(rows[0].priority, first.priority)
        self.assertEqual(
            rows[0].source_names,
            (
                "most_played",
                "top_sellers",
                "new_releases",
                "coming_soon",
                "alpha",
                "zeta",
            ),
        )
        self.assertEqual(rows[0].source_ranks["most_played_rank"].value, 3)
        self.assertEqual(rows[0].source_ranks["most_played_rank"].source_id, "rank-alpha")
        self.assertEqual(rows[0].source_ranks["peak_players"].value, 200)
        self.assertEqual(rows[0].source_ranks["peak_players"].source_id, "peak-alpha")
        self.assertEqual(rows[0].source_ranks["custom_signal"].source_id, "alpha")
        self.assertIs(rows[0].source_ranks["shared_metric"], identical)
        self.assertEqual(rows[0].source_ranks["top_seller_rank"].value, 1)
        with self.assertRaises(TypeError):
            rows[0].source_ranks["extra"] = metric(1, "extra")

    def test_collection_uses_exact_urls_order_and_request_bound(self) -> None:
        config = RadarConfig(
            country="GB",
            language="schinese",
            released_candidate_limit=3,
            unreleased_candidate_limit=2,
        )
        responses = {
            MOST_PLAYED_URL: most_played_payload(900),
            featured_url(config): featured_payload(
                top_sellers=(100,),
                new_releases=(200,),
                coming_soon=(300,),
            ),
            appdetails_url(900, config): appdetails_payload(900),
            appdetails_url(100, config): appdetails_payload(100),
            appdetails_url(200, config): appdetails_payload(200),
            appdetails_url(300, config): appdetails_payload(
                300, coming_soon=True
            ),
            current_players_url(900): current_players_payload(909),
            current_players_url(100): current_players_payload(101),
            current_players_url(200): current_players_payload(202),
        }
        client = FakeJsonHttpClient(responses)

        result = collect_official(client, config, OBSERVED_AT)

        expected_calls = [
            MOST_PLAYED_URL,
            featured_url(config),
            appdetails_url(900, config),
            appdetails_url(100, config),
            appdetails_url(200, config),
            appdetails_url(300, config),
            current_players_url(900),
            current_players_url(100),
            current_players_url(200),
        ]
        self.assertEqual(client.calls, expected_calls)
        self.assertEqual(
            len(client.calls),
            2 + len({900, 100, 200, 300}) + len(result.released),
        )
        self.assertEqual(client.real_network_calls, 0)
        self.assertEqual([row.appid for row in result.released], [900, 100, 200])
        self.assertEqual([row.appid for row in result.unreleased], [300])

    def test_collection_caps_preliminary_pools_and_fetches_overlap_once(self) -> None:
        config = RadarConfig(
            released_candidate_limit=1,
            unreleased_candidate_limit=1,
        )
        responses = {
            MOST_PLAYED_URL: most_played_payload(10, 11),
            featured_url(config): featured_payload(
                top_sellers=(12,),
                new_releases=(13,),
                coming_soon=(10, 14),
            ),
            appdetails_url(10, config): appdetails_payload(
                10, coming_soon=True
            ),
        }
        client = FakeJsonHttpClient(responses)

        result = collect_official(client, config, OBSERVED_AT)

        self.assertEqual(
            client.calls,
            [MOST_PLAYED_URL, featured_url(config), appdetails_url(10, config)],
        )
        self.assertEqual(result.released, ())
        self.assertEqual([row.appid for row in result.unreleased], [10])
        self.assertEqual(
            result.unreleased[0].source_extra["discovery_sources"],
            ("most_played", "coming_soon"),
        )
        self.assertIn("most_played_rank", result.unreleased[0].metrics)
        self.assertIn("coming_soon_rank", result.unreleased[0].metrics)

    def test_collection_filters_non_games_with_stable_type_warnings(self) -> None:
        app_types = {
            101: ("dlc", "steam_app_type_dlc_excluded", "Steam DLC was excluded."),
            102: ("demo", "steam_app_type_demo_excluded", "Steam demo was excluded."),
            103: (
                "software",
                "steam_app_type_software_excluded",
                "Steam software was excluded.",
            ),
            104: (
                "video",
                "steam_app_type_video_excluded",
                "Steam video was excluded.",
            ),
            105: ("tool", "steam_app_type_tool_excluded", "Steam tool was excluded."),
            106: (
                "unclassified",
                "steam_app_type_unknown_excluded",
                "Steam app with an unknown type was excluded.",
            ),
        }
        unknown_release_appid = 107
        config = RadarConfig(released_candidate_limit=len(app_types) + 1)
        responses = {
            MOST_PLAYED_URL: most_played_payload(
                *(tuple(app_types) + (unknown_release_appid,))
            ),
            featured_url(config): featured_payload(),
        }
        responses.update(
            {
                appdetails_url(appid, config): appdetails_payload(
                    appid, app_type=app_type
                )
                for appid, (app_type, _, _) in app_types.items()
            }
        )
        unknown_release = appdetails_payload(unknown_release_appid)
        del unknown_release[str(unknown_release_appid)]["data"]["release_date"]
        responses[appdetails_url(unknown_release_appid, config)] = unknown_release
        client = FakeJsonHttpClient(responses)

        result = collect_official(client, config, OBSERVED_AT)

        self.assertEqual(result.released, ())
        self.assertEqual(result.unreleased, ())
        self.assertTrue(result.capabilities["appdetails"])
        self.assertEqual(
            [warning.to_dict() for warning in result.warnings],
            [
                {"code": code, "message": message, "appid": appid}
                for appid, (_, code, message) in app_types.items()
            ]
            + [
                {
                    "code": "steam_release_status_unknown_excluded",
                    "message": (
                        "Steam app with an unknown release status was excluded."
                    ),
                    "appid": unknown_release_appid,
                }
            ],
        )
        self.assertTrue(result.capabilities["current_players"])
        self.assertFalse(
            any("GetNumberOfCurrentPlayers" in url for url in client.calls)
        )

    def test_appdetails_identity_release_metadata_and_one_pool_take_precedence(self) -> None:
        config = RadarConfig(released_candidate_limit=2, unreleased_candidate_limit=2)
        responses = {
            MOST_PLAYED_URL: most_played_payload(10),
            featured_url(config): featured_payload(coming_soon=(10, 20)),
            appdetails_url(10, config): appdetails_payload(
                10,
                name="Moved to upcoming",
                coming_soon=True,
                release_date="2026-09-01",
                genres=("RPG", "Strategy"),
            ),
            appdetails_url(20, config): appdetails_payload(
                20,
                name="Already released",
                coming_soon=False,
                release_date="2026-08-01",
                genres=("Action",),
            ),
            current_players_url(20): current_players_payload(20),
        }
        client = FakeJsonHttpClient(responses)

        result = collect_official(client, config, OBSERVED_AT)

        self.assertEqual([row.appid for row in result.released], [20])
        self.assertEqual([row.appid for row in result.unreleased], [10])
        moved = result.unreleased[0]
        self.assertEqual(moved.name, "Moved to upcoming")
        self.assertEqual(moved.release_status, "unreleased")
        self.assertEqual(
            moved.metrics["release_date"].to_dict(),
            {
                "value": "2026-09-01",
                "source_id": "steam_appdetails",
                "source_kind": "steam_official",
                "observed_at": OBSERVED_AT,
            },
        )
        self.assertEqual(
            moved.source_extra,
            {
                "app_type": "game",
                "genres": ("RPG", "Strategy"),
                "discovery_sources": ("most_played", "coming_soon"),
            },
        )
        self.assertEqual(
            moved.to_dict()["source_extra"]["genres"], ["RPG", "Strategy"]
        )
        self.assertEqual(
            moved.store_url, "https://store.steampowered.com/app/10/"
        )
        self.assertEqual(len({row.appid for row in result.released + result.unreleased}), 2)

    def test_current_players_only_targets_retained_released_games_in_order(self) -> None:
        config = RadarConfig(
            released_candidate_limit=2,
            unreleased_candidate_limit=2,
        )
        responses = {
            MOST_PLAYED_URL: most_played_payload(10, 20),
            featured_url(config): featured_payload(coming_soon=(30, 40)),
            appdetails_url(10, config): appdetails_payload(10),
            appdetails_url(20, config): appdetails_payload(20),
            appdetails_url(30, config): appdetails_payload(30),
            appdetails_url(40, config): appdetails_payload(40, app_type="dlc"),
            current_players_url(10): current_players_payload(None),
            current_players_url(20): {"response": {"result": "bad"}},
        }
        client = FakeJsonHttpClient(responses)

        result = collect_official(client, config, OBSERVED_AT)

        current_calls = [
            url for url in client.calls if "GetNumberOfCurrentPlayers" in url
        ]
        self.assertEqual(
            current_calls,
            [current_players_url(10), current_players_url(20)],
        )
        self.assertEqual([row.appid for row in result.released], [10, 20, 30])
        self.assertLessEqual(len(current_calls), config.released_candidate_limit)
        self.assertNotIn("current_players", result.released[0].metrics)
        self.assertNotIn("current_players", result.released[1].metrics)
        self.assertFalse(result.capabilities["current_players"])
        self.assertEqual(
            [warning.code for warning in result.warnings],
            ["steam_app_type_dlc_excluded", "steam_current_players_malformed"],
        )

        missing_config = RadarConfig(released_candidate_limit=1)
        missing_client = FakeJsonHttpClient(
            {
                MOST_PLAYED_URL: most_played_payload(50),
                featured_url(missing_config): featured_payload(),
                appdetails_url(50, missing_config): appdetails_payload(50),
                current_players_url(50): current_players_payload(None),
            }
        )
        missing_result = collect_official(
            missing_client,
            missing_config,
            OBSERVED_AT,
        )
        self.assertTrue(missing_result.capabilities["current_players"])
        self.assertNotIn("current_players", missing_result.released[0].metrics)

    def test_http_failures_are_sanitized_and_independent_raw_is_retained(self) -> None:
        config = RadarConfig(released_candidate_limit=2, unreleased_candidate_limit=1)
        featured = featured_payload(top_sellers=(20, 30), coming_soon=(40,))
        responses = {
            MOST_PLAYED_URL: ProviderUnavailableError(
                "secret https://forbidden.example/token=credential"
            ),
            featured_url(config): featured,
            appdetails_url(20, config): appdetails_payload(20),
            appdetails_url(30, config): ProviderUnavailableError("private payload"),
            appdetails_url(40, config): appdetails_payload(40, coming_soon=True),
            current_players_url(20): ProviderUnavailableError("secret current URL"),
        }
        client = FakeJsonHttpClient(responses)

        result = collect_official(client, config, OBSERVED_AT)

        self.assertEqual(
            result.capabilities,
            {
                "most_played": False,
                "featured_categories": True,
                "appdetails": False,
                "current_players": False,
            },
        )
        self.assertEqual(
            [warning.to_dict() for warning in result.warnings],
            [
                {
                    "code": "steam_most_played_unavailable",
                    "message": "Steam most-played data is unavailable.",
                    "appid": None,
                },
                {
                    "code": "steam_appdetails_unavailable",
                    "message": "Steam app-details data is unavailable.",
                    "appid": 30,
                },
                {
                    "code": "steam_current_players_unavailable",
                    "message": "Steam current-player data is unavailable.",
                    "appid": 20,
                },
            ],
        )
        self.assertEqual(
            list(result.raw),
            ["featured_categories", "appdetails_20", "appdetails_40"],
        )
        self.assertFalse(any("secret" in warning.message for warning in result.warnings))
        featured["top_sellers"]["items"][0]["id"] = 999
        self.assertEqual(
            result.raw["featured_categories"]["top_sellers"]["items"][0]["id"],
            20,
        )
        with self.assertRaises(TypeError):
            result.raw["new"] = {}

        failed_featured_config = RadarConfig()
        failed_featured_client = FakeJsonHttpClient(
            {
                MOST_PLAYED_URL: most_played_payload(),
                featured_url(failed_featured_config): ProviderUnavailableError(
                    "private featured URL"
                ),
            }
        )
        failed_featured = collect_official(
            failed_featured_client,
            failed_featured_config,
            OBSERVED_AT,
        )
        self.assertEqual(
            failed_featured.capabilities,
            {
                "most_played": True,
                "featured_categories": False,
                "appdetails": True,
                "current_players": True,
            },
        )
        self.assertEqual(
            [warning.to_dict() for warning in failed_featured.warnings],
            [
                {
                    "code": "steam_featured_categories_unavailable",
                    "message": "Steam featured-categories data is unavailable.",
                    "appid": None,
                }
            ],
        )
        self.assertEqual(list(failed_featured.raw), ["most_played"])

    def test_parser_capability_failures_and_nonfatal_date_warning_are_stable(self) -> None:
        config = RadarConfig(released_candidate_limit=2)
        malformed_featured = {"top_sellers": {"items": "bad"}}
        nonfatal = appdetails_payload(10, release_date="not a known date")
        malformed_identity = {"20": {"success": True, "data": {"steam_appid": 20}}}
        responses = {
            MOST_PLAYED_URL: most_played_payload(10, 20),
            featured_url(config): malformed_featured,
            appdetails_url(10, config): nonfatal,
            appdetails_url(20, config): malformed_identity,
            current_players_url(10): current_players_payload(10),
        }
        client = FakeJsonHttpClient(responses)

        result = collect_official(client, config, OBSERVED_AT)

        self.assertEqual(
            result.capabilities,
            {
                "most_played": True,
                "featured_categories": False,
                "appdetails": False,
                "current_players": True,
            },
        )
        self.assertEqual(
            [warning.code for warning in result.warnings],
            [
                "steam_featured_categories_malformed",
                "steam_appdetails_release_date_unparsed",
                "steam_appdetails_malformed",
            ],
        )
        self.assertEqual([row.appid for row in result.released], [10])
        self.assertNotIn("release_date", result.released[0].metrics)
        self.assertEqual(
            list(result.raw),
            [
                "most_played",
                "featured_categories",
                "appdetails_10",
                "appdetails_20",
                "current_players_10",
            ],
        )

        nonfatal_config = RadarConfig(released_candidate_limit=1)
        nonfatal_client = FakeJsonHttpClient(
            {
                MOST_PLAYED_URL: most_played_payload(10),
                featured_url(nonfatal_config): featured_payload(),
                appdetails_url(10, nonfatal_config): nonfatal,
                current_players_url(10): current_players_payload(10),
            }
        )
        nonfatal_result = collect_official(
            nonfatal_client,
            nonfatal_config,
            OBSERVED_AT,
        )
        self.assertTrue(nonfatal_result.capabilities["appdetails"])
        self.assertEqual(
            [warning.code for warning in nonfatal_result.warnings],
            ["steam_appdetails_release_date_unparsed"],
        )

    def test_result_and_public_collection_inputs_are_validated_and_immutable(self) -> None:
        raw: dict[str, object] = {
            "payload": {
                "items": [1],
                "minimum": MIN_JSON_SAFE_INTEGER,
                "maximum": MAX_JSON_SAFE_INTEGER,
                "flag": True,
                "ratio": 1.5,
            }
        }
        capabilities = {
            "most_played": True,
            "featured_categories": True,
            "appdetails": True,
            "current_players": True,
        }
        result = CollectionResult(
            released=(),
            unreleased=(),
            capabilities=capabilities,
            warnings=(WarningRecord(code="notice", message="A notice."),),
            raw=raw,
        )
        capabilities["most_played"] = False
        raw["payload"]["items"].append(2)

        self.assertEqual(result.capabilities["most_played"], True)
        self.assertEqual(result.raw["payload"]["items"], (1,))
        first_export = result.raw_to_dict()
        second_export = result.raw_to_dict()
        expected_raw = {
            "payload": {
                "items": [1],
                "minimum": MIN_JSON_SAFE_INTEGER,
                "maximum": MAX_JSON_SAFE_INTEGER,
                "flag": True,
                "ratio": 1.5,
            }
        }
        self.assertEqual(first_export, expected_raw)
        first_export["payload"]["items"].append(99)
        self.assertEqual(result.raw["payload"]["items"], (1,))
        self.assertEqual(second_export, expected_raw)
        self.assertIsNot(first_export, second_export)
        self.assertIsNot(first_export["payload"], second_export["payload"])
        with tempfile.TemporaryDirectory() as directory:
            persisted = persist_raw(
                RadarConfig(data_dir=Path(directory)),
                "20260824T030405Z-1234abcd",
                "steam_official",
                result.raw_to_dict(),
                datetime(2026, 8, 24, 3, 4, 5, tzinfo=timezone.utc),
            )
            self.assertEqual(
                json.loads(persisted.read_text(encoding="utf-8")),
                expected_raw,
            )
        self.assertIsInstance(result.released, tuple)
        self.assertIsInstance(result.warnings, tuple)
        with self.assertRaises(TypeError):
            result.capabilities["most_played"] = False
        with self.assertRaises(InputValidationError):
            CollectionResult((), (), {"most_played": True}, (), {})
        invalid_raw_values = (
            {"nested": [MAX_JSON_SAFE_INTEGER + 1]},
            {"nested": {"minimum": MIN_JSON_SAFE_INTEGER - 1}},
        )
        for invalid_raw in invalid_raw_values:
            with self.subTest(invalid_raw=invalid_raw), self.assertRaises(
                InputValidationError
            ):
                CollectionResult(
                    released=(),
                    unreleased=(),
                    capabilities={name: True for name in capabilities},
                    warnings=(),
                    raw=invalid_raw,
                )
        with self.assertRaises(InputValidationError):
            collect_official(object(), RadarConfig(), OBSERVED_AT)
        with self.assertRaises(InputValidationError):
            collect_official(FakeJsonHttpClient({}), object(), OBSERVED_AT)
        with self.assertRaises(InputValidationError):
            collect_official(FakeJsonHttpClient({}), RadarConfig(), "invalid")

        class BrokenClient:
            def get_json(self, url: str) -> object:
                del url
                raise RuntimeError("programming error")

        with self.assertRaisesRegex(RuntimeError, "programming error"):
            collect_official(BrokenClient(), RadarConfig(), OBSERVED_AT)


if __name__ == "__main__":
    unittest.main()
