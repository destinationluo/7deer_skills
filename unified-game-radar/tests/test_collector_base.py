from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from unified_game_radar.collectors.base import (
    Collector,
    CollectorResult,
    PendingRawPayload,
    classify_source_health,
)
from unified_game_radar.errors import InputValidationError
from unified_game_radar.schemas import (
    PlatformObservation,
    RadarRun,
    RawArtifact,
    SourceHealth,
    WarningRecord,
)


RUN_ID = "20260831T020000Z-a1b2c3d4"
OTHER_RUN_ID = "20260831T030000Z-b1c2d3e4"
NOW = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)


def make_run(
    *,
    run_id: str = RUN_ID,
    platforms: tuple[str, ...] = ("itch", "steam", "roblox"),
) -> RadarRun:
    return RadarRun(
        schema_version=1,
        run_id=run_id,
        started_at=NOW,
        mode="scheduled",
        platforms=platforms,
        publish_daily=False,
    )


def make_observation(
    *,
    platform: str = "steam",
    run_id: str = RUN_ID,
    observed_at: datetime = NOW,
) -> PlatformObservation:
    platform_id = "example-game" if platform == "itch" else "123456"
    timestamp = observed_at.strftime("%Y%m%dT%H%M%SZ")
    return PlatformObservation(
        schema_version=1,
        observation_id=f"{platform}:{platform_id}:discover:{timestamp}",
        run_id=run_id,
        platform=platform,
        platform_id=platform_id,
        provider=f"{platform}_official",
        surface="discover",
        geo="US",
        locale="en",
        query_parameters={},
        metric_definition_version=1,
        observed_at=observed_at,
        release_at=None,
        source_rank=1,
        raw_metrics={"rank": 1},
        evidence_urls=(f"https://example.com/{platform}",),
    )


def make_warning(
    *,
    collector: str | None = "steam",
    code: str = "provider_unavailable",
) -> WarningRecord:
    return WarningRecord(
        schema_version=1,
        code=code,
        message="The provider could not return usable data",
        collector=collector,
        opportunity_id=None,
    )


def make_artifact(*, run_id: str = RUN_ID) -> RawArtifact:
    return RawArtifact(
        schema_version=1,
        run_id=run_id,
        provider="steam_official",
        path="data/unified-game-radar/raw/steam.json",
        observed_at=NOW,
        sha256="a" * 64,
    )


def make_pending_payload(
    *,
    run_id: str = RUN_ID,
    provider: str = "steam_official",
    artifact_name: str = "steam_featured_categories.json",
    observed_at: datetime = NOW,
    payload: object | None = None,
) -> PendingRawPayload:
    return PendingRawPayload(
        run_id=run_id,
        provider=provider,
        artifact_name=artifact_name,
        observed_at=observed_at,
        payload=(
            {"response": {"items": [1, {"token": "secret"}]}}
            if payload is None
            else payload
        ),
    )


def make_health(
    *,
    collector: str = "steam",
    run_id: str = RUN_ID,
    status: str = "fresh",
    warnings: tuple[WarningRecord, ...] = (),
    capabilities: Mapping[str, bool] | None = None,
) -> SourceHealth:
    return SourceHealth(
        schema_version=1,
        run_id=run_id,
        collector=collector,
        status=status,
        observed_at=NOW,
        capabilities=(
            {"listing": True} if capabilities is None else capabilities
        ),
        warnings=warnings,
    )


def classify(**overrides: object) -> SourceHealth:
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "now": NOW,
        "attempted": True,
        "active_observations": (make_observation(),),
        "capabilities": {"listing": True},
        "fallback_observed_at": None,
        "warnings": (),
        "fresh_hours": 6,
        "stale_fallback_hours": 72,
        "collector": "steam",
    }
    values.update(overrides)
    return classify_source_health(**values)  # type: ignore[arg-type]


class CollectorContractTests(unittest.TestCase):
    def test_protocol_accepts_structural_collector(self) -> None:
        class StaticCollector:
            def collect(self, run: RadarRun) -> CollectorResult:
                return CollectorResult(
                    collector="steam",
                    observations=(make_observation(run_id=run.run_id),),
                    health=make_health(run_id=run.run_id),
                    raw_artifacts=(make_artifact(run_id=run.run_id),),
                )

        self.assertIsInstance(StaticCollector(), Collector)

    def test_result_freezes_typed_sequences_and_has_no_second_warning_list(
        self,
    ) -> None:
        warning = make_warning()
        result = CollectorResult(
            collector="steam",
            observations=[make_observation()],  # type: ignore[arg-type]
            health=make_health(warnings=(warning,)),
            raw_artifacts=[make_artifact()],  # type: ignore[arg-type]
            pending_raw_payloads=[make_pending_payload()],  # type: ignore[arg-type]
        )

        self.assertIs(type(result.observations), tuple)
        self.assertIs(type(result.raw_artifacts), tuple)
        self.assertIs(type(result.pending_raw_payloads), tuple)
        self.assertEqual((warning,), result.health.warnings)
        self.assertNotIn("warnings", {field.name for field in fields(result)})
        with self.assertRaises(FrozenInstanceError):
            result.collector = "itch"  # type: ignore[misc]

    def test_result_validates_cross_record_provenance(self) -> None:
        valid = {
            "collector": "steam",
            "observations": (make_observation(),),
            "health": make_health(),
            "raw_artifacts": (make_artifact(),),
        }
        invalid_values = (
            {"collector": "unknown"},
            {"health": make_health(collector="roblox")},
            {"health": make_health(run_id=OTHER_RUN_ID)},
            {"observations": (make_observation(platform="roblox"),)},
            {"observations": (make_observation(run_id=OTHER_RUN_ID),)},
            {"raw_artifacts": (make_artifact(run_id=OTHER_RUN_ID),)},
            {"pending_raw_payloads": (make_pending_payload(run_id=OTHER_RUN_ID),)},
            {
                "pending_raw_payloads": (
                    make_pending_payload(
                        observed_at=NOW - timedelta(seconds=1)
                    ),
                )
            },
            {
                "pending_raw_payloads": (
                    make_pending_payload(provider="roblox_official"),
                )
            },
            {
                "pending_raw_payloads": (
                    make_pending_payload(artifact_name="roblox_charts.json"),
                )
            },
            {"observations": (object(),)},
            {"raw_artifacts": (object(),)},
            {"pending_raw_payloads": (object(),)},
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                values = {**valid, **invalid}
                with self.assertRaises(InputValidationError):
                    CollectorResult(**values)  # type: ignore[arg-type]

    def test_result_rejects_health_warning_for_another_collector(self) -> None:
        with self.assertRaisesRegex(
            InputValidationError,
            "warning collector must match result collector",
        ):
            CollectorResult(
                collector="steam",
                observations=(make_observation(),),
                health=make_health(
                    warnings=(make_warning(collector="roblox"),)
                ),
                raw_artifacts=(),
            )

        result = CollectorResult(
            collector="steam",
            observations=(make_observation(),),
            health=make_health(warnings=(make_warning(collector=None),)),
            raw_artifacts=(),
        )
        self.assertIsNone(result.health.warnings[0].collector)

    def test_result_requires_observations_for_fresh_and_partial_health(self) -> None:
        for health in (
            make_health(status="fresh"),
            make_health(
                status="partial",
                capabilities={"listing": False},
            ),
        ):
            with self.subTest(status=health.status):
                with self.assertRaisesRegex(
                    InputValidationError,
                    "requires at least one observation",
                ):
                    CollectorResult(
                        collector="steam",
                        observations=(),
                        health=health,
                        raw_artifacts=(),
                    )

    def test_result_enforces_fresh_and_partial_capability_meaning(self) -> None:
        invalid_health = (
            make_health(
                status="fresh",
                capabilities={"listing": True, "metadata": False},
            ),
            make_health(
                status="partial",
                capabilities={"listing": True},
            ),
            make_health(status="partial", capabilities={}),
        )
        for health in invalid_health:
            with self.subTest(
                status=health.status,
                capabilities=dict(health.capabilities),
            ):
                with self.assertRaises(InputValidationError):
                    CollectorResult(
                        collector="steam",
                        observations=(make_observation(),),
                        health=health,
                        raw_artifacts=(),
                    )

    def test_result_not_run_rejects_collection_output_or_success(self) -> None:
        valid = {
            "collector": "steam",
            "observations": (),
            "health": make_health(
                status="not_run",
                capabilities={"listing": False},
            ),
            "raw_artifacts": (),
        }
        invalid_values = (
            {"observations": (make_observation(),)},
            {"raw_artifacts": (make_artifact(),)},
            {"pending_raw_payloads": (make_pending_payload(),)},
            {
                "health": make_health(
                    status="not_run",
                    capabilities={"listing": True},
                )
            },
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    InputValidationError,
                    "not_run",
                ):
                    CollectorResult(**{**valid, **invalid})  # type: ignore[arg-type]

        result = CollectorResult(**valid)  # type: ignore[arg-type]
        self.assertEqual("not_run", result.health.status)
        self.assertEqual({"listing": False}, result.health.capabilities)

    def test_pending_raw_payload_is_lossless_and_recursively_immutable(self) -> None:
        original = {
            "authorization": "Bearer secret",
            "nested": [{"cookie": "session"}, True, None, 1.25],
        }

        pending = make_pending_payload(payload=original)
        original["authorization"] = "changed"
        original["nested"][0]["cookie"] = "changed"  # type: ignore[index]

        self.assertEqual(
            pending.payload["authorization"],  # type: ignore[index]
            "Bearer secret",
        )
        nested = pending.payload["nested"]  # type: ignore[index]
        self.assertIs(type(nested), tuple)
        self.assertEqual(nested[0]["cookie"], "session")  # type: ignore[index]
        with self.assertRaises(TypeError):
            pending.payload["authorization"] = "mutate"  # type: ignore[index]
        with self.assertRaises(TypeError):
            nested[0]["cookie"] = "mutate"  # type: ignore[index]

    def test_pending_raw_payload_validates_safe_provenance_and_json(self) -> None:
        invalid_values = (
            {"run_id": "not-a-run"},
            {"provider": "Steam Official"},
            {"artifact_name": "../steam.json"},
            {"artifact_name": "steam_payload.txt"},
            {"observed_at": NOW.replace(tzinfo=None)},
            {"payload": {"bad": object()}},
            {"payload": {"bad": float("nan")}},
            {"payload": {1: "non-string key"}},
        )
        valid = {
            "run_id": RUN_ID,
            "provider": "steam_official",
            "artifact_name": "steam_payload.json",
            "observed_at": NOW,
            "payload": {"ok": True},
        }
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with self.assertRaises(InputValidationError):
                    PendingRawPayload(**{**valid, **invalid})

    def test_pending_payload_secrets_are_excluded_from_record_reprs(self) -> None:
        secret_values = (
            "top-secret-token-value",
            "private-cookie-value",
        )
        pending = make_pending_payload(
            payload={
                "token": secret_values[0],
                "headers": {"cookie": secret_values[1]},
            }
        )
        result = CollectorResult(
            collector="steam",
            observations=(make_observation(),),
            health=make_health(),
            raw_artifacts=(),
            pending_raw_payloads=(pending,),
        )

        for rendered in (repr(pending), repr(result)):
            with self.subTest(rendered=rendered):
                self.assertNotIn("token", rendered)
                self.assertNotIn("cookie", rendered)
                for secret in secret_values:
                    self.assertNotIn(secret, rendered)


class SourceHealthClassificationTests(unittest.TestCase):
    def test_empty_not_attempted_is_not_run(self) -> None:
        health = classify(
            attempted=False,
            active_observations=(),
            capabilities=None,
        )

        self.assertEqual("not_run", health.status)
        self.assertEqual({}, health.capabilities)
        self.assertEqual(NOW, health.observed_at)

    def test_empty_attempted_is_unavailable(self) -> None:
        health = classify(active_observations=(), capabilities={"listing": False})

        self.assertEqual("unavailable", health.status)
        self.assertEqual({"listing": False}, health.capabilities)

    def test_fresh_boundary_is_inclusive_but_just_over_is_unavailable(self) -> None:
        at_boundary = classify(
            active_observations=(
                make_observation(observed_at=NOW - timedelta(hours=6)),
            ),
        )
        just_over = classify(
            active_observations=(
                make_observation(
                    observed_at=NOW - timedelta(hours=6, seconds=1)
                ),
            ),
        )

        self.assertEqual("fresh", at_boundary.status)
        self.assertEqual("unavailable", just_over.status)

    def test_all_active_observations_must_be_nonfuture_and_fresh(self) -> None:
        one_old = classify(
            active_observations=(
                make_observation(observed_at=NOW - timedelta(hours=1)),
                make_observation(observed_at=NOW - timedelta(hours=7)),
            )
        )
        future = classify(
            active_observations=(
                make_observation(observed_at=NOW + timedelta(seconds=1)),
            )
        )

        self.assertEqual("unavailable", one_old.status)
        self.assertEqual("unavailable", future.status)

    def test_failed_capability_makes_fresh_collection_partial(self) -> None:
        health = classify(
            capabilities={"listing": True, "current_players": False}
        )

        self.assertEqual("partial", health.status)
        self.assertEqual(
            {"listing": True, "current_players": False},
            health.capabilities,
        )

    def test_no_declared_capabilities_can_still_be_fresh(self) -> None:
        self.assertEqual("fresh", classify(capabilities=None).status)
        self.assertEqual("fresh", classify(capabilities={}).status)

    def test_only_explicit_fallback_can_make_old_active_data_stale(self) -> None:
        old_active = (
            make_observation(observed_at=NOW - timedelta(hours=7)),
        )
        without_fallback = classify(active_observations=old_active)
        with_fallback = classify(
            active_observations=old_active,
            fallback_observed_at=NOW - timedelta(hours=12),
        )

        self.assertEqual("unavailable", without_fallback.status)
        self.assertEqual("stale", with_fallback.status)

    def test_stale_boundary_is_inclusive_but_just_over_is_unavailable(self) -> None:
        at_boundary = classify(
            active_observations=(),
            fallback_observed_at=NOW - timedelta(hours=72),
        )
        just_over = classify(
            active_observations=(),
            fallback_observed_at=(
                NOW - timedelta(hours=72, microseconds=1)
            ),
        )
        future = classify(
            active_observations=(),
            fallback_observed_at=NOW + timedelta(seconds=1),
        )

        self.assertEqual("stale", at_boundary.status)
        self.assertEqual("unavailable", just_over.status)
        self.assertEqual("unavailable", future.status)

    def test_source_health_owns_the_exact_warning_tuple(self) -> None:
        warning = make_warning()
        health = classify(warnings=[warning])

        self.assertIs(type(health.warnings), tuple)
        self.assertEqual((warning,), health.warnings)

    def test_browser_health_can_move_from_not_run_to_fresh_or_partial(self) -> None:
        idle = classify(
            collector="itch",
            attempted=False,
            active_observations=(),
            capabilities=None,
        )
        fresh = classify(
            collector="itch",
            active_observations=(make_observation(platform="itch"),),
            capabilities={"newest": True},
        )
        partial = classify(
            collector="itch",
            active_observations=(make_observation(platform="itch"),),
            capabilities={"newest": True, "metadata": False},
        )

        self.assertEqual("not_run", idle.status)
        self.assertEqual("fresh", fresh.status)
        self.assertEqual("partial", partial.status)

    def test_failed_provider_does_not_change_independently_classified_source(self) -> None:
        steam_failure = classify(
            active_observations=(),
            capabilities={"listing": False},
            warnings=(make_warning(),),
        )
        roblox_success = classify(
            collector="roblox",
            active_observations=(make_observation(platform="roblox"),),
            capabilities={"discover": True},
        )

        self.assertEqual("unavailable", steam_failure.status)
        self.assertEqual("fresh", roblox_success.status)
        self.assertEqual((), roblox_success.warnings)

    def test_not_attempted_rejects_evidence_of_a_successful_attempt(self) -> None:
        invalid_values = (
            {"active_observations": (make_observation(),)},
            {"fallback_observed_at": NOW - timedelta(hours=1)},
            {"capabilities": {"listing": True}},
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                values = {
                    "attempted": False,
                    "active_observations": (),
                    "capabilities": None,
                    **invalid,
                }
                with self.assertRaises(ValueError):
                    classify(**values)

    def test_rejects_invalid_scalar_inputs(self) -> None:
        invalid_values = (
            {"run_id": "bad"},
            {"now": NOW.replace(tzinfo=None)},
            {"attempted": 1},
            {"fresh_hours": True},
            {"fresh_hours": 0},
            {"stale_fallback_hours": True},
            {"stale_fallback_hours": 6},
            {"collector": "unknown"},
            {"fallback_observed_at": NOW.replace(tzinfo=None)},
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    classify(**invalid)

    def test_rejects_huge_hour_windows_with_stable_value_error(self) -> None:
        invalid_values = (
            {
                "fresh_hours": 10**400,
                "stale_fallback_hours": 10**401,
            },
            {
                "active_observations": (),
                "stale_fallback_hours": 10**400,
            },
        )
        for invalid in invalid_values:
            with self.subTest(field=tuple(invalid)):
                with self.assertRaises(ValueError) as caught:
                    classify(**invalid)
                self.assertIs(type(caught.exception), ValueError)

    def test_rejects_invalid_capability_maps(self) -> None:
        invalid_values: tuple[object, ...] = (
            [],
            {"": True},
            {" listing": True},
            {1: True},
            {"listing": 1},
        )
        for capabilities in invalid_values:
            with self.subTest(capabilities=capabilities):
                with self.assertRaises(ValueError):
                    classify(capabilities=capabilities)

    def test_rejects_wrong_record_types_and_provenance(self) -> None:
        invalid_values = (
            {"active_observations": "not-a-sequence"},
            {"active_observations": (object(),)},
            {
                "active_observations": (
                    make_observation(run_id=OTHER_RUN_ID),
                )
            },
            {
                "collector": "roblox",
                "active_observations": (make_observation(),),
            },
            {"warnings": "not-a-sequence"},
            {"warnings": (object(),)},
            {"warnings": (make_warning(collector="roblox"),)},
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    classify(**invalid)


if __name__ == "__main__":
    unittest.main()
