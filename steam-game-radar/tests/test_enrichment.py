from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import MappingProxyType
import unittest
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from steam_game_radar.enrichment import (
    EnrichmentBundle,
    EnrichmentRecord,
    Evidence,
    load_enrichment,
)
from steam_game_radar.errors import InputValidationError
from steam_game_radar.schemas import MAX_JSON_SAFE_INTEGER, MAX_STEAM_APPID
import steam_game_radar.enrichment as enrichment_module


RUN_ID = "20260824T030000Z-a1b2c3d4"
SAFE_TEMP_DIR = str(Path(tempfile.gettempdir()).resolve())


class EnrichmentTests(unittest.TestCase):
    def payload(self, **game_overrides: object) -> dict[str, object]:
        game: dict[str, object] = {
            "appid": 730,
            "google_competition_gap_score": 80,
            "expandable_queries": ["counter strike 2 guide"],
            "youtube_relevant_7d": 3,
            "reddit_relevant_7d": 4,
            "reddit_upvotes_7d": 120,
            "evidence": [
                {
                    "source": "google",
                    "url": "https://www.google.com/search?q=counter+strike+2",
                },
                {
                    "source": "youtube",
                    "url": "https://www.youtube.com/results?search_query=cs2",
                },
                {
                    "source": "reddit",
                    "url": "https://www.reddit.com/search/?q=cs2",
                },
            ],
        }
        game.update(game_overrides)
        return {
            "schema_version": 1,
            "run_id": RUN_ID,
            "observed_at": "2026-08-24T03:20:00Z",
            "games": [game],
        }

    def load(self, payload: object, expected_run_id: str = RUN_ID) -> EnrichmentBundle:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            path = Path(directory) / "enrichment.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_enrichment(path, expected_run_id)

    def test_schema_run_id_observed_at_and_deep_freezing(self) -> None:
        bundle = self.load(self.payload())

        self.assertEqual(bundle.schema_version, 1)
        self.assertEqual(bundle.run_id, RUN_ID)
        self.assertIsInstance(bundle.games, MappingProxyType)
        self.assertEqual(tuple(bundle.games), (730,))
        self.assertEqual(bundle.games[730].expandable_queries, ("counter strike 2 guide",))
        self.assertIsInstance(bundle.games[730].evidence, tuple)

        invalid_payloads = []
        for field, value in (
            ("schema_version", 2),
            ("schema_version", True),
            ("run_id", "20260230T030000Z-a1b2c3d4"),
            ("observed_at", "2026-08-24T11:20:00+08:00"),
            ("observed_at", "2026-08-24T03:20:00"),
        ):
            payload = self.payload()
            payload[field] = value
            invalid_payloads.append(payload)
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(InputValidationError):
                self.load(payload)
        with self.assertRaises(InputValidationError):
            self.load(self.payload(), expected_run_id="invalid")
        with self.assertRaises(InputValidationError):
            self.load(self.payload(), expected_run_id="20260824T030001Z-deadbeef")

    def test_scores_and_optional_counts_are_strict_bounded_integers(self) -> None:
        for score in (0, 100):
            with self.subTest(score=score):
                self.assertEqual(
                    self.load(self.payload(google_competition_gap_score=score))
                    .games[730]
                    .google_competition_gap_score,
                    score,
                )
        for invalid in (-1, 101, True, 1.0, None):
            with self.subTest(score=invalid), self.assertRaises(InputValidationError):
                self.load(self.payload(google_competition_gap_score=invalid))

        for field in (
            "youtube_relevant_7d",
            "reddit_relevant_7d",
            "reddit_upvotes_7d",
        ):
            for valid in (None, 0, MAX_JSON_SAFE_INTEGER):
                with self.subTest(field=field, value=valid):
                    self.assertEqual(self.load(self.payload(**{field: valid})).games[730].__getattribute__(field), valid)
            for invalid in (-1, True, 1.0, MAX_JSON_SAFE_INTEGER + 1):
                with self.subTest(field=field, value=invalid), self.assertRaises(InputValidationError):
                    self.load(self.payload(**{field: invalid}))

        for appid in (0, MAX_STEAM_APPID + 1, True):
            with self.subTest(appid=appid), self.assertRaises(InputValidationError):
                self.load(self.payload(appid=appid))

    def test_evidence_requires_safe_https_urls(self) -> None:
        valid = Evidence("google", "https://example.com/search?q=game")
        self.assertEqual(valid.url, "https://example.com/search?q=game")
        self.assertEqual(
            Evidence("google", "https://[2001:db8::1]:443/search").url,
            "https://[2001:db8::1]:443/search",
        )
        self.assertEqual(
            Evidence("google", "https://example.com:443/search").url,
            "https://example.com:443/search",
        )
        for url in (
            "http://example.com/",
            "https:///missing-host",
            "https://user:pass@example.com/",
            "https://example.com/path#fragment",
            "https://example.com/white space",
            "https://example.com:+443/path",
            "https://example.com:0443/path",
            "https://example.com:80/path",
            "https://example.com:/path",
        ):
            with self.subTest(url=url), self.assertRaises(InputValidationError):
                Evidence("google", url)

    def test_every_game_requires_google_evidence(self) -> None:
        evidence = self.payload()["games"][0]["evidence"]  # type: ignore[index]
        without_google = [item for item in evidence if item["source"] != "google"]
        with self.assertRaises(InputValidationError):
            self.load(self.payload(evidence=without_google))

    def test_supplied_youtube_and_reddit_signals_require_typed_evidence(self) -> None:
        google_only = [
            {"source": "google", "url": "https://www.google.com/search?q=game"}
        ]
        for supplied in (
            {"youtube_relevant_7d": 0},
            {"reddit_relevant_7d": 0},
            {"reddit_upvotes_7d": 0},
        ):
            with self.subTest(supplied=supplied), self.assertRaises(InputValidationError):
                self.load(self.payload(evidence=google_only, **supplied))

        no_optional_signals = self.load(
            self.payload(
                youtube_relevant_7d=None,
                reddit_relevant_7d=None,
                reddit_upvotes_7d=None,
                evidence=google_only,
            )
        )
        self.assertEqual(tuple(no_optional_signals.games[730].evidence), (Evidence("google", "https://www.google.com/search?q=game"),))

    def test_unknown_evidence_source_and_duplicate_appids_are_rejected(self) -> None:
        with self.assertRaises(InputValidationError):
            Evidence("steam", "https://example.com/")  # type: ignore[arg-type]
        duplicate = self.payload()
        duplicate["games"] = [duplicate["games"][0], duplicate["games"][0]]  # type: ignore[index]
        with self.assertRaises(InputValidationError):
            self.load(duplicate)

    def test_top_level_and_nested_fields_are_exact(self) -> None:
        extra_top = self.payload()
        extra_top["unexpected"] = True
        extra_game = self.payload(unexpected=True)
        missing_game = self.payload()
        del missing_game["games"][0]["expandable_queries"]  # type: ignore[index]
        invalid_queries = (
            "not-an-array",
            [""],
            ["query", 1],
        )
        for payload in (extra_top, extra_game, missing_game):
            with self.subTest(payload=payload), self.assertRaises(InputValidationError):
                self.load(payload)
        for queries in invalid_queries:
            with self.subTest(queries=queries), self.assertRaises(InputValidationError):
                self.load(self.payload(expandable_queries=queries))

    def test_loader_rejects_unsafe_files_and_malformed_bounded_json(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            valid = root / "valid.json"
            valid.write_text(json.dumps(self.payload()), encoding="utf-8")
            symlink = root / "alias.json"
            symlink.symlink_to(valid)
            with self.assertRaises(InputValidationError):
                load_enrichment(symlink, RUN_ID)
            with self.assertRaises(InputValidationError):
                load_enrichment(root, RUN_ID)

            raced = root / "raced.json"
            displaced = root / "raced-original.json"
            raced.write_text(json.dumps(self.payload()), encoding="utf-8")
            replacement = self.payload(google_competition_gap_score=99)
            real_read = enrichment_module.os.read
            swapped = False

            def replace_name_before_first_read(
                descriptor: int,
                byte_count: int,
            ) -> bytes:
                nonlocal swapped
                if not swapped:
                    raced.rename(displaced)
                    raced.write_text(json.dumps(replacement), encoding="utf-8")
                    swapped = True
                return real_read(descriptor, byte_count)

            with mock.patch.object(
                enrichment_module.os,
                "read",
                side_effect=replace_name_before_first_read,
            ), self.assertRaises(InputValidationError):
                load_enrichment(raced, RUN_ID)
            self.assertTrue(swapped)
            self.assertEqual(
                json.loads(raced.read_text(encoding="utf-8"))["games"][0][
                    "google_competition_gap_score"
                ],
                99,
            )
            self.assertEqual(
                json.loads(displaced.read_text(encoding="utf-8"))["games"][0][
                    "google_competition_gap_score"
                ],
                80,
            )

            malformed = {
                "duplicate.json": '{"schema_version":1,"schema_version":1}',
                "nan.json": '{"schema_version":NaN}',
                "unsafe-int.json": '{"schema_version":9007199254740992}',
                "huge-int.json": '{"schema_version":' + "9" * 250_000 + "}",
                "surrogate.json": json.dumps(
                    self.payload(expandable_queries=["\ud800"])
                ),
                "utf8.json": b"\xff".decode("latin1"),
            }
            for name, text in malformed.items():
                path = root / name
                if name == "utf8.json":
                    path.write_bytes(b"\xff")
                else:
                    path.write_text(text, encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(InputValidationError):
                    load_enrichment(path, RUN_ID)

            deep = root / "deep.json"
            deep.write_text("[" * 300 + "0" + "]" * 300, encoding="utf-8")
            with self.assertRaises(InputValidationError):
                load_enrichment(deep, RUN_ID)

            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * (5 * 1024 * 1024 + 1))
            with self.assertRaises(InputValidationError):
                load_enrichment(oversized, RUN_ID)


if __name__ == "__main__":
    unittest.main()
