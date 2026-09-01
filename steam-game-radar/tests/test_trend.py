from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from types import MappingProxyType
from typing import Mapping
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from steam_game_radar.config import RadarConfig
from steam_game_radar.errors import InputValidationError
from steam_game_radar.schemas import (
    GameRecord,
    MAX_JSON_SAFE_INTEGER,
    MetricObservation,
)
from steam_game_radar.snapshot import load_snapshots, persist_snapshot
from steam_game_radar.trend import (
    AnalyzedCandidate,
    analyze_trends,
    select_rank_improvement,
)


def deep_freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: deep_freeze(nested) for key, nested in value.items()}
        )
    if isinstance(value, list):
        return tuple(deep_freeze(item) for item in value)
    return value


class TrendTests(unittest.TestCase):
    def record(
        self,
        appid: int,
        metrics: dict[str, tuple[object, str, str]],
        *,
        source_kind: str = "steam_official",
    ) -> GameRecord:
        return GameRecord(
            schema_version=1,
            appid=appid,
            name=f"Game {appid}",
            release_status="released",
            store_url=f"https://store.steampowered.com/app/{appid}/",
            metrics={
                name: MetricObservation(
                    value=value,
                    source_id=source_id,
                    source_kind=source_kind,
                    observed_at=observed_at,
                )
                for name, (value, source_id, observed_at) in metrics.items()
            },
            source_extra={"genres": ["Action"]},
        )

    def snapshot(
        self,
        records: list[GameRecord],
        *,
        frozen: bool = True,
        observed_at: str = "2026-08-23T12:00:00Z",
    ) -> Mapping[str, object]:
        stamp = observed_at.replace("-", "").replace(":", "")
        snapshot = {
            "schema_version": 1,
            "run_id": f"{stamp}-1234abcd",
            "observed_at": observed_at,
            "records": [record.to_dict() for record in records],
            "metadata": {},
        }
        if not frozen:
            return snapshot
        frozen_snapshot = deep_freeze(snapshot)
        self.assertIsInstance(frozen_snapshot, Mapping)
        return frozen_snapshot

    def test_deltas_require_the_same_metric_name_and_source_id(self) -> None:
        current = self.record(
            10,
            {"current_players": (150, "steam_current_players", "2026-08-24T12:00:00Z")},
        )
        wrong_source = self.record(
            10,
            {"current_players": (100, "steamdb_players", "2026-08-23T12:00:00Z")},
        )
        wrong_name = self.record(
            10,
            {"peak_players": (100, "steam_current_players", "2026-08-23T12:00:00Z")},
        )
        wrong_kind = self.record(
            10,
            {"current_players": (100, "steam_current_players", "2026-08-23T12:00:00Z")},
            source_kind="steamdb_manual_import",
        )

        for historical in (wrong_source, wrong_name, wrong_kind):
            with self.subTest(metrics=tuple(historical.metrics)):
                candidate = analyze_trends(
                    [current], self.snapshot([historical]), None
                )[0]
                self.assertEqual(dict(candidate.deltas), {})
                self.assertFalse(candidate.newly_observed)

        plain_snapshot = self.snapshot([wrong_source], frozen=False)
        self.assertEqual(
            dict(analyze_trends([current], plain_snapshot, None)[0].deltas),
            {},
        )

        manual_current = self.record(
            11,
            {"current_players": (150, "steamdb_players", "2026-08-24T12:00:00Z")},
            source_kind="steamdb_manual_import",
        )
        manual_history = self.record(
            11,
            {"current_players": (100, "steamdb_players", "2026-08-23T12:00:00Z")},
            source_kind="steamdb_manual_import",
        )
        manual_candidate = analyze_trends(
            [manual_current], self.snapshot([manual_history]), None
        )[0]
        self.assertEqual(
            manual_candidate.deltas["current_players_1d_percent"],
            50.0,
        )

        equal_time = self.record(
            10,
            {"current_players": (100, "steam_current_players", "2026-08-24T12:00:00Z")},
        )
        future_time = self.record(
            10,
            {"current_players": (100, "steam_current_players", "2026-08-25T12:00:00Z")},
        )
        for historical, observed_at in (
            (equal_time, "2026-08-24T12:00:00Z"),
            (future_time, "2026-08-25T12:00:00Z"),
        ):
            with self.subTest(history_time=observed_at):
                candidate = analyze_trends(
                    [current],
                    self.snapshot([historical], observed_at=observed_at),
                    None,
                )[0]
                self.assertEqual(dict(candidate.deltas), {})
                self.assertFalse(candidate.newly_observed)

        after_envelope = self.record(
            10,
            {
                "current_players": (
                    100,
                    "steam_current_players",
                    "2026-08-23T12:00:00.000001Z",
                )
            },
        )
        after_envelope_candidate = analyze_trends(
            [current],
            self.snapshot(
                [after_envelope],
                observed_at="2026-08-23T12:00:00Z",
            ),
            None,
        )[0]
        self.assertEqual(
            after_envelope_candidate.deltas["current_players_1d_percent"],
            50.0,
        )

        with tempfile.TemporaryDirectory(
            dir=str(Path(tempfile.gettempdir()).resolve())
        ) as directory:
            config = RadarConfig(data_dir=Path(directory))
            microsecond_history = self.record(
                12,
                {
                    "current_players": (
                        100,
                        "steam_current_players",
                        "2026-08-23T12:00:00.000001Z",
                    )
                },
            )
            persist_snapshot(
                config,
                "20260823T120000Z-abcdef12",
                [microsecond_history],
                {"kind": "official"},
            )
            loaded = load_snapshots(config)
            microsecond_current = self.record(
                12,
                {
                    "current_players": (
                        150,
                        "steam_current_players",
                        "2026-08-24T12:00:00Z",
                    )
                },
            )
            persisted_candidate = analyze_trends(
                [microsecond_current], loaded[0], None
            )[0]
            self.assertEqual(
                persisted_candidate.deltas["current_players_1d_percent"],
                50.0,
            )

        cycle: dict[str, object] = {}
        cycle["self"] = cycle
        too_deep: object = None
        for _ in range(258):
            too_deep = [too_deep]
        invalid_metadata = (
            {"bad": {"not", "json"}},
            {"bad": object()},
            {1: "non-string key"},
            {"bad": float("nan")},
            {"bad": float("inf")},
            {"bad": MAX_JSON_SAFE_INTEGER + 1},
            cycle,
            {"bad": too_deep},
        )
        for metadata in invalid_metadata:
            invalid = dict(plain_snapshot)
            invalid["metadata"] = metadata
            with self.subTest(metadata=type(metadata).__name__), self.assertRaises(
                InputValidationError
            ):
                analyze_trends([current], invalid, None)

        invalid_top_level = dict(plain_snapshot)
        invalid_top_level["unexpected"] = {"bad": object()}
        with self.assertRaises(InputValidationError):
            analyze_trends([current], invalid_top_level, None)

    def test_one_day_percentage_is_exact_and_zero_baseline_is_omitted(self) -> None:
        current = [
            self.record(
                20,
                {"current_players": (125, "players", "2026-08-24T12:00:00Z")},
            ),
            self.record(
                21,
                {"current_players": (10, "players", "2026-08-24T12:00:00Z")},
            ),
        ]
        historical = [
            self.record(
                20,
                {"current_players": (100, "players", "2026-08-23T12:00:00Z")},
            ),
            self.record(
                21,
                {"current_players": (0, "players", "2026-08-23T12:00:00Z")},
            ),
        ]

        analyzed = analyze_trends(current, self.snapshot(historical), None)

        self.assertEqual(
            analyzed[0].deltas["current_players_1d_percent"], 25.0
        )
        self.assertNotIn("current_players_1d_percent", analyzed[1].deltas)

    def test_seven_day_rank_delta_is_old_rank_minus_current_rank(self) -> None:
        current = self.record(
            30,
            {"most_played_rank": (20, "chart_rank", "2026-08-24T12:00:00Z")},
        )
        historical = self.record(
            30,
            {"most_played_rank": (38, "chart_rank", "2026-08-17T12:00:00Z")},
        )

        candidate = analyze_trends(
            [current], None, self.snapshot([historical])
        )[0]

        self.assertEqual(candidate.deltas["most_played_rank_7d_change"], 18.0)

    def test_no_history_omits_deltas_and_newly_observed_uses_appid_presence(self) -> None:
        current = self.record(
            40,
            {"current_players": (100, "players", "2026-08-24T12:00:00Z")},
        )
        no_history = analyze_trends([current], None, None)[0]
        incompatible_history = self.record(
            40,
            {"peak_players": (100, "peak", "2026-08-23T12:00:00Z")},
        )
        previously_seen = analyze_trends(
            [current], self.snapshot([incompatible_history]), None
        )[0]

        self.assertEqual(dict(no_history.deltas), {})
        self.assertTrue(no_history.newly_observed)
        self.assertEqual(dict(previously_seen.deltas), {})
        self.assertFalse(previously_seen.newly_observed)

        no_time_reference = self.record(41, {})
        historical_without_metrics = self.record(41, {})
        present_without_time_reference = analyze_trends(
            [no_time_reference],
            self.snapshot([historical_without_metrics]),
            None,
        )[0]
        self.assertFalse(present_without_time_reference.newly_observed)

        stale_current = self.record(
            42,
            {
                "current_players": (
                    150,
                    "steam_current_players",
                    "2026-08-22T12:00:00Z",
                )
            },
        )
        selected_history = self.record(
            42,
            {
                "current_players": (
                    100,
                    "steam_current_players",
                    "2026-08-23T12:00:00Z",
                )
            },
        )
        stale_candidate = analyze_trends(
            [stale_current],
            self.snapshot(
                [selected_history],
                observed_at="2026-08-23T12:00:00Z",
            ),
            None,
        )[0]
        self.assertEqual(dict(stale_candidate.deltas), {})
        self.assertFalse(stale_candidate.newly_observed)

    def test_rank_improvement_prefers_7d_then_1d_then_provider_previous(self) -> None:
        record = self.record(
            50,
            {
                "most_played_rank": (
                    20,
                    "steam_most_played_rank",
                    "2026-08-24T12:00:00Z",
                ),
                "previous_rank": (
                    27,
                    "steam_previous_rank",
                    "2026-08-24T12:00:00Z",
                ),
            },
        )
        cases = (
            ({"most_played_rank_7d_change": 11.0, "most_played_rank_1d_change": 8.0}, 11.0),
            ({"most_played_rank_1d_change": 8.0}, 8.0),
            ({"not_a_rank_7d_change": 99.0}, 7.0),
            ({}, 7.0),
        )
        for deltas, expected in cases:
            with self.subTest(deltas=deltas):
                candidate = AnalyzedCandidate(
                    record=record,
                    deltas=deltas,
                    newly_observed=False,
                    warnings=(),
                )
                self.assertEqual(select_rank_improvement(candidate), expected)

        mismatched_time = self.record(
            51,
            {
                "most_played_rank": (
                    20,
                    "steam_most_played_rank",
                    "2026-08-24T12:00:00Z",
                ),
                "previous_rank": (
                    27,
                    "steam_previous_rank",
                    "2026-08-24T11:59:59Z",
                ),
            },
        )
        self.assertIsNone(
            select_rank_improvement(
                AnalyzedCandidate(
                    record=mismatched_time,
                    deltas={},
                    newly_observed=False,
                    warnings=(),
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
