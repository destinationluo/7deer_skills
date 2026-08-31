from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
import unittest
from uuid import UUID


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from unified_game_radar.errors import InputValidationError
from unified_game_radar.schemas import (
    CommandManifest,
    ExternalEvidence,
    GameIdentity,
    NormalizedHeat,
    ObservationEnvelope,
    OpportunityEvidence,
    OutstandingTask,
    PlatformHeat,
    PlatformObservation,
    PlatformRecord,
    PreliminaryResult,
    Publication,
    RadarRun,
    RawArtifact,
    ScoredOpportunity,
    SearchQueryEvidence,
    SerpEvidence,
    SourceHealth,
    TrendEvidence,
    TrendPoint,
    WarningRecord,
)


RUN_ID = "20260831T020000Z-a1b2c3d4"
OPPORTUNITY_ID = "0f840f6f-5c62-4ca6-9d53-e0be9ab2740b"
OBSERVATION_ID = "steam:123456:most_played:20260831T020000Z"
OBSERVED_AT = datetime(2026, 8, 31, 2, tzinfo=timezone.utc)
PUBLISHED_AT = datetime(2026, 8, 31, 6, tzinfo=timezone.utc)


def build_instances() -> tuple[object, ...]:
    warning = WarningRecord(
        schema_version=1,
        code="missing_metric",
        message="Current players were unavailable",
        collector="steam",
        opportunity_id=OPPORTUNITY_ID,
    )
    platform_record = PlatformRecord(
        schema_version=1,
        platform="steam",
        platform_id="123456",
        name="Example Game",
        developer="Example Studio",
        official_domain="example.com",
        url="https://store.steampowered.com/app/123456/",
    )
    identity = GameIdentity(
        schema_version=1,
        opportunity_id=OPPORTUNITY_ID,
        name="Example Game",
        normalized_name="example game",
        developer="Example Studio",
        official_domain="example.com",
        platform_records=(platform_record,),
    )
    observation = PlatformObservation(
        schema_version=1,
        observation_id=OBSERVATION_ID,
        run_id=RUN_ID,
        platform="steam",
        platform_id="123456",
        provider="steam_official",
        surface="most_played",
        geo="US",
        locale="en",
        query_parameters={"cc": "US", "filters": {"tags": ["co-op"]}},
        metric_definition_version=1,
        observed_at=OBSERVED_AT,
        release_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        source_rank=12,
        raw_metrics={
            "current_players": None,
            "review_count": 250,
            "provenance": {"surfaces": ["most_played"]},
        },
        evidence_urls=("https://store.steampowered.com/charts/mostplayed",),
    )
    envelope = ObservationEnvelope(
        schema_version=1,
        run_id=RUN_ID,
        collector="steam",
        surface="most_played",
        geo="US",
        locale="en",
        metric_definition_version=1,
        observations=(observation,),
    )
    trend_point = TrendPoint(
        date=date(2026, 8, 30),
        value=32.5,
        complete=True,
    )
    trends = TrendEvidence(
        query="Example Game game",
        query_type="search_term",
        timeframe="now 7-d",
        geo="US",
        category=0,
        property="web",
        timezone="America/Los_Angeles",
        points=(trend_point,),
        comparison_term=None,
        comparison_average=None,
        evidence_url="https://trends.google.com/trends/explore?q=example",
        raw_artifact="data/unified-game-radar/raw/trends.json",
        observed_at=PUBLISHED_AT,
    )
    suggestion = SearchQueryEvidence(
        schema_version=1,
        query="example game codes",
        observed_at=PUBLISHED_AT,
        source_url="https://www.google.com/complete/search?q=example",
    )
    external = ExternalEvidence(
        source="youtube.com",
        url="https://www.youtube.com/watch?v=example",
        published_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        observed_at=PUBLISHED_AT,
        author_relation="independent",
        engagement_count=None,
        evidence_kind="gameplay_video",
    )
    serp = SerpEvidence(
        query="Example Game game",
        relevant_nonofficial_results=None,
        guide_results=0,
        missing_intents=("guide", "codes"),
        evidence_url="https://www.google.com/search?q=Example+Game+game",
        observed_at=PUBLISHED_AT,
    )
    opportunity_evidence = OpportunityEvidence(
        schema_version=1,
        run_id=RUN_ID,
        opportunity_id=OPPORTUNITY_ID,
        observed_at=PUBLISHED_AT,
        trends=trends,
        autocomplete_queries=(suggestion,),
        related_queries=(),
        external_evidence=(external,),
        serp=serp,
    )
    health = SourceHealth(
        schema_version=1,
        run_id=RUN_ID,
        collector="steam",
        status="partial",
        observed_at=OBSERVED_AT,
        capabilities={"charts": True, "current_players": False},
        warnings=(warning,),
    )
    platform_heat = PlatformHeat(
        schema_version=1,
        run_id=RUN_ID,
        platform_key="steam:123456",
        surface="released",
        observation_ids=(OBSERVATION_ID,),
        heat=72.5,
    )
    normalized_heat = NormalizedHeat(
        schema_version=1,
        run_id=RUN_ID,
        platform_key="steam:123456",
        surface="released",
        observation_ids=(OBSERVATION_ID,),
        heat=72.5,
        platform_score=15.0,
    )
    score = ScoredOpportunity(
        schema_version=1,
        run_id=RUN_ID,
        opportunity_id=OPPORTUNITY_ID,
        demand_state="pass",
        platform_score=15.0,
        demand_score=24.0,
        external_score=8.0,
        seo_score=16.0,
        total_score=63.0,
        action="watch",
        warnings=(warning,),
    )
    artifact = RawArtifact(
        schema_version=1,
        run_id=RUN_ID,
        provider="steam_official",
        path="data/unified-game-radar/raw/provider.json",
        observed_at=OBSERVED_AT,
        sha256="a" * 64,
    )
    publication = Publication(
        schema_version=1,
        run_id=RUN_ID,
        phase="final",
        published_at=PUBLISHED_AT,
        report_json="reports/unified-game-radar/run.final.json",
        report_markdown="reports/unified-game-radar/run.final.md",
        daily_date=date(2026, 8, 31),
        advances_daily_latest=True,
    )
    task = OutstandingTask(
        schema_version=1,
        run_id=RUN_ID,
        collector="itch",
        surface="newest",
        action="collect_browser_observations",
        collection_contract={
            "required_fields": ["platform_id", "name"],
            "bounds": {"max_scrolls": 5},
        },
    )
    preliminary = PreliminaryResult(
        schema_version=1,
        run_id=RUN_ID,
        candidates=(identity,),
        source_health=(health,),
        warnings=(warning,),
        outstanding_tasks=(task,),
    )
    manifest = CommandManifest(
        schema_version=1,
        run_id=RUN_ID,
        phase="preliminary",
        report_json="reports/unified-game-radar/run.preliminary.json",
        report_markdown="reports/unified-game-radar/run.preliminary.md",
        source_health=(health,),
        warnings=(warning,),
        outstanding_tasks=(task,),
    )
    run = RadarRun(
        schema_version=1,
        run_id=RUN_ID,
        started_at=OBSERVED_AT,
        mode="scheduled",
        platforms=("itch", "steam", "roblox"),
        publish_daily=False,
    )
    return (
        run,
        platform_record,
        identity,
        observation,
        envelope,
        trend_point,
        trends,
        suggestion,
        external,
        serp,
        opportunity_evidence,
        health,
        platform_heat,
        normalized_heat,
        score,
        warning,
        artifact,
        publication,
        manifest,
        task,
        preliminary,
    )


EXPECTED_FIELD_ORDER = {
    RadarRun: (
        "schema_version",
        "run_id",
        "started_at",
        "mode",
        "platforms",
        "publish_daily",
    ),
    PlatformRecord: (
        "schema_version",
        "platform",
        "platform_id",
        "name",
        "developer",
        "official_domain",
        "url",
    ),
    GameIdentity: (
        "schema_version",
        "opportunity_id",
        "name",
        "normalized_name",
        "developer",
        "official_domain",
        "platform_records",
    ),
    PlatformObservation: (
        "schema_version",
        "observation_id",
        "run_id",
        "platform",
        "platform_id",
        "provider",
        "surface",
        "geo",
        "locale",
        "query_parameters",
        "metric_definition_version",
        "observed_at",
        "release_at",
        "source_rank",
        "raw_metrics",
        "evidence_urls",
    ),
    ObservationEnvelope: (
        "schema_version",
        "run_id",
        "collector",
        "surface",
        "geo",
        "locale",
        "metric_definition_version",
        "observations",
    ),
    TrendPoint: ("date", "value", "complete"),
    TrendEvidence: (
        "query",
        "query_type",
        "timeframe",
        "geo",
        "category",
        "property",
        "timezone",
        "points",
        "comparison_term",
        "comparison_average",
        "evidence_url",
        "raw_artifact",
        "observed_at",
    ),
    SearchQueryEvidence: (
        "schema_version",
        "query",
        "observed_at",
        "source_url",
    ),
    ExternalEvidence: (
        "source",
        "url",
        "published_at",
        "observed_at",
        "author_relation",
        "engagement_count",
        "evidence_kind",
    ),
    SerpEvidence: (
        "query",
        "relevant_nonofficial_results",
        "guide_results",
        "missing_intents",
        "evidence_url",
        "observed_at",
    ),
    OpportunityEvidence: (
        "schema_version",
        "run_id",
        "opportunity_id",
        "observed_at",
        "trends",
        "autocomplete_queries",
        "related_queries",
        "external_evidence",
        "serp",
    ),
    SourceHealth: (
        "schema_version",
        "run_id",
        "collector",
        "status",
        "observed_at",
        "capabilities",
        "warnings",
    ),
    PlatformHeat: (
        "schema_version",
        "run_id",
        "platform_key",
        "surface",
        "observation_ids",
        "heat",
    ),
    NormalizedHeat: (
        "schema_version",
        "run_id",
        "platform_key",
        "surface",
        "observation_ids",
        "heat",
        "platform_score",
    ),
    ScoredOpportunity: (
        "schema_version",
        "run_id",
        "opportunity_id",
        "demand_state",
        "platform_score",
        "demand_score",
        "external_score",
        "seo_score",
        "total_score",
        "action",
        "warnings",
    ),
    WarningRecord: (
        "schema_version",
        "code",
        "message",
        "collector",
        "opportunity_id",
    ),
    RawArtifact: (
        "schema_version",
        "run_id",
        "provider",
        "path",
        "observed_at",
        "sha256",
    ),
    Publication: (
        "schema_version",
        "run_id",
        "phase",
        "published_at",
        "report_json",
        "report_markdown",
        "daily_date",
        "advances_daily_latest",
    ),
    CommandManifest: (
        "schema_version",
        "run_id",
        "phase",
        "report_json",
        "report_markdown",
        "source_health",
        "warnings",
        "outstanding_tasks",
    ),
    OutstandingTask: (
        "schema_version",
        "run_id",
        "collector",
        "surface",
        "action",
        "collection_contract",
    ),
    PreliminaryResult: (
        "schema_version",
        "run_id",
        "candidates",
        "source_health",
        "warnings",
        "outstanding_tasks",
    ),
}


class SchemaRoundTripTests(unittest.TestCase):
    def test_every_schema_has_exact_field_order_and_json_round_trip(self) -> None:
        instances = build_instances()
        self.assertEqual({type(item) for item in instances}, set(EXPECTED_FIELD_ORDER))

        for instance in instances:
            schema_type = type(instance)
            with self.subTest(schema=schema_type.__name__):
                expected_order = EXPECTED_FIELD_ORDER[schema_type]
                self.assertEqual(
                    tuple(field.name for field in fields(instance)),
                    expected_order,
                )
                encoded = instance.to_dict()
                self.assertEqual(tuple(encoded), expected_order)
                wire_value = json.loads(json.dumps(encoded, allow_nan=False))
                self.assertEqual(schema_type.from_dict(wire_value), instance)
                self.assertEqual(
                    schema_type.from_dict(wire_value).to_dict(),
                    encoded,
                )

    def test_every_schema_is_frozen(self) -> None:
        for instance in build_instances():
            with self.subTest(schema=type(instance).__name__):
                first_field = fields(instance)[0].name
                with self.assertRaises(FrozenInstanceError):
                    setattr(instance, first_field, object())

    def test_mappings_and_nested_json_sequences_are_deeply_immutable(self) -> None:
        by_type = {type(instance): instance for instance in build_instances()}
        observation = by_type[PlatformObservation]
        envelope = by_type[ObservationEnvelope]
        health = by_type[SourceHealth]
        task = by_type[OutstandingTask]

        with self.assertRaises(TypeError):
            observation.raw_metrics["review_count"] = 0
        with self.assertRaises(TypeError):
            observation.raw_metrics["provenance"]["surfaces"] = ()
        with self.assertRaises(AttributeError):
            observation.raw_metrics["provenance"]["surfaces"].append("new")
        self.assertIsInstance(envelope.observations[0], PlatformObservation)
        with self.assertRaises(FrozenInstanceError):
            envelope.observations[0].source_rank = 2
        with self.assertRaises(TypeError):
            health.capabilities["charts"] = False
        with self.assertRaises(TypeError):
            task.collection_contract["bounds"]["max_scrolls"] = 10

    def test_unknown_and_missing_keys_are_rejected_at_every_boundary(self) -> None:
        for instance in build_instances():
            schema_type = type(instance)
            payload = instance.to_dict()
            with self.subTest(schema=schema_type.__name__, case="unexpected"):
                with self.assertRaisesRegex(InputValidationError, "unexpected"):
                    schema_type.from_dict({**payload, "unexpected": True})
            with self.subTest(schema=schema_type.__name__, case="missing"):
                payload_without_first = dict(payload)
                payload_without_first.pop(next(iter(payload_without_first)))
                with self.assertRaisesRegex(InputValidationError, "missing"):
                    schema_type.from_dict(payload_without_first)


class SchemaValidationTests(unittest.TestCase):
    def test_versioned_records_accept_only_integer_schema_version_one(self) -> None:
        unversioned = {TrendPoint, TrendEvidence, ExternalEvidence, SerpEvidence}
        for instance in build_instances():
            schema_type = type(instance)
            if schema_type in unversioned:
                continue
            for invalid in (2, "1", True):
                with self.subTest(schema=schema_type.__name__, invalid=invalid):
                    payload = instance.to_dict()
                    payload["schema_version"] = invalid
                    with self.assertRaises(InputValidationError):
                        schema_type.from_dict(payload)

    def test_all_per_run_records_reject_unsafe_or_empty_run_ids(self) -> None:
        per_run_types = {
            RadarRun,
            PlatformObservation,
            ObservationEnvelope,
            OpportunityEvidence,
            SourceHealth,
            PlatformHeat,
            NormalizedHeat,
            ScoredOpportunity,
            RawArtifact,
            Publication,
            CommandManifest,
            OutstandingTask,
            PreliminaryResult,
        }
        for instance in build_instances():
            schema_type = type(instance)
            if schema_type not in per_run_types:
                continue
            for invalid in (
                "",
                "x",
                "20260230T020000Z-a1b2c3d4",
                "20260831T020000Z-abc",
                "20260831T020000Z-ABCDEF12",
                "20260831T020000Z-abcdefg!",
                "20260831T020000Z-" + "a" * 33,
                "20260831T020000Z-a1b2c3d4/path",
                "20260831T020000Z-a1b2c3d4\n",
            ):
                with self.subTest(schema=schema_type.__name__, invalid=invalid):
                    payload = instance.to_dict()
                    payload["run_id"] = invalid
                    with self.assertRaises(InputValidationError):
                        schema_type.from_dict(payload)

    def test_structured_run_ids_accept_valid_utc_timestamp_and_suffix_bounds(self) -> None:
        run = next(
            instance for instance in build_instances() if isinstance(instance, RadarRun)
        )
        for valid in (
            "20240229T235959Z-01234567",
            "20260831T020000Z-" + "abcdef01" * 4,
        ):
            with self.subTest(valid=valid):
                payload = run.to_dict()
                payload["run_id"] = valid
                parsed = RadarRun.from_dict(payload)
                self.assertEqual(parsed.run_id, valid)
                self.assertEqual(parsed.to_dict()["run_id"], valid)

    def test_observation_ids_are_structured_and_validated_in_all_references(self) -> None:
        by_type = {type(instance): instance for instance in build_instances()}
        valid_observations = (
            (
                "itch:studio.example-game:newest:20240229T235959Z",
                "itch",
                "studio.example-game",
                "newest",
                "2024-02-29T23:59:59Z",
            ),
            (
                "steam:123456:most_played:20260831T020000Z",
                "steam",
                "123456",
                "most_played",
                "2026-08-31T02:00:00Z",
            ),
            (
                "roblox:123:up-and-coming:20260831T020000Z",
                "roblox",
                "123",
                "up-and-coming",
                "2026-08-31T02:00:00Z",
            ),
        )
        for valid, platform, platform_id, surface, observed_at in valid_observations:
            with self.subTest(valid=valid):
                payload = by_type[PlatformObservation].to_dict()
                payload["observation_id"] = valid
                payload["platform"] = platform
                payload["platform_id"] = platform_id
                payload["surface"] = surface
                payload["observed_at"] = observed_at
                parsed = PlatformObservation.from_dict(payload)
                self.assertEqual(parsed.observation_id, valid)
                self.assertEqual(parsed.to_dict()["observation_id"], valid)

        invalid_ids = (
            "x",
            "epic:123:most_played:20260831T020000Z",
            "steam:-123:most_played:20260831T020000Z",
            "steam:123-:most_played:20260831T020000Z",
            "steam:123..456:most_played:20260831T020000Z",
            "steam:../123:most_played:20260831T020000Z",
            "steam:123:MostPlayed:20260831T020000Z",
            "steam:123:most.played:20260831T020000Z",
            "steam:123:most__played:20260831T020000Z",
            "steam:123:most_played:20260230T020000Z",
            "steam:123:most_played:20260831T020000",
            "steam:123:most_played:20260831T020000Z\n",
        )
        for schema_type in (PlatformObservation, PlatformHeat, NormalizedHeat):
            for invalid in invalid_ids:
                with self.subTest(schema=schema_type.__name__, invalid=invalid):
                    payload = by_type[schema_type].to_dict()
                    field_name = (
                        "observation_id"
                        if schema_type is PlatformObservation
                        else "observation_ids"
                    )
                    payload[field_name] = (
                        invalid if field_name == "observation_id" else [invalid]
                    )
                    with self.assertRaises(InputValidationError):
                        schema_type.from_dict(payload)

    def test_observation_id_provenance_must_match_observation_fields(self) -> None:
        observation = next(
            instance
            for instance in build_instances()
            if isinstance(instance, PlatformObservation)
        )
        mismatches = (
            ("observation_id", "itch:123456:most_played:20260831T020000Z"),
            ("observation_id", "steam:999999:most_played:20260831T020000Z"),
            ("observation_id", "steam:123456:newest:20260831T020000Z"),
            ("observation_id", "steam:123456:most_played:20260831T020001Z"),
            ("observed_at", "2026-08-31T02:00:00.100Z"),
        )
        for field_name, value in mismatches:
            with self.subTest(field=field_name, value=value):
                payload = observation.to_dict()
                payload[field_name] = value
                with self.assertRaises(InputValidationError):
                    PlatformObservation.from_dict(payload)

    def test_platform_records_and_observations_enforce_shared_id_boundaries(self) -> None:
        by_type = {type(instance): instance for instance in build_instances()}
        record_payload = by_type[PlatformRecord].to_dict()
        observation_payload = by_type[PlatformObservation].to_dict()

        for schema_type, template in (
            (PlatformRecord, record_payload),
            (PlatformObservation, observation_payload),
        ):
            accepted = dict(template)
            accepted["platform_id"] = str(2**53 - 1)
            if schema_type is PlatformObservation:
                accepted["observation_id"] = (
                    f"steam:{2**53 - 1}:most_played:20260831T020000Z"
                )
            self.assertEqual(
                schema_type.from_dict(accepted).platform_id,
                str(2**53 - 1),
            )

            for invalid in (str(2**53), str(10**400)):
                with self.subTest(schema=schema_type.__name__, invalid=invalid):
                    changed = dict(template)
                    changed["platform_id"] = invalid
                    if schema_type is PlatformObservation:
                        changed["observation_id"] = (
                            f"steam:{invalid}:most_played:20260831T020000Z"
                        )
                    with self.assertRaises(InputValidationError):
                        schema_type.from_dict(changed)

        for invalid in (str(2**53), str(10**400)):
            changed_observation = dict(observation_payload)
            changed_observation["observation_id"] = (
                f"steam:{invalid}:most_played:20260831T020000Z"
            )
            with self.subTest(boundary="observation_id", invalid=invalid):
                with self.assertRaises(InputValidationError):
                    PlatformObservation.from_dict(changed_observation)

        for schema_type in (PlatformHeat, NormalizedHeat):
            template = by_type[schema_type].to_dict()
            accepted = dict(template)
            accepted["platform_key"] = f"steam:{2**53 - 1}"
            accepted["observation_ids"] = [
                f"steam:{2**53 - 1}:most_played:20260831T020000Z"
            ]
            self.assertEqual(
                schema_type.from_dict(accepted).platform_key,
                f"steam:{2**53 - 1}",
            )

            for invalid in (str(2**53), str(10**400)):
                changed = dict(template)
                changed["platform_key"] = f"steam:{invalid}"
                changed["observation_ids"] = [
                    f"steam:{invalid}:most_played:20260831T020000Z"
                ]
                with self.subTest(
                    schema=schema_type.__name__, boundary="heat", invalid=invalid
                ):
                    with self.assertRaises(InputValidationError):
                        schema_type.from_dict(changed)

    def test_heat_references_match_platform_key_and_allow_multi_surface_history(self) -> None:
        by_type = {type(instance): instance for instance in build_instances()}
        historical_ids = [
            "steam:123456:most_played:20260830T020000Z",
            "steam:123456:top_seller:20260830T100000Z",
            "steam:123456:new_release:20260831T020000Z",
        ]
        for schema_type in (PlatformHeat, NormalizedHeat):
            payload = by_type[schema_type].to_dict()
            payload["observation_ids"] = historical_ids
            parsed = schema_type.from_dict(payload)
            self.assertEqual(parsed.observation_ids, tuple(historical_ids))
            self.assertEqual(parsed.surface, "released")

            for invalid in (
                "itch:123456:most_played:20260830T020000Z",
                "steam:999999:most_played:20260830T020000Z",
            ):
                with self.subTest(schema=schema_type.__name__, invalid=invalid):
                    changed = by_type[schema_type].to_dict()
                    changed["observation_ids"] = [invalid]
                    with self.assertRaises(InputValidationError):
                        schema_type.from_dict(changed)

    def test_observation_envelope_contains_only_typed_observations(self) -> None:
        envelope = next(
            instance
            for instance in build_instances()
            if isinstance(instance, ObservationEnvelope)
        )
        wire_value = envelope.to_dict()
        self.assertIsInstance(wire_value["observations"][0], dict)
        parsed = ObservationEnvelope.from_dict(wire_value)
        self.assertIsInstance(parsed.observations[0], PlatformObservation)
        self.assertEqual(parsed, envelope)

        wire_value["observations"] = [
            {
                "platform_id": "untyped",
                "name": "Loose payload",
            }
        ]
        with self.assertRaises(InputValidationError):
            ObservationEnvelope.from_dict(wire_value)

    def test_opportunity_ids_are_canonical_uuids(self) -> None:
        self.assertEqual(str(UUID(OPPORTUNITY_ID)), OPPORTUNITY_ID)
        opportunity_types = (
            GameIdentity,
            OpportunityEvidence,
            ScoredOpportunity,
        )
        by_type = {type(instance): instance for instance in build_instances()}
        for schema_type in opportunity_types:
            for invalid in ("not-a-uuid", OPPORTUNITY_ID.upper(), ""):
                with self.subTest(schema=schema_type.__name__, invalid=invalid):
                    payload = by_type[schema_type].to_dict()
                    payload["opportunity_id"] = invalid
                    with self.assertRaises(InputValidationError):
                        schema_type.from_dict(payload)

        warning = by_type[WarningRecord]
        payload = warning.to_dict()
        payload["opportunity_id"] = "not-a-uuid"
        with self.assertRaises(InputValidationError):
            WarningRecord.from_dict(payload)

    def test_utc_timestamps_parse_and_serialize_canonically(self) -> None:
        run = RadarRun.from_dict(
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "started_at": "2026-08-31T02:00:00.123000Z",
                "mode": "manual",
                "platforms": ["steam"],
                "publish_daily": False,
            }
        )
        self.assertEqual(run.started_at.tzinfo, timezone.utc)
        self.assertEqual(run.to_dict()["started_at"], "2026-08-31T02:00:00.123Z")

        for invalid in (
            "2026-08-31T02:00:00",
            "2026-08-31T10:00:00+08:00",
            "2026-08-31 02:00:00Z",
            "not-a-time",
        ):
            with self.subTest(invalid=invalid):
                payload = run.to_dict()
                payload["started_at"] = invalid
                with self.assertRaises(InputValidationError):
                    RadarRun.from_dict(payload)

        with self.assertRaises(InputValidationError):
            RadarRun(
                1,
                RUN_ID,
                datetime(2026, 8, 31, 2),
                "manual",
                ("steam",),
                False,
            )

    def test_dates_are_strict_iso_dates(self) -> None:
        for invalid in ("2026-8-1", "2026-02-30", "2026-08-31T00:00:00Z"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(InputValidationError):
                    TrendPoint.from_dict(
                        {"date": invalid, "value": 1, "complete": True}
                    )

    def test_https_urls_are_required(self) -> None:
        by_type = {type(instance): instance for instance in build_instances()}
        cases = (
            (PlatformRecord, "url"),
            (TrendEvidence, "evidence_url"),
            (SearchQueryEvidence, "source_url"),
            (ExternalEvidence, "url"),
            (SerpEvidence, "evidence_url"),
        )
        for schema_type, field_name in cases:
            for invalid in ("http://example.com", "javascript:alert(1)", ""):
                with self.subTest(
                    schema=schema_type.__name__, field=field_name, invalid=invalid
                ):
                    payload = by_type[schema_type].to_dict()
                    payload[field_name] = invalid
                    with self.assertRaises(InputValidationError):
                        schema_type.from_dict(payload)

        observation = by_type[PlatformObservation]
        payload = observation.to_dict()
        payload["evidence_urls"] = ["http://example.com/evidence"]
        with self.assertRaises(InputValidationError):
            PlatformObservation.from_dict(payload)

    def test_malformed_urls_and_timezones_never_leak_value_error(self) -> None:
        by_type = {type(instance): instance for instance in build_instances()}
        record_payload = by_type[PlatformRecord].to_dict()
        for invalid in (
            "https://[::1",
            "https://example.com:bad/path",
            "https://example.com:/empty-port",
            "https:///missing-host",
            "https://:443/path",
        ):
            with self.subTest(url=invalid):
                record_payload["url"] = invalid
                with self.assertRaises(InputValidationError):
                    PlatformRecord.from_dict(record_payload)

        trends_payload = by_type[TrendEvidence].to_dict()
        for invalid in ("/etc/passwd", "../UTC", "Mars/Olympus", ""):
            with self.subTest(timezone=invalid):
                trends_payload["timezone"] = invalid
                with self.assertRaises(InputValidationError):
                    TrendEvidence.from_dict(trends_payload)

    def test_optional_unavailable_values_remain_null_not_zero(self) -> None:
        unavailable_point = TrendPoint(date(2026, 8, 31), None, False)
        self.assertEqual(unavailable_point.to_dict()["value"], None)
        self.assertEqual(
            TrendPoint.from_dict(unavailable_point.to_dict()).value,
            None,
        )

        by_type = {type(instance): instance for instance in build_instances()}
        observation = by_type[PlatformObservation]
        self.assertIsNone(observation.to_dict()["raw_metrics"]["current_players"])
        self.assertIsNone(by_type[TrendEvidence].to_dict()["comparison_average"])
        self.assertIsNone(by_type[ExternalEvidence].to_dict()["engagement_count"])
        self.assertIsNone(by_type[SerpEvidence].to_dict()["relevant_nonofficial_results"])

        evidence_payload = by_type[OpportunityEvidence].to_dict()
        evidence_payload["trends"] = None
        evidence_payload["serp"] = None
        evidence_payload["autocomplete_queries"] = []
        evidence_payload["related_queries"] = []
        evidence_payload["external_evidence"] = []
        unavailable = OpportunityEvidence.from_dict(evidence_payload)
        self.assertIsNone(unavailable.trends)
        self.assertIsNone(unavailable.serp)
        self.assertEqual(unavailable.autocomplete_queries, ())
        self.assertEqual(unavailable.related_queries, ())
        self.assertEqual(unavailable.external_evidence, ())

    def test_allowed_literal_sets_are_narrow(self) -> None:
        by_type = {type(instance): instance for instance in build_instances()}
        cases = (
            (RadarRun, "mode", "adhoc"),
            (PlatformRecord, "platform", "epic"),
            (ObservationEnvelope, "collector", "youtube"),
            (TrendEvidence, "query_type", "topic"),
            (TrendEvidence, "property", "youtube"),
            (ExternalEvidence, "author_relation", "publisher"),
            (SourceHealth, "status", "failed"),
            (ScoredOpportunity, "demand_state", "single_spike"),
            (ScoredOpportunity, "action", "worth_doing"),
            (Publication, "phase", "scan"),
            (CommandManifest, "phase", "enrich"),
            (OutstandingTask, "action", "browse_everything"),
        )
        for schema_type, field_name, invalid in cases:
            with self.subTest(schema=schema_type.__name__, field=field_name):
                payload = by_type[schema_type].to_dict()
                payload[field_name] = invalid
                with self.assertRaises(InputValidationError):
                    schema_type.from_dict(payload)

        for valid in ("scheduled", "manual"):
            payload = by_type[RadarRun].to_dict()
            payload["mode"] = valid
            self.assertEqual(RadarRun.from_dict(payload).mode, valid)
        for valid in ("preliminary", "final"):
            payload = by_type[Publication].to_dict()
            payload["phase"] = valid
            self.assertEqual(Publication.from_dict(payload).phase, valid)

    def test_external_evidence_kind_is_extensible_but_strict_text(self) -> None:
        external = next(
            instance
            for instance in build_instances()
            if isinstance(instance, ExternalEvidence)
        )
        for valid in ("gameplay_video", "forum_post", "future-kind"):
            payload = external.to_dict()
            payload["evidence_kind"] = valid
            self.assertEqual(
                ExternalEvidence.from_dict(payload).evidence_kind,
                valid,
            )
        for invalid in ("", " leading", "trailing ", 7):
            payload = external.to_dict()
            payload["evidence_kind"] = invalid
            with self.assertRaises(InputValidationError):
                ExternalEvidence.from_dict(payload)

    def test_numbers_are_finite_not_booleans_and_respect_score_bounds(self) -> None:
        by_type = {type(instance): instance for instance in build_instances()}
        bounded_cases = (
            (TrendPoint, "value", 100.1),
            (PlatformHeat, "heat", 100.1),
            (NormalizedHeat, "platform_score", 30.1),
            (ScoredOpportunity, "platform_score", 30.1),
            (ScoredOpportunity, "demand_score", 30.1),
            (ScoredOpportunity, "external_score", 20.1),
            (ScoredOpportunity, "seo_score", 20.1),
            (ScoredOpportunity, "total_score", 100.1),
        )
        for schema_type, field_name, invalid in bounded_cases:
            with self.subTest(schema=schema_type.__name__, field=field_name):
                payload = by_type[schema_type].to_dict()
                payload[field_name] = invalid
                with self.assertRaises(InputValidationError):
                    schema_type.from_dict(payload)

        for invalid in (float("nan"), float("inf"), float("-inf"), True):
            payload = by_type[PlatformHeat].to_dict()
            payload["heat"] = invalid
            with self.assertRaises(InputValidationError):
                PlatformHeat.from_dict(payload)

        payload = by_type[PlatformObservation].to_dict()
        payload["raw_metrics"] = {"bad": float("nan")}
        with self.assertRaises(InputValidationError):
            PlatformObservation.from_dict(payload)

    def test_counts_versions_ranks_and_booleans_are_strict(self) -> None:
        by_type = {type(instance): instance for instance in build_instances()}
        integer_cases = (
            (PlatformObservation, "metric_definition_version", 0),
            (PlatformObservation, "source_rank", 0),
            (ObservationEnvelope, "metric_definition_version", True),
            (TrendEvidence, "category", -1),
            (ExternalEvidence, "engagement_count", -1),
            (SerpEvidence, "guide_results", -1),
        )
        for schema_type, field_name, invalid in integer_cases:
            with self.subTest(schema=schema_type.__name__, field=field_name):
                payload = by_type[schema_type].to_dict()
                payload[field_name] = invalid
                with self.assertRaises(InputValidationError):
                    schema_type.from_dict(payload)

        run_payload = by_type[RadarRun].to_dict()
        run_payload["publish_daily"] = 1
        with self.assertRaises(InputValidationError):
            RadarRun.from_dict(run_payload)

    def test_integer_fields_reject_values_above_json_safe_range(self) -> None:
        by_type = {type(instance): instance for instance in build_instances()}
        cases = (
            (PlatformObservation, "metric_definition_version"),
            (PlatformObservation, "source_rank"),
            (ObservationEnvelope, "metric_definition_version"),
            (TrendEvidence, "category"),
            (ExternalEvidence, "engagement_count"),
            (SerpEvidence, "relevant_nonofficial_results"),
            (SerpEvidence, "guide_results"),
        )
        for schema_type, field_name in cases:
            for invalid in (2**53, 10**400):
                with self.subTest(
                    schema=schema_type.__name__, field=field_name, invalid=invalid
                ):
                    payload = by_type[schema_type].to_dict()
                    payload[field_name] = invalid
                    with self.assertRaises(InputValidationError):
                        schema_type.from_dict(payload)

    def test_domains_platforms_intents_and_hashes_are_validated(self) -> None:
        by_type = {type(instance): instance for instance in build_instances()}

        identity_payload = by_type[GameIdentity].to_dict()
        identity_payload["official_domain"] = "https://example.com/path"
        with self.assertRaises(InputValidationError):
            GameIdentity.from_dict(identity_payload)

        run_payload = by_type[RadarRun].to_dict()
        for invalid in ([], ["steam", "steam"], ["steam", "epic"]):
            with self.subTest(platforms=invalid):
                run_payload["platforms"] = invalid
                with self.assertRaises(InputValidationError):
                    RadarRun.from_dict(run_payload)

        serp_payload = by_type[SerpEvidence].to_dict()
        for invalid in (["guide", "guide"], ["reviews"]):
            with self.subTest(intents=invalid):
                serp_payload["missing_intents"] = invalid
                with self.assertRaises(InputValidationError):
                    SerpEvidence.from_dict(serp_payload)

        artifact_payload = by_type[RawArtifact].to_dict()
        for invalid in ("A" * 64, "a" * 63, "z" * 64):
            with self.subTest(sha256=invalid):
                artifact_payload["sha256"] = invalid
                with self.assertRaises(InputValidationError):
                    RawArtifact.from_dict(artifact_payload)

    def test_collection_maps_accept_only_json_safe_string_keyed_values(self) -> None:
        observation = next(
            instance
            for instance in build_instances()
            if isinstance(instance, PlatformObservation)
        )
        payload = observation.to_dict()
        for invalid in (
            {1: "not-string-key"},
            {"bad": object()},
            {"bad": {"nested": float("nan")}},
        ):
            with self.subTest(invalid=invalid):
                payload["query_parameters"] = invalid
                with self.assertRaises(InputValidationError):
                    PlatformObservation.from_dict(payload)

    def test_collection_maps_enforce_json_safe_numeric_boundaries(self) -> None:
        maximum = 2**53 - 1
        by_type = {type(instance): instance for instance in build_instances()}

        observation_payload = by_type[PlatformObservation].to_dict()
        observation_payload["raw_metrics"] = {
            "maximum": maximum,
            "minimum": -maximum,
            "nested": {"exact": maximum},
        }
        observation = PlatformObservation.from_dict(observation_payload)
        self.assertEqual(observation.to_dict()["raw_metrics"], observation_payload["raw_metrics"])

        map_cases = (
            (PlatformObservation, "raw_metrics"),
            (PlatformObservation, "query_parameters"),
            (OutstandingTask, "collection_contract"),
        )
        for schema_type, field_name in map_cases:
            for invalid in (maximum + 1, -maximum - 1, 10**400, 1e20):
                with self.subTest(
                    schema=schema_type.__name__, field=field_name, invalid=invalid
                ):
                    payload = by_type[schema_type].to_dict()
                    payload[field_name] = {"nested": {"value": invalid}}
                    with self.assertRaises(InputValidationError):
                        schema_type.from_dict(payload)

    def test_parent_records_reject_nested_records_from_other_runs(self) -> None:
        by_type = {type(instance): instance for instance in build_instances()}
        other_run = "20260831T030000Z-deadbeef"
        for parent_type in (CommandManifest, PreliminaryResult):
            for child_field in ("source_health", "outstanding_tasks"):
                with self.subTest(
                    parent=parent_type.__name__, child_field=child_field
                ):
                    payload = by_type[parent_type].to_dict()
                    child = dict(payload[child_field][0])
                    child["run_id"] = other_run
                    payload[child_field] = [child]
                    with self.assertRaises(InputValidationError):
                        parent_type.from_dict(payload)

    def test_scored_opportunity_requires_one_decimal_and_exact_total(self) -> None:
        score = next(
            instance
            for instance in build_instances()
            if isinstance(instance, ScoredOpportunity)
        )
        for field_name in (
            "platform_score",
            "demand_score",
            "external_score",
            "seo_score",
            "total_score",
        ):
            with self.subTest(field=field_name):
                payload = score.to_dict()
                payload[field_name] = 1.23
                with self.assertRaises(InputValidationError):
                    ScoredOpportunity.from_dict(payload)

        payload = score.to_dict()
        payload["total_score"] = 62.9
        with self.assertRaises(InputValidationError):
            ScoredOpportunity.from_dict(payload)

        payload.update(
            platform_score=0.1,
            demand_score=0.2,
            external_score=0.3,
            seo_score=0.4,
            total_score=1.0,
        )
        floating_sum = ScoredOpportunity.from_dict(payload)
        self.assertEqual(floating_sum.total_score, 1.0)

        payload.update(
            platform_score=30.0,
            demand_score=30.0,
            external_score=20.0,
            seo_score=20.0,
            total_score=100.0,
        )
        self.assertEqual(ScoredOpportunity.from_dict(payload).total_score, 100.0)

    def test_external_evidence_cannot_be_published_in_the_future(self) -> None:
        external = next(
            instance
            for instance in build_instances()
            if isinstance(instance, ExternalEvidence)
        )
        payload = external.to_dict()
        payload["published_at"] = payload["observed_at"]
        self.assertEqual(
            ExternalEvidence.from_dict(payload).published_at,
            ExternalEvidence.from_dict(payload).observed_at,
        )

        payload["published_at"] = "2026-08-31T06:00:01Z"
        with self.assertRaises(InputValidationError):
            ExternalEvidence.from_dict(payload)

    def test_sequence_boundaries_accept_only_lists_or_tuples(self) -> None:
        by_type = {type(instance): instance for instance in build_instances()}
        cases = (
            (RadarRun, "platforms", ("steam",)),
            (SerpEvidence, "missing_intents", ("guide",)),
            (PlatformHeat, "observation_ids", (OBSERVATION_ID,)),
        )
        for schema_type, field_name, valid in cases:
            for accepted in (list(valid), tuple(valid)):
                payload = by_type[schema_type].to_dict()
                payload[field_name] = accepted
                parsed = schema_type.from_dict(payload)
                self.assertIsInstance(getattr(parsed, field_name), tuple)

            for rejected in (set(valid), (item for item in valid), "steam"):
                with self.subTest(
                    schema=schema_type.__name__,
                    field=field_name,
                    rejected_type=type(rejected).__name__,
                ):
                    payload = by_type[schema_type].to_dict()
                    payload[field_name] = rejected
                    with self.assertRaises(InputValidationError):
                        schema_type.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
