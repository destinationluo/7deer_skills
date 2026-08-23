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
    STEAM_APPDETAILS_SOURCE_ID,
    parse_appdetails,
    parse_current_players,
    parse_featured,
    parse_most_played,
)


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

    def test_most_played_missing_optional_fields_remain_absent(self) -> None:
        result = parse_most_played(
            {"response": {"ranks": [{"rank": 3, "appid": 440}]}},
            OBSERVED_AT,
        )

        self.assertEqual(result.warnings, ())
        self.assertEqual(len(result.value), 1)
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
        payload["730"]["data"]["genres"][0]["description"] = "Changed"
        result = parse_appdetails(730, payload, OBSERVED_AT)
        payload["730"]["data"]["genres"][0]["description"] = "Changed Again"
        self.assertEqual(result.value.genres, ("Changed", "Free to Play"))

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


if __name__ == "__main__":
    unittest.main()
