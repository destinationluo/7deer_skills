"""Contract tests for the Steam game radar Skill and reference documents."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "steam-game-radar"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from steam_game_radar.config import RadarConfig
from steam_game_radar.enrichment import load_enrichment
from steam_game_radar.http_client import ALLOWED_HOSTS, USER_AGENT
from steam_game_radar.official_provider import (
    APPDETAILS_URL,
    CURRENT_PLAYERS_URL,
    FEATURED_CATEGORIES_URL,
    MOST_PLAYED_URL,
    _CAPABILITY_NAMES,
)
from steam_game_radar.report import _CANDIDATE_FIELDS, _REPORT_FIELDS
from steam_game_radar.schemas import GameRecord, MetricObservation
from steam_game_radar.score import (
    _RELEASED_WEIGHTS,
    _UNRELEASED_WEIGHTS,
    _action_for_combined_score,
    _combine_scores,
    score_unreleased,
)
from steam_game_radar.steamdb_import import _ALIASES, _ALLOWED_VIEWS
from steam_game_radar.trend import AnalyzedCandidate


SCAN_COMMAND = """python3 steam-game-radar/scripts/steam_radar.py scan \\
  --config steam-game-radar/references/config.example.json"""
IMPORT_COMMAND = """python3 steam-game-radar/scripts/steam_radar.py import-steamdb \\
  --config steam-game-radar/references/config.example.json \\
  --view wishlist_activity \\
  --input /path/to/steamdb-export.csv"""
ENRICH_COMMAND = """python3 steam-game-radar/scripts/steam_radar.py enrich \\
  --config steam-game-radar/references/config.example.json \\
  --run-id 20260824T030000Z-a1b2c3d4 \\
  --input /path/to/enrichment.json"""
INSTALLED_SCAN_COMMAND = """python3 .agent/skills/steam-game-radar/scripts/steam_radar.py scan \\
  --config .agent/skills/steam-game-radar/references/config.example.json"""
INSTALLED_IMPORT_COMMAND = """python3 .agent/skills/steam-game-radar/scripts/steam_radar.py import-steamdb \\
  --config .agent/skills/steam-game-radar/references/config.example.json \\
  --view wishlist_activity \\
  --input /path/to/steamdb-export.csv"""
INSTALLED_ENRICH_COMMAND = """python3 .agent/skills/steam-game-radar/scripts/steam_radar.py enrich \\
  --config .agent/skills/steam-game-radar/references/config.example.json \\
  --run-id 20260824T030000Z-a1b2c3d4 \\
  --input /path/to/enrichment.json"""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ""


class SkillDocumentationTests(unittest.TestCase):
    def test_skill_frontmatter_and_progressive_routing(self) -> None:
        text = _read(SKILL_ROOT / "SKILL.md")
        match = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.DOTALL)
        self.assertIsNotNone(match, "SKILL.md requires YAML frontmatter")
        frontmatter = match.group(1) if match else ""
        fields = {}
        for line in frontmatter.splitlines():
            key, separator, value = line.partition(":")
            self.assertEqual(separator, ":")
            fields[key.strip()] = value.strip()
        self.assertEqual(set(fields), {"name", "description"})
        self.assertEqual(fields.get("name"), "steam-game-radar")
        description = fields.get("description", "")
        self.assertTrue(description.startswith("Use when "))
        self.assertLessEqual(len(frontmatter), 1024)
        for workflow_word in ("runs scan", "then enrich", "workflow", "three steps"):
            self.assertNotIn(workflow_word, description.casefold())
        body = text[match.end() :] if match else ""
        self.assertLess(len(body.split()), 700)
        for reference in (
            "references/data-sources.md",
            "references/steamdb-import-format.md",
            "references/scoring-rules.md",
            "references/report-template.md",
        ):
            self.assertIn(reference, body)

    def test_manual_trigger_routes_are_exact(self) -> None:
        text = _read(SKILL_ROOT / "SKILL.md")
        self.assertIn("`\u8dd1 Steam \u96f7\u8fbe`", text)
        self.assertIn("`\u5bfc\u5165 SteamDB \u699c\u5355\u5e76\u8dd1 Steam \u96f7\u8fbe`", text)
        scan_index = text.find("`\u8dd1 Steam \u96f7\u8fbe`")
        import_index = text.find("`\u5bfc\u5165 SteamDB \u699c\u5355\u5e76\u8dd1 Steam \u96f7\u8fbe`")
        self.assertGreater(text.find(SCAN_COMMAND, scan_index), scan_index)
        self.assertGreater(text.find(IMPORT_COMMAND, import_index), import_index)
        import_route = text[import_index : text.find("##", import_index + 3)]
        self.assertRegex(import_route, r"\u672c\u5730(?: CSV/JSON| CSV \u6216 JSON).*(?:view|\u89c6\u56fe)")
        for token in ("Top 10", "Google", "YouTube", "Reddit"):
            self.assertIn(token, import_route)
        path_section = text[text.find("## Path modes") : text.find("## Manual routes")]
        self.assertIn("Repository checkout", path_section)
        self.assertIn("Installed in the target project", path_section)
        self.assertIn("target project root", path_section)
        self.assertIn(
            "Relative `data_dir` and `report_dir` values resolve from that target project root",
            " ".join(path_section.split()),
        )
        for command in (
            INSTALLED_SCAN_COMMAND,
            INSTALLED_IMPORT_COMMAND,
            INSTALLED_ENRICH_COMMAND,
        ):
            self.assertIn(command, path_section)

    def test_exact_cli_commands_and_outputs(self) -> None:
        text = _read(SKILL_ROOT / "SKILL.md")
        for command in (SCAN_COMMAND, IMPORT_COMMAND, ENRICH_COMMAND):
            self.assertIn(command, text)
            self.assertEqual(text.count(command), 1)
        self.assertIn("reports/steam-game-radar/{run_id}.preliminary.json", text)
        self.assertIn("reports/steam-game-radar/{run_id}.final.json", text)
        self.assertIn("references/report-template.md", text)
        exit_section = text[text.find("## Exit codes") : text.find("## References")]
        exit_rows = {
            int(code): meaning.strip()
            for code, meaning in re.findall(
                r"(?m)^\|\s*([0-6])\s*\|\s*([^|]+?)\s*\|$",
                exit_section,
            )
        }
        self.assertEqual(
            exit_rows,
            {
                0: "Success, including stale fallback no older than 72 hours",
                1: "Unexpected failure; traceback is emitted",
                2: "Input or schema validation failure",
                3: "Provider failure with no usable fallback",
                4: "Configuration failure",
                5: "Snapshot or report persistence failure",
                6: "Another run holds the project lock",
            },
        )

    def test_agent_schedule_and_preliminary_only_cron(self) -> None:
        text = _read(SKILL_ROOT / "SKILL.md")
        schedule = text[text.find("## Schedules") : text.find("## Policy and outputs")]
        schedule_flat = " ".join(schedule.split())
        self.assertIn("scheduler-neutral parameters", schedule_flat)
        self.assertIn("map into the host Agent scheduler UI or API", schedule_flat)
        self.assertIn("not a universal registration JSON", schedule_flat)
        self.assertIn("`0 11 * * *`", schedule)
        self.assertIn("`Asia/Shanghai`", schedule)
        self.assertNotIn("<preliminary_run_id>", schedule)
        manifest_match = re.search(
            r"```jsonl\n([^\n]+)\n```",
            schedule,
        )
        self.assertIsNotNone(manifest_match)
        manifest = json.loads(manifest_match.group(1) if manifest_match else "{}")
        self.assertEqual(
            set(manifest),
            {
                "schema_version",
                "run_id",
                "phase",
                "report_json",
                "report_markdown",
                "warnings",
                "enrichment_candidate_appids",
            },
        )
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["phase"], "preliminary")
        self.assertEqual(manifest["enrichment_candidate_appids"], [123456])
        self.assertEqual(manifest["warnings"], [])
        self.assertTrue(Path(manifest["report_json"]).is_absolute())
        self.assertTrue(Path(manifest["report_markdown"]).is_absolute())
        workflow = " ".join(
            schedule[schedule.find("### Agent payload") :].split()
        )
        workflow_steps = (
            "Run `scan`",
            "capture and parse its exact one-line JSON manifest",
            "read `run_id`",
            "research every AppID in `enrichment_candidate_appids`",
            "write `enrichment.json`",
            "run `enrich`",
            "capture and consume the final one-line manifest",
        )
        for step in workflow_steps:
            self.assertIn(step, workflow)
        for earlier, later in zip(
            workflow_steps,
            workflow_steps[1:],
        ):
            self.assertLess(workflow.find(earlier), workflow.find(later))
        self.assertIn(
            "one total Top N across released and unreleased eligible reported candidates",
            workflow,
        )
        self.assertIn("canonical `candidate_sort_key`", workflow)
        self.assertIn("not N per pool", workflow)
        self.assertIn("already limited by configured `enrichment_top_n`", workflow)
        self.assertIn("manifest field is authoritative", workflow)
        self.assertIn("TZ=Asia/Shanghai", text)
        self.assertIn("0 11 * * *", text)
        self.assertRegex(
            text,
            r"(?s)conventional cron.*?(?:preliminary-only|\u4ec5\u751f\u6210 preliminary).*?(?:\u72ec\u7acb Agent|separate agent)",
        )
        self.assertRegex(text, r"(?:\u4e0d\u4f1a|never).*(?:\u4fee\u6539|edit).*(?:cron|\u5b9a\u65f6)")

    def test_steamdb_is_local_import_only_and_never_scraped(self) -> None:
        skill = _read(SKILL_ROOT / "SKILL.md")
        import_reference = _read(SKILL_ROOT / "references/steamdb-import-format.md")
        combined = f"{skill}\n{import_reference}".casefold()
        self.assertIn("steamdb.info", combined)
        self.assertRegex(combined, r"(?:local csv/json|\u672c\u5730 csv/json|\u672c\u5730 csv \u6216 json)")
        for prohibited in ("request", "browse", "crawl", "scrape", "refresh"):
            self.assertRegex(
                combined,
                rf"(?:never|\u4ece\u4e0d|\u4e0d\u5f97|\u7981\u6b62)[^\n]{{0,80}}{prohibited}",
            )
        self.assertIn("\u4eba\u5de5", combined)

    def test_data_sources_reference_is_complete(self) -> None:
        text = _read(SKILL_ROOT / "references/data-sources.md")
        endpoint_rows = {
            capability: endpoint
            for capability, endpoint in re.findall(
                r"(?m)^\| `([^`]+)` \| `([^`]+)` \|",
                text,
            )
        }
        self.assertEqual(
            endpoint_rows,
            {
                "most_played": MOST_PLAYED_URL,
                "current_players": CURRENT_PLAYERS_URL,
                "featured_categories": FEATURED_CATEGORIES_URL,
                "appdetails": APPDETAILS_URL,
            },
        )
        self.assertEqual(set(endpoint_rows), set(_CAPABILITY_NAMES))
        self.assertEqual(
            {urlsplit(url).hostname for url in endpoint_rows.values()},
            set(ALLOWED_HOSTS),
        )
        flattened = " ".join(text.split())
        config_path = SKILL_ROOT / "references/config.example.json"
        config_mapping = json.loads(_read(config_path))
        config = RadarConfig.from_mapping(config_mapping, project_root=ROOT)
        policy_rows = {
            field: (meaning.strip(), documented.strip())
            for field, meaning, documented in re.findall(
                r"(?m)^\| `([^`]+)` \| ([^|]+?) \| `([^`]+)` \|$",
                text,
            )
        }
        self.assertEqual(
            policy_rows,
            {
                "released_candidate_limit": (
                    "Released candidates capped before per-AppID requests",
                    str(config.released_candidate_limit),
                ),
                "unreleased_candidate_limit": (
                    "Unreleased candidates capped before per-AppID requests",
                    str(config.unreleased_candidate_limit),
                ),
                "request_timeout_seconds": (
                    "Per-attempt request timeout",
                    f"{config.request_timeout_seconds} seconds",
                ),
                "minimum_request_interval_seconds": (
                    "Minimum interval between request starts",
                    f"{config.minimum_request_interval_seconds} seconds",
                ),
                "max_retries": (
                    "Retries after the first attempt",
                    str(config.max_retries),
                ),
                "raw_max_bytes_per_provider": (
                    "Maximum response/raw bytes per provider",
                    f"{config.raw_max_bytes_per_provider} bytes (5 MiB)",
                ),
                "stale_warning_hours": (
                    "Age strictly greater than this produces a stale warning",
                    f"{config.stale_warning_hours} hours",
                ),
                "stale_fallback_limit_hours": (
                    "Maximum inclusive official fallback age",
                    f"{config.stale_fallback_limit_hours} hours",
                ),
            },
        )
        for field in policy_rows:
            self.assertEqual(config_mapping[field], getattr(config, field))
        self.assertIn("HTTPS-only", text)
        self.assertIn(
            "allows only `api.steampowered.com` and `store.steampowered.com`",
            flattened,
        )
        self.assertIn(
            "rejects userinfo and every explicit port, including `:443`",
            flattened,
        )
        self.assertRegex(text.casefold(), r"(?:no authentication|no api key|\u65e0\u9700\u8ba4\u8bc1).*(?:no api key|\u65e0\u9700 api key)")
        self.assertRegex(text.casefold(), r"(?:redirects disabled|no redirects|\u7981\u7528\u91cd\u5b9a\u5411)")
        self.assertIn(
            f"`max_retries={config.max_retries}` means {config.max_retries + 1} total attempts",
            flattened,
        )
        self.assertIn(
            "Retries apply only to timeouts, HTTP 429, and HTTP 5xx",
            flattened,
        )
        self.assertIn(f"`User-Agent: {USER_AGENT}`", flattened)
        self.assertIn(
            "sends no credentials, cookies, authorization, API keys, or other secret headers",
            flattened,
        )
        self.assertIn("36 hours produces a stale warning", flattened)
        self.assertIn("72 hours inclusive", flattened)
        self.assertRegex(text.casefold(), r"capability.*(?:warning|\u8b66\u544a).*(?:fallback|\u964d\u7ea7)")

    def test_steamdb_import_reference_matches_implementation(self) -> None:
        text = _read(SKILL_ROOT / "references/steamdb-import-format.md")
        for view in sorted(_ALLOWED_VIEWS):
            self.assertIn(f"`{view}`", text)
        self.assertRegex(text, r"CSV.*(?:explicit `--view`|\u663e\u5f0f.*`--view`)")
        self.assertIn('{ "schema_version": 1, "view": "...", "rows": [...] }', text)
        self.assertRegex(text, r"JSON.*(?:array|\u6570\u7ec4)")
        for canonical, aliases in _ALIASES.items():
            self.assertIn(f"`{canonical}`", text)
            for alias in aliases:
                self.assertIn(f"`{alias}`", text)
        for token in ("commas", "leading plus", "K", "M", "91.2%", "YYYY-MM-DD", "DD Mon YYYY", "UTC", "em dash"):
            self.assertIn(token, text)
        self.assertIn("/app/{appid}", text)
        self.assertRegex(text.casefold(), r"duplicate.*reject")
        self.assertIn("`source_extra`", text)
        self.assertRegex(text.casefold(), r"invalid rows.*valid rows|\u65e0\u6548\u884c.*\u6709\u6548\u884c")

    def test_scoring_reference_contains_exact_rules(self) -> None:
        text = _read(SKILL_ROOT / "references/scoring-rules.md")
        flattened = " ".join(text.split())
        released_section = text[
            text.find("## Released Steam heat") : text.find("## Unreleased Steam heat")
        ]
        unreleased_section = text[
            text.find("## Unreleased Steam heat") : text.find("## SEO opportunity")
        ]
        confidence_section = " ".join(
            text[
                text.find("## Combination, actions, and confidence") : text.find("## Stable ordering")
            ].split()
        )
        ordering_section = " ".join(
            text[text.find("## Stable ordering") :].split()
        )
        required_rows = (
            "| Player growth | 25 | `(0,0)`, `(5,25)`, `(15,50)`, `(30,75)`, `(60,100)` |",
            "| Current player scale | 10 | `(0,0)`, `(100,20)`, `(1000,50)`, `(10000,80)`, `(100000,100)` |",
            "| Rank improvement | 15 | `(0,0)`, `(5,40)`, `(20,70)`, `(50,100)` |",
            "| Upcoming rank improvement | 20 | `(0,0)`, `(5,40)`, `(20,70)`, `(50,100)` |",
            "| Wishlist/follower 7d gain | 20 | `(0,0)`, `(100,20)`, `(1000,60)`, `(5000,85)`, `(20000,100)` |",
            "| Google competition gap | 20 | validated 0-100 |",
            "| Expandable query count | 10 | `min(query_count / 20 * 100, 100)` |",
            "| YouTube/Reddit cross-signal | 10 | `(0,0)`, `(1,20)`, `(3,50)`, `(10,80)`, `(25,100)` |",
        )
        for row in required_rows:
            self.assertIn(row, text)
        self.assertIn(
            f"| Release recency | {_RELEASED_WEIGHTS['release_recency']} | age 0-7: 100; 8-30: 70; 31-90: 40; older: 0 |",
            released_section,
        )
        self.assertIn(
            f"| Release proximity | {_UNRELEASED_WEIGHTS['release_proximity']} | 0-14 days: 100; 15-30: 80; 31-90: 60; 91-180: 30; later/TBA: 0 |",
            unreleased_section,
        )
        self.assertIn(
            f"| Coming-soon visibility | {_UNRELEASED_WEIGHTS['coming_soon_visibility']} | rank 1: 100; 2-5: 80; 6-20: 50; 21-50: 20; lower/unranked: 0 |",
            unreleased_section,
        )
        for rule in (
            "7d same-source -> 1d same-source -> provider previous_rank - current_rank",
            "7d same-source -> 1d same-source; provider previous_rank is not allowed",
            "at least 2 metrics and at least 25 configured weight",
            "at least 2 metrics and at least 30 configured weight",
            "Google competition gap plus at least one other SEO metric",
            "empty expandable_queries is an observed zero",
            "0.60 * steam_heat_score + 0.40 * seo_opportunity_score",
            "80.00-100.00",
            "65.00-79.99",
            "50.00-64.99",
            "0.00-49.99",
            "confidence A",
            "confidence B",
            "confidence C",
        ):
            self.assertIn(rule, flattened)
        for action in ("immediate_action", "worth_positioning", "watch", "skip"):
            self.assertIn(f"`{action}`", text)
        for definition in (
            "confidence A: official values + valid history + manual SteamDB confirmation + valid SEO/community enrichment.",
            "confidence B: official or manual SteamDB values + valid history + valid SEO/community enrichment.",
            "confidence C: baseline, missing history, or incomplete enrichment. C never receives a final score/action",
        ):
            self.assertIn(definition, confidence_section)
        for criterion in (
            "1. Final score descending when present, otherwise Steam heat descending.",
            "2. Confidence A, B, C.",
            "3. Current players descending for released; wishlist/follower gain descending for unreleased.",
            "4. Case-folded name ascending.",
            "5. AppID ascending.",
        ):
            self.assertIn(criterion, ordering_section)

        enrichment_match = re.search(
            r"## Enrichment file contract.*?```json\n(.*?)\n```",
            text,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(enrichment_match)
        enrichment_example = json.loads(
            enrichment_match.group(1) if enrichment_match else "{}"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "enrichment.json"
            path.write_text(
                json.dumps(enrichment_example),
                encoding="utf-8",
            )
            bundle = load_enrichment(
                path,
                expected_run_id=enrichment_example["run_id"],
            )
        self.assertEqual(bundle.schema_version, 1)
        self.assertEqual(set(bundle.games), {123456})
        enrichment_section = " ".join(
            text[text.find("## Enrichment file contract") : text.find("## Combination, actions, and confidence")].split()
        )
        for contract in (
            "`google_competition_gap_score` is an integer from 0 through 100",
            "YouTube/Reddit counts are null or non-negative integers",
            "Google evidence is required",
            "supplied YouTube or Reddit signals require source-matching evidence",
            "evidence URLs must use HTTPS",
            "file `run_id` must exactly match the preliminary manifest `run_id`",
        ):
            self.assertIn(contract, enrichment_section)

        self.assertEqual(_combine_scores(50.0, 49.9), (50.0, 4_996))
        self.assertEqual(_action_for_combined_score(4_996), "skip")
        self.assertEqual(_combine_scores(65.0, 64.9), (65.0, 6_496))
        self.assertEqual(_action_for_combined_score(6_496), "watch")
        self.assertIn(
            "49.96 -> persisted 50.0 -> `skip`",
            confidence_section,
        )
        self.assertIn(
            "64.96 -> persisted 65.0 -> `watch`",
            confidence_section,
        )
        self.assertIn("pre-round composite interval", confidence_section)
        self.assertIn(
            "Action is selected from exact pre-round composite hundredths before final_score is persisted half-up to one decimal",
            confidence_section,
        )

        observed_at = "2026-08-24T03:00:00Z"

        def observation(
            value: object,
            source_id: str,
            source_kind: str = "steam_official",
        ) -> MetricObservation:
            return MetricObservation(
                value=value,
                source_id=source_id,
                source_kind=source_kind,  # type: ignore[arg-type]
                observed_at=observed_at,
            )

        def analyzed(
            appid: int,
            metrics: dict[str, MetricObservation],
        ) -> AnalyzedCandidate:
            return AnalyzedCandidate(
                record=GameRecord(
                    schema_version=1,
                    appid=appid,
                    name=f"Upcoming {appid}",
                    release_status="unreleased",
                    store_url=f"https://store.steampowered.com/app/{appid}/",
                    metrics=metrics,
                    source_extra={},
                ),
                deltas={},
                newly_observed=True,
                warnings=(),
            )

        missing = score_unreleased(
            analyzed(
                700001,
                {"follower_gain_7d": observation(1_000, "gain")},
            )
        )
        self.assertEqual(
            dict(missing.metric_scores),
            {
                "coming_soon_visibility": 0.0,
                "release_proximity": 0.0,
                "wishlist_or_follower_gain": 60.0,
            },
        )
        self.assertEqual(missing.steam_heat_score, 30.0)
        unavailable = score_unreleased(
            analyzed(
                700002,
                {
                    "follower_gain_7d": observation(1_000, "gain"),
                    "release_date": observation("TBA", "release"),
                    "coming_soon_rank": observation("unranked", "rank"),
                },
            )
        )
        self.assertEqual(missing.metric_scores, unavailable.metric_scores)
        omitted = score_unreleased(
            analyzed(
                700003,
                {
                    "follower_gain_7d": observation(1_000, "gain"),
                    "release_date": observation(
                        "TBA",
                        "seo_release",
                        "seo_enrichment",
                    ),
                    "coming_soon_rank": observation("malformed", "rank"),
                },
            )
        )
        self.assertNotIn("release_proximity", omitted.metric_scores)
        self.assertNotIn("coming_soon_visibility", omitted.metric_scores)
        self.assertIsNone(omitted.steam_heat_score)
        for rule in (
            "Missing or TBA `release_date` contributes `release_proximity = 0` at weight 10",
            "Missing or unranked `coming_soon_rank` contributes `coming_soon_visibility = 0` at weight 10",
            "Both zero-valued signals count toward metric count and available weight",
            "Malformed or non-Steam observations remain omitted",
        ):
            self.assertIn(rule, unreleased_section)

    def test_report_template_lists_the_canonical_schema(self) -> None:
        text = _read(SKILL_ROOT / "references/report-template.md")
        top_section = text[text.find("## Canonical top-level fields") : text.find("## Canonical candidate fields")]
        candidate_section = text[text.find("## Canonical candidate fields") :]
        for field in _REPORT_FIELDS:
            self.assertIn(f"`{field}`", top_section)
        for field in _CANDIDATE_FIELDS:
            self.assertIn(f"`{field}`", candidate_section)
        self.assertIn("canonical JSON", text)
        self.assertRegex(text, r"(?:does not|never|\u4e0d).*(?:re-sort|sort|\u6392\u5e8f).*(?:recompute|\u91cd\u7b97)")

    def test_readme_catalog_count_and_transport_policy(self) -> None:
        readme = _read(ROOT / "README.md")
        self.assertNotIn("kennyzir", readme.casefold())
        for command in (
            "git clone https://github.com/destinationluo/7deer_skills.git .agent/skills",
            "git clone https://github.com/destinationluo/7deer_skills.git ~/.openclaw/skills/7deer",
            "git submodule add https://github.com/destinationluo/7deer_skills.git .agent/skills",
            "npx degit destinationluo/7deer_skills .agent/skills",
            "npx degit destinationluo/7deer_skills/google-trends-to-pages .agent/skills/google-trends-to-pages",
        ):
            self.assertIn(command, readme)
        self.assertIn(
            "https://github.com/destinationluo/7deer_skills",
            readme,
        )
        skill_names = sorted(path.parent.name for path in ROOT.glob("*/SKILL.md"))
        self.assertEqual(len(skill_names), 32)
        self.assertIn("32 \u4e2a\u53ef\u590d\u7528", readme)
        self.assertIn("skills-32-blue.svg", readme)
        self.assertIn("**\u603b\u6280\u80fd\u6570**: 32 \u4e2a", readme)
        for name in skill_names:
            self.assertIn(f"**{name}**", readme)
        steam_line = next(line for line in readme.splitlines() if "**steam-game-radar**" in line)
        html5_line = next(line for line in readme.splitlines() if "**html5-game-radar**" in line)
        self.assertRegex(steam_line, r"Steam.*(?:\u5b98\u65b9|\u8d8b\u52bf|SteamDB)")
        self.assertRegex(html5_line, r"HTML5|\u6d4f\u89c8\u5668\u53ef\u73a9")
        self.assertNotEqual(steam_line, html5_line)

        allowed_hosts = {"api.steampowered.com", "store.steampowered.com"}
        transport_files = tuple((SKILL_ROOT / "steam_game_radar").glob("*.py")) + tuple((SKILL_ROOT / "scripts").glob("*.py"))
        for path in transport_files:
            source = _read(path)
            self.assertNotIn("steamdb.info", source.casefold(), str(path))
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("https://"):
                    self.assertIn(urlsplit(node.value).hostname, allowed_hosts, str(path))


if __name__ == "__main__":
    unittest.main()
