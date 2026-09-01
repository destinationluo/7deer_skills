from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(PROJECT_DIR))

from steam_game_radar.official_provider import (
    AppIdentity,
    DiscoveryCandidate,
    STEAM_APPDETAILS_SOURCE_ID,
    parse_appdetails,
    parse_current_players,
    parse_featured,
    parse_most_played,
)
from steam_game_radar.errors import InputValidationError
from steam_game_radar.schemas import MAX_STEAM_APPID


OBSERVED_AT = "2026-08-24T03:00:00Z"


def load_fixture(name: str) -> object:
    with (FIXTURE_DIR / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def observation_dict(value: int, source_id: str) -> dict[str, object]:
    return {
        "value": value,
        "source_id": source_id,
        "source_kind": "steam_official",
        "observed_at": OBSERVED_AT,
    }


class MostPlayedParserTests(unittest.TestCase):
    def test_most_played_valid_mapping(self) -> None:
        payload = load_fixture("most_played.json")
        result = parse_most_played(payload, OBSERVED_AT)

        self.assertEqual(payload["response"]["ranks"][2]["last_week_rank"], -1)
        self.assertEqual(result.warnings, ())
        self.assertEqual(
            [
                {
                    "appid": candidate.appid,
                    "priority": candidate.priority,
                    "source_names": candidate.source_names,
                    "source_ranks": {
                        name: value.to_dict()
                        for name, value in candidate.source_ranks.items()
                    },
                }
                for candidate in result.value
            ],
            [
                {
                    "appid": 730,
                    "priority": (0, 1, 730),
                    "source_names": ("most_played",),
                    "source_ranks": {
                        "most_played_rank": observation_dict(
                            1, "steam_most_played_rank"
                        ),
                        "previous_rank": observation_dict(
                            2, "steam_previous_rank"
                        ),
                        "peak_players": observation_dict(
                            1_543_210, "steam_peak_players"
                        ),
                    },
                },
                {
                    "appid": 570,
                    "priority": (0, 2, 570),
                    "source_names": ("most_played",),
                    "source_ranks": {
                        "most_played_rank": observation_dict(
                            2, "steam_most_played_rank"
                        ),
                        "previous_rank": observation_dict(
                            1, "steam_previous_rank"
                        ),
                    },
                },
                {
                    "appid": 1_091_500,
                    "priority": (0, 14, 1_091_500),
                    "source_names": ("most_played",),
                    "source_ranks": {
                        "most_played_rank": observation_dict(
                            14, "steam_most_played_rank"
                        ),
                        "peak_players": observation_dict(
                            98_214, "steam_peak_players"
                        ),
                    },
                },
            ],
        )
        payload["response"]["ranks"][0]["rank"] = 999
        self.assertEqual(result.value[0].source_ranks["most_played_rank"].value, 1)
        with self.assertRaises(TypeError):
            result.value[0].source_ranks["new"] = result.value[0].source_ranks[
                "most_played_rank"
            ]
        for invalid_names in ("most_played", b"most_played"):
            with self.subTest(invalid_names=invalid_names), self.assertRaises(
                InputValidationError
            ):
                DiscoveryCandidate(
                    appid=730,
                    priority=(0, 1, 730),
                    source_ranks={},
                    source_names=invalid_names,
                )
        names = ["most_played"]
        isolated = DiscoveryCandidate(
            appid=730,
            priority=(0, 1, 730),
            source_ranks={},
            source_names=names,
        )
        names.append("top_sellers")
        self.assertEqual(isolated.source_names, ("most_played",))
        for invalid_names in ((), ("",)):
            with self.subTest(invalid_names=invalid_names), self.assertRaises(
                InputValidationError
            ):
                DiscoveryCandidate(
                    appid=730,
                    priority=(0, 1, 730),
                    source_ranks={},
                    source_names=invalid_names,
                )
        maximum = DiscoveryCandidate(
            appid=MAX_STEAM_APPID,
            priority=(0, 1, MAX_STEAM_APPID),
            source_ranks={},
            source_names=("most_played",),
        )
        self.assertEqual(maximum.appid, MAX_STEAM_APPID)
        with self.assertRaises(InputValidationError):
            DiscoveryCandidate(
                appid=MAX_STEAM_APPID + 1,
                priority=(0, 1, MAX_STEAM_APPID + 1),
                source_ranks={},
                source_names=("most_played",),
            )

    def test_most_played_missing_optional_fields_remain_absent(self) -> None:
        result = parse_most_played(
            {
                "response": {
                    "ranks": [{"rank": 3, "appid": MAX_STEAM_APPID}]
                }
            },
            OBSERVED_AT,
        )

        self.assertEqual(result.warnings, ())
        self.assertEqual(len(result.value), 1)
        self.assertEqual(result.value[0].appid, MAX_STEAM_APPID)
        self.assertEqual(
            {
                name: value.to_dict()
                for name, value in result.value[0].source_ranks.items()
            },
            {
                "most_played_rank": observation_dict(
                    3, "steam_most_played_rank"
                )
            },
        )
        self.assertNotIn("previous_rank", result.value[0].source_ranks)
        self.assertNotIn("peak_players", result.value[0].source_ranks)

    def test_most_played_malformed_capability_is_all_or_nothing(self) -> None:
        payloads = (
            None,
            {"response": {"ranks": "not-a-list"}},
            {
                "response": {
                    "ranks": [
                        {"rank": 1, "appid": 730},
                        {"rank": True, "appid": 570},
                    ]
                }
            },
            {
                "response": {
                    "ranks": [
                        {"rank": 1, "appid": MAX_STEAM_APPID + 1}
                    ]
                }
            },
            {
                "response": {
                    "ranks": [
                        {"rank": 1, "appid": 730, "last_week_rank": 0}
                    ]
                }
            },
            {
                "response": {
                    "ranks": [
                        {"rank": 1, "appid": 730, "last_week_rank": -2}
                    ]
                }
            },
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                result = parse_most_played(payload, OBSERVED_AT)
                self.assertEqual(result.value, ())
                self.assertEqual(
                    [warning.to_dict() for warning in result.warnings],
                    [
                        {
                            "code": "steam_most_played_malformed",
                            "message": "Steam most-played response is malformed.",
                            "appid": None,
                        }
                    ],
                )
        invalid_time = parse_most_played(
            {"response": {"ranks": []}}, "not-a-timestamp"
        )
        self.assertEqual(invalid_time.value, ())
        self.assertEqual(
            invalid_time.warnings[0].code, "steam_most_played_malformed"
        )


class FeaturedParserTests(unittest.TestCase):
    def test_featured_valid_mapping(self) -> None:
        payload = load_fixture("featured_categories.json")
        result = parse_featured(payload, OBSERVED_AT)

        self.assertEqual(result.warnings, ())
        expected = {
            "top_sellers": [
                (730, (1, 1, 730), "top_seller_rank", "steam_top_seller_rank"),
                (
                    2_715_900,
                    (1, 2, 2_715_900),
                    "top_seller_rank",
                    "steam_top_seller_rank",
                ),
            ],
            "new_releases": [
                (730, (2, 1, 730), "new_release_rank", "steam_new_release_rank"),
                (
                    3_000_002,
                    (2, 2, 3_000_002),
                    "new_release_rank",
                    "steam_new_release_rank",
                ),
            ],
            "coming_soon": [
                (
                    3_000_001,
                    (0, 1, 3_000_001),
                    "coming_soon_rank",
                    "steam_coming_soon_rank",
                ),
                (
                    3_000_003,
                    (0, 2, 3_000_003),
                    "coming_soon_rank",
                    "steam_coming_soon_rank",
                ),
                (
                    3_000_004,
                    (0, 3, 3_000_004),
                    "coming_soon_rank",
                    "steam_coming_soon_rank",
                ),
            ],
        }
        self.assertEqual(set(result.value), set(expected))
        for category, rows in expected.items():
            with self.subTest(category=category):
                candidates = result.value[category]
                self.assertEqual(
                    [
                        (
                            candidate.appid,
                            candidate.priority,
                            next(iter(candidate.source_ranks)),
                            next(iter(candidate.source_ranks.values())).source_id,
                        )
                        for candidate in candidates
                    ],
                    rows,
                )
                self.assertTrue(
                    all(
                        candidate.source_names == (category,)
                        and next(iter(candidate.source_ranks.values())).observed_at
                        == OBSERVED_AT
                        for candidate in candidates
                    )
                )
        payload["top_sellers"]["items"][0]["id"] = 999
        self.assertEqual(result.value["top_sellers"][0].appid, 730)
        with self.assertRaises(TypeError):
            result.value["extra"] = ()

    def test_featured_missing_optional_items_yield_empty_categories(self) -> None:
        result = parse_featured(
            {
                "top_sellers": {"name": "Top Sellers"},
                "new_releases": {"items": []},
                "coming_soon": {},
            },
            OBSERVED_AT,
        )

        self.assertEqual(result.warnings, ())
        self.assertEqual(
            {name: tuple(rows) for name, rows in result.value.items()},
            {"top_sellers": (), "new_releases": (), "coming_soon": ()},
        )
        maximum = parse_featured(
            {
                "top_sellers": {"items": [{"id": MAX_STEAM_APPID}]},
                "new_releases": {},
                "coming_soon": {},
            },
            OBSERVED_AT,
        )
        self.assertEqual(maximum.warnings, ())
        self.assertEqual(maximum.value["top_sellers"][0].appid, MAX_STEAM_APPID)

    def test_featured_malformed_capability_is_all_or_nothing(self) -> None:
        payloads = (
            {"top_sellers": {}, "new_releases": {}},
            {
                "top_sellers": {"items": [{"id": 730}]},
                "new_releases": {"items": [{"id": False}]},
                "coming_soon": {"items": []},
            },
            {
                "top_sellers": {"items": "not-a-list"},
                "new_releases": {},
                "coming_soon": {},
            },
            {
                "top_sellers": {
                    "items": [{"id": MAX_STEAM_APPID + 1}]
                },
                "new_releases": {},
                "coming_soon": {},
            },
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                result = parse_featured(payload, OBSERVED_AT)
                self.assertEqual(
                    {name: tuple(rows) for name, rows in result.value.items()},
                    {"top_sellers": (), "new_releases": (), "coming_soon": ()},
                )
                self.assertEqual(
                    [warning.to_dict() for warning in result.warnings],
                    [
                        {
                            "code": "steam_featured_categories_malformed",
                            "message": (
                                "Steam featured-categories response is malformed."
                            ),
                            "appid": None,
                        }
                    ],
                )
        invalid_time = parse_featured(
            {category: {} for category in ("top_sellers", "new_releases", "coming_soon")},
            "not-a-timestamp",
        )
        self.assertEqual(invalid_time.warnings[0].code, "steam_featured_categories_malformed")


class AppDetailsParserTests(unittest.TestCase):
    def test_appdetails_valid_mapping_and_date_normalization(self) -> None:
        payload = load_fixture("appdetails.json")
        self.assertEqual(STEAM_APPDETAILS_SOURCE_ID, "steam_appdetails")
        expected = (
            (
                730,
                AppIdentity(
                    appid=730,
                    name="Counter-Strike 2",
                    app_type="game",
                    release_status="released",
                    release_date="2012-08-21",
                    genres=("Action", "Free to Play"),
                    observed_at=OBSERVED_AT,
                ),
            ),
            (
                3_000_001,
                AppIdentity(
                    appid=3_000_001,
                    name="Project Aurora",
                    app_type="game",
                    release_status="unreleased",
                    release_date="2026-08-20",
                    genres=("Adventure", "RPG"),
                    observed_at=OBSERVED_AT,
                ),
            ),
            (
                3_000_004,
                AppIdentity(
                    appid=3_000_004,
                    name="Unclassified Steam App",
                    app_type="unclassified",
                    release_status="unreleased",
                    release_date="2026-08-20",
                    genres=("Experimental",),
                    observed_at=OBSERVED_AT,
                ),
            ),
        )
        for appid, identity in expected:
            with self.subTest(appid=appid):
                result = parse_appdetails(appid, payload, OBSERVED_AT)
                self.assertEqual(result.warnings, ())
                self.assertEqual(result.value, identity)
        numeric_dates = (
            ("2026 年 9 月 7 日", "2026-09-07"),
            ("2026年9月7日", "2026-09-07"),
            ("2026년 9월 7일", "2026-09-07"),
            ("2026/09/07", "2026-09-07"),
            ("2026.09.07", "2026-09-07"),
            ("1/nov./2000", "2000-11-01"),
            ("7 sept. 2026", "2026-09-07"),
            ("7. Sept. 2026", "2026-09-07"),
            ("7 FÉVR. 2026", "2026-02-07"),
            ("7. März. 2026", "2026-03-07"),
            ("7 abr. 2026", "2026-04-07"),
            ("7 out. 2026", "2026-10-07"),
        )
        for raw_date, normalized_date in numeric_dates:
            with self.subTest(raw_date=raw_date):
                localized_payload = {
                    "730": {
                        "success": True,
                        "data": {
                            "steam_appid": 730,
                            "name": "Counter-Strike 2",
                            "type": "game",
                            "release_date": {
                                "coming_soon": False,
                                "date": raw_date,
                            },
                        },
                    }
                }
                localized = parse_appdetails(730, localized_payload, OBSERVED_AT)
                self.assertEqual(localized.warnings, ())
                self.assertEqual(localized.value.release_date, normalized_date)
        payload["730"]["data"]["genres"][0]["description"] = "Changed"
        result = parse_appdetails(730, payload, OBSERVED_AT)
        payload["730"]["data"]["genres"][0]["description"] = "Changed Again"
        self.assertEqual(result.value.genres, ("Changed", "Free to Play"))
        maximum_payload = {
            str(MAX_STEAM_APPID): {
                "success": True,
                "data": {
                    "steam_appid": MAX_STEAM_APPID,
                    "name": "Maximum AppID Game",
                    "type": "game",
                },
            }
        }
        maximum = parse_appdetails(
            MAX_STEAM_APPID, maximum_payload, OBSERVED_AT
        )
        self.assertEqual(maximum.warnings, ())
        self.assertEqual(maximum.value.appid, MAX_STEAM_APPID)
        maximum_identity = AppIdentity(
            appid=MAX_STEAM_APPID,
            name="Maximum AppID Game",
            app_type="game",
            release_status="unknown",
            release_date=None,
            genres=(),
            observed_at=OBSERVED_AT,
        )
        self.assertEqual(maximum_identity.appid, MAX_STEAM_APPID)
        with self.assertRaises(InputValidationError):
            AppIdentity(
                appid=MAX_STEAM_APPID + 1,
                name="Out-of-range game",
                app_type="game",
                release_status="unknown",
                release_date=None,
                genres=(),
                observed_at=OBSERVED_AT,
            )

    def test_appdetails_missing_optional_fields_remain_absent(self) -> None:
        payload = {
            "440": {
                "success": True,
                "data": {
                    "steam_appid": 440,
                    "name": "Team Fortress 2",
                    "type": "game",
                },
            }
        }
        result = parse_appdetails(440, payload, OBSERVED_AT)

        self.assertEqual(result.warnings, ())
        self.assertEqual(
            result.value,
            AppIdentity(
                appid=440,
                name="Team Fortress 2",
                app_type="game",
                release_status="unknown",
                release_date=None,
                genres=(),
                observed_at=OBSERVED_AT,
            ),
        )
        for raw_date in (
            "2026年2月30日",
            "07/09/2026",
            "7 xyz. 2026",
        ):
            impossible_date = {
                "440": {
                    "success": True,
                    "data": {
                        "steam_appid": 440,
                        "name": "Team Fortress 2",
                        "type": "game",
                        "release_date": {
                            "coming_soon": False,
                            "date": raw_date,
                        },
                    },
                }
            }
            with self.subTest(raw_date=raw_date):
                invalid_date_result = parse_appdetails(
                    440, impossible_date, OBSERVED_AT
                )
                self.assertEqual(
                    invalid_date_result.value.release_status, "released"
                )
                self.assertIsNone(invalid_date_result.value.release_date)
                self.assertEqual(
                    [
                        warning.to_dict()
                        for warning in invalid_date_result.warnings
                    ],
                    [
                        {
                            "code": "steam_appdetails_release_date_unparsed",
                            "message": (
                                "Steam app-details release date could not be "
                                "normalized."
                            ),
                            "appid": 440,
                        }
                    ],
                )
        for invalid_genres in ("Action", b"Action", ("",)):
            with self.subTest(invalid_genres=invalid_genres), self.assertRaises(
                InputValidationError
            ):
                AppIdentity(
                    appid=440,
                    name="Team Fortress 2",
                    app_type="game",
                    release_status="released",
                    release_date=None,
                    genres=invalid_genres,
                    observed_at=OBSERVED_AT,
                )
        genres = ["Action"]
        isolated = AppIdentity(
            appid=440,
            name="Team Fortress 2",
            app_type="game",
            release_status="released",
            release_date=None,
            genres=genres,
            observed_at=OBSERVED_AT,
        )
        genres.append("Free to Play")
        self.assertEqual(isolated.genres, ("Action",))

        shape_drift_values = (
            "20 Aug, 2026",
            42,
            False,
            ["20 Aug, 2026"],
        )
        for release_date_value in shape_drift_values:
            with self.subTest(release_date_value=release_date_value):
                drift_payload = {
                    "440": {
                        "success": True,
                        "data": {
                            "steam_appid": 440,
                            "name": "Team Fortress 2",
                            "type": "game",
                            "release_date": release_date_value,
                        },
                    }
                }
                drift = parse_appdetails(440, drift_payload, OBSERVED_AT)
                self.assertEqual(drift.value.release_status, "unknown")
                self.assertIsNone(drift.value.release_date)
                self.assertEqual(
                    [warning.to_dict() for warning in drift.warnings],
                    [
                        {
                            "code": "steam_appdetails_release_date_unparsed",
                            "message": (
                                "Steam app-details release date could not be "
                                "normalized."
                            ),
                            "appid": 440,
                        }
                    ],
                )

        empty_release_date_values = (None, "", "   ", [], (), {})
        for release_date_value in empty_release_date_values:
            with self.subTest(empty_release_date_value=release_date_value):
                empty_payload = {
                    "440": {
                        "success": True,
                        "data": {
                            "steam_appid": 440,
                            "name": "Team Fortress 2",
                            "type": "game",
                            "release_date": release_date_value,
                        },
                    }
                }
                empty = parse_appdetails(440, empty_payload, OBSERVED_AT)
                self.assertEqual(empty.value.release_status, "unknown")
                self.assertIsNone(empty.value.release_date)
                self.assertEqual(empty.warnings, ())

    def test_appdetails_malformed_capability_returns_no_identity(self) -> None:
        payloads = (
            {},
            {"730": {"success": False}},
            {
                "730": {
                    "success": True,
                    "data": {"steam_appid": 570, "name": "Wrong", "type": "game"},
                }
            },
            {
                "730": {
                    "success": True,
                    "data": {"steam_appid": 730, "name": "", "type": "game"},
                }
            },
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                result = parse_appdetails(730, payload, OBSERVED_AT)
                self.assertIsNone(result.value)
                self.assertEqual(
                    [warning.to_dict() for warning in result.warnings],
                    [
                        {
                            "code": "steam_appdetails_malformed",
                            "message": "Steam app-details response is malformed.",
                            "appid": 730,
                        }
                    ],
                )
        invalid_time = parse_appdetails(
            730, load_fixture("appdetails.json"), "not-a-timestamp"
        )
        self.assertIsNone(invalid_time.value)
        self.assertEqual(invalid_time.warnings[0].code, "steam_appdetails_malformed")
        oversized_payload = {
            str(MAX_STEAM_APPID + 1): {
                "success": True,
                "data": {
                    "steam_appid": MAX_STEAM_APPID + 1,
                    "name": "Out-of-range game",
                    "type": "game",
                },
            }
        }
        oversized = parse_appdetails(
            MAX_STEAM_APPID + 1,
            oversized_payload,
            OBSERVED_AT,
        )
        self.assertIsNone(oversized.value)
        self.assertEqual(
            [warning.to_dict() for warning in oversized.warnings],
            [
                {
                    "code": "steam_appdetails_malformed",
                    "message": "Steam app-details response is malformed.",
                    "appid": None,
                }
            ],
        )
        oversized_data = parse_appdetails(
            MAX_STEAM_APPID,
            {
                str(MAX_STEAM_APPID): {
                    "success": True,
                    "data": {
                        "steam_appid": MAX_STEAM_APPID + 1,
                        "name": "Out-of-range game",
                        "type": "game",
                    },
                }
            },
            OBSERVED_AT,
        )
        self.assertIsNone(oversized_data.value)
        self.assertEqual(oversized_data.warnings[0].appid, MAX_STEAM_APPID)


class CurrentPlayersParserTests(unittest.TestCase):
    def test_current_players_valid_mapping(self) -> None:
        result = parse_current_players(
            730,
            load_fixture("current_players.json"),
            OBSERVED_AT,
        )

        self.assertEqual(result.warnings, ())
        self.assertEqual(
            result.value.to_dict(),
            observation_dict(1_287_345, "steam_current_players"),
        )
        maximum = parse_current_players(
            MAX_STEAM_APPID,
            load_fixture("current_players.json"),
            OBSERVED_AT,
        )
        self.assertEqual(maximum.warnings, ())
        self.assertEqual(
            maximum.value.to_dict(),
            observation_dict(1_287_345, "steam_current_players"),
        )

    def test_current_players_missing_count_remains_absent(self) -> None:
        result = parse_current_players(
            730,
            {"response": {"result": 1}},
            OBSERVED_AT,
        )

        self.assertIsNone(result.value)
        self.assertEqual(result.warnings, ())

    def test_current_players_malformed_capability_returns_no_observation(self) -> None:
        payloads = (
            None,
            {"response": {"result": 0, "player_count": 123}},
            {"response": {"result": 1, "player_count": True}},
            {"response": {"result": 1, "player_count": -1}},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                result = parse_current_players(730, payload, OBSERVED_AT)
                self.assertIsNone(result.value)
                self.assertEqual(
                    [warning.to_dict() for warning in result.warnings],
                    [
                        {
                            "code": "steam_current_players_malformed",
                            "message": "Steam current-players response is malformed.",
                            "appid": 730,
                        }
                    ],
                )
        invalid_time = parse_current_players(
            730, load_fixture("current_players.json"), "not-a-timestamp"
        )
        self.assertIsNone(invalid_time.value)
        self.assertEqual(
            invalid_time.warnings[0].code, "steam_current_players_malformed"
        )
        oversized = parse_current_players(
            MAX_STEAM_APPID + 1,
            load_fixture("current_players.json"),
            OBSERVED_AT,
        )
        self.assertIsNone(oversized.value)
        self.assertEqual(
            [warning.to_dict() for warning in oversized.warnings],
            [
                {
                    "code": "steam_current_players_malformed",
                    "message": "Steam current-players response is malformed.",
                    "appid": None,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
