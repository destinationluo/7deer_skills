"""Contract tests for the Steam game radar Skill and reference documents."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys
import unittest
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "steam-game-radar"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from steam_game_radar.report import _CANDIDATE_FIELDS, _REPORT_FIELDS
from steam_game_radar.steamdb_import import _ALIASES, _ALLOWED_VIEWS


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
        self.assertLess(len(body.split()), 500)
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

    def test_exact_cli_commands_and_outputs(self) -> None:
        text = _read(SKILL_ROOT / "SKILL.md")
        for command in (SCAN_COMMAND, IMPORT_COMMAND, ENRICH_COMMAND):
            self.assertIn(command, text)
            self.assertEqual(text.count(command), 1)
        self.assertIn("reports/steam-game-radar/{run_id}.preliminary.json", text)
        self.assertIn("reports/steam-game-radar/{run_id}.final.json", text)
        self.assertIn("references/report-template.md", text)
        for code in range(7):
            if code == 1:
                continue
            self.assertRegex(text, rf"\b{code}\b")

    def test_agent_schedule_and_preliminary_only_cron(self) -> None:
        text = _read(SKILL_ROOT / "SKILL.md")
        self.assertIn('"expression": "0 11 * * *"', text)
        self.assertIn('"timezone": "Asia/Shanghai"', text)
        self.assertIn('"payload":', text)
        schedule = text[text.find('"expression": "0 11 * * *"') :]
        self.assertLess(schedule.find("scan"), schedule.find("Top 10"))
        self.assertLess(schedule.find("Top 10"), schedule.find("enrich"))
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
        endpoints = (
            "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/",
            "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={appid}",
            "https://store.steampowered.com/api/featuredcategories?cc={country}&l={language}",
            "https://store.steampowered.com/api/appdetails?appids={appid}&cc={country}&l={language}",
        )
        for endpoint in endpoints:
            self.assertIn(endpoint, text)
        self.assertIn("api.steampowered.com", text)
        self.assertIn("store.steampowered.com", text)
        self.assertRegex(text.casefold(), r"(?:no authentication|no api key|\u65e0\u9700\u8ba4\u8bc1).*(?:no api key|\u65e0\u9700 api key)")
        self.assertRegex(text.casefold(), r"(?:redirects disabled|no redirects|\u7981\u7528\u91cd\u5b9a\u5411)")
        for token in ("1.0", "15", "3", "50", "5 MiB"):
            self.assertIn(token, text)
        for capability in (
            "most_played",
            "featured_categories",
            "appdetails",
            "current_players",
        ):
            self.assertIn(f"`{capability}`", text)
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
        for ordering in range(1, 6):
            self.assertRegex(text, rf"(?m)^{ordering}\. ")

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
