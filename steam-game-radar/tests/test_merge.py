from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import MappingProxyType
from typing import Mapping
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from steam_game_radar.config import RadarConfig
from steam_game_radar.merge import merge_import_with_official
from steam_game_radar.schemas import GameRecord, MetricObservation, RejectedRow


UTC = timezone.utc
NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


def deep_freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: deep_freeze(nested) for key, nested in value.items()}
        )
    if isinstance(value, list):
        return tuple(deep_freeze(item) for item in value)
    return value


class MergeTests(unittest.TestCase):
    def record(
        self,
        appid: int,
        *,
        release_status: str,
        observed_at: str,
        source_kind: str,
        metrics: dict[str, tuple[object, str]] | None = None,
        appdetails: bool = False,
    ) -> GameRecord:
        values = metrics or {"current_players": (100, "players")}
        return GameRecord(
            schema_version=1,
            appid=appid,
            name=f"Game {appid}",
            release_status=release_status,
            store_url=f"https://store.steampowered.com/app/{appid}/",
            metrics={
                name: MetricObservation(
                    value=value,
                    source_id=source_id,
                    source_kind=source_kind,
                    observed_at=observed_at,
                )
                for name, (value, source_id) in values.items()
            },
            source_extra=(
                {"app_type": "game", "genres": ["Action"]}
                if appdetails
                else {}
            ),
        )

    def snapshot(
        self,
        observed_at: datetime,
        records: list[GameRecord],
    ) -> Mapping[str, object]:
        stamp = observed_at.astimezone(UTC)
        run_id = stamp.strftime("%Y%m%dT%H%M%SZ") + "-1234abcd"
        snapshot = {
            "schema_version": 1,
            "run_id": run_id,
            "observed_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "records": [record.to_dict() for record in records],
            "metadata": {"provider": "steam_official"},
        }
        frozen = deep_freeze(snapshot)
        self.assertIsInstance(frozen, Mapping)
        return frozen

    def test_official_fallback_is_inclusive_at_72h_then_manual_only(self) -> None:
        official_time = NOW - timedelta(hours=72)
        official = self.record(
            10,
            release_status="released",
            observed_at=official_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            source_kind="steam_official",
            metrics={"release_date": ("2026-01-01", "steam_appdetails")},
            appdetails=True,
        )
        manual = self.record(
            20,
            release_status="unreleased",
            observed_at="2026-08-24T12:00:00Z",
            source_kind="steamdb_manual_import",
            metrics={"release_date": ("2026-09-10", "steamdb_release_date")},
        )

        eligible = merge_import_with_official(
            [manual], self.snapshot(official_time, [official]), NOW, RadarConfig()
        )
        expired = merge_import_with_official(
            [manual],
            self.snapshot(official_time - timedelta(seconds=1), [official]),
            NOW,
            RadarConfig(),
        )

        self.assertEqual(eligible.mode, "official_plus_manual")
        self.assertEqual([record.appid for record in eligible.records], [10, 20])
        self.assertEqual(expired.mode, "manual_baseline")
        self.assertEqual(expired.data_status, "manual_only")
        self.assertEqual([record.appid for record in expired.records], [20])

    def test_newer_metric_observation_wins(self) -> None:
        official = self.record(
            30,
            release_status="released",
            observed_at="2026-08-24T10:00:00Z",
            source_kind="steam_official",
            metrics={"current_players": (100, "steam_current_players")},
            appdetails=True,
        )
        manual = self.record(
            30,
            release_status="released",
            observed_at="2026-08-24T12:00:00Z",
            source_kind="steamdb_manual_import",
            metrics={
                "current_players": (175, "steamdb_recent_releases_current_players"),
                "release_date": ("2026-08-01", "steamdb_recent_releases_release_date"),
            },
        )

        result = merge_import_with_official(
            [manual], self.snapshot(NOW, [official]), NOW, RadarConfig()
        )

        selected = result.records[0].metrics["current_players"]
        self.assertEqual(selected.value, 175)
        self.assertEqual(selected.source_kind, "steamdb_manual_import")

    def test_official_metric_wins_an_exact_timestamp_tie(self) -> None:
        official = self.record(
            40,
            release_status="released",
            observed_at="2026-08-24T12:00:00Z",
            source_kind="steam_official",
            metrics={"current_players": (100, "steam_current_players")},
            appdetails=True,
        )
        manual = self.record(
            40,
            release_status="released",
            observed_at="2026-08-24T12:00:00Z",
            source_kind="steamdb_manual_import",
            metrics={
                "current_players": (999, "steamdb_recent_releases_current_players"),
                "release_date": ("2026-08-01", "steamdb_recent_releases_release_date"),
            },
        )

        result = merge_import_with_official(
            [manual], self.snapshot(NOW, [official]), NOW, RadarConfig()
        )

        selected = result.records[0].metrics["current_players"]
        self.assertEqual(selected.value, 100)
        self.assertEqual(selected.source_kind, "steam_official")

    def test_official_appdetails_release_status_is_authoritative(self) -> None:
        official = self.record(
            50,
            release_status="released",
            observed_at="2026-08-24T10:00:00Z",
            source_kind="steam_official",
            metrics={"release_date": ("2026-08-01", "steam_appdetails")},
            appdetails=True,
        )
        manual = self.record(
            50,
            release_status="unreleased",
            observed_at="2026-08-24T12:00:00Z",
            source_kind="steamdb_manual_import",
            metrics={"release_date": ("2026-09-01", "steamdb_release_date")},
        )

        result = merge_import_with_official(
            [manual], self.snapshot(NOW, [official]), NOW, RadarConfig()
        )

        self.assertEqual(result.records[0].release_status, "released")
        self.assertEqual(result.rejected_rows, ())

    def test_manual_released_requires_a_non_future_release_date(self) -> None:
        cases = (
            (60, "2026-08-24", True),
            (61, "2026-08-25", False),
        )
        for appid, release_date, accepted in cases:
            with self.subTest(release_date=release_date):
                manual = self.record(
                    appid,
                    release_status="released",
                    observed_at="2026-08-24T12:00:00Z",
                    source_kind="steamdb_manual_import",
                    metrics={
                        "release_date": (release_date, "steamdb_release_date")
                    },
                )
                result = merge_import_with_official(
                    [manual], None, NOW, RadarConfig()
                )
                self.assertEqual(bool(result.records), accepted)
                self.assertEqual(bool(result.rejected_rows), not accepted)

    def test_unresolved_release_status_conflict_rejects_record(self) -> None:
        official = self.record(
            70,
            release_status="released",
            observed_at="2026-08-24T11:00:00Z",
            source_kind="steam_official",
        )
        manual = self.record(
            70,
            release_status="unreleased",
            observed_at="2026-08-24T12:00:00Z",
            source_kind="steamdb_manual_import",
        )

        result = merge_import_with_official(
            [manual], self.snapshot(NOW, [official]), NOW, RadarConfig()
        )

        self.assertEqual(result.records, ())
        self.assertEqual(len(result.rejected_rows), 1)
        self.assertIsInstance(result.rejected_rows[0], RejectedRow)
        self.assertEqual(result.rejected_rows[0].code, "steam_release_status_conflict")
        self.assertEqual(result.rejected_rows[0].appid, 70)

    def test_stale_boundary_and_warning_are_visible_and_stable(self) -> None:
        official = self.record(
            80,
            release_status="released",
            observed_at="2026-08-23T00:00:00Z",
            source_kind="steam_official",
            appdetails=True,
        )
        exact = merge_import_with_official(
            [], self.snapshot(NOW - timedelta(hours=36), [official]), NOW, RadarConfig()
        )
        older = merge_import_with_official(
            [],
            self.snapshot(NOW - timedelta(hours=36, seconds=1), [official]),
            NOW,
            RadarConfig(),
        )

        self.assertEqual(exact.data_status, "fresh")
        self.assertEqual(exact.warnings, ())
        self.assertEqual(older.data_status, "stale")
        self.assertEqual(len(older.warnings), 1)
        self.assertEqual(older.warnings[0].code, "steam_official_snapshot_stale")
        self.assertIn("36 hours", older.warnings[0].message)


if __name__ == "__main__":
    unittest.main()
