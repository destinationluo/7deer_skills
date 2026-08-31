from __future__ import annotations

from pathlib import Path
import re
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
UNIFIED_SKILL = REPOSITORY_ROOT / "unified-game-radar/SKILL.md"
HTML5_SKILL = REPOSITORY_ROOT / "html5-game-radar/SKILL.md"
STEAM_SKILL = REPOSITORY_ROOT / "steam-game-radar/SKILL.md"
README = REPOSITORY_ROOT / "README.md"


def body(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class UnifiedSkillPolicyTests(unittest.TestCase):
    def test_frontmatter_is_discoverable_for_unified_monitoring_requests(self) -> None:
        text = body(UNIFIED_SKILL)
        match = re.match(r"\A---\n(?P<frontmatter>.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(match)
        frontmatter = match.group("frontmatter")
        self.assertIn("name: unified-game-radar", frontmatter)
        self.assertIn("description: Use when", frontmatter)
        for trigger in (
            "统一游戏雷达",
            "热词游戏",
            "itch.io",
            "Steam",
            "Roblox",
        ):
            self.assertIn(trigger, frontmatter)

    def test_one_canonical_schedule_separates_collection_and_publication(self) -> None:
        text = body(UNIFIED_SKILL)
        self.assertIn("10:00", text)
        self.assertIn("collection-only", text)
        self.assertIn("16:00", text)
        self.assertIn("--publish-daily", text)
        ten_command = re.search(
            r"### 10:00.*?```(?:bash)?\n(?P<command>.*?)```",
            text,
            re.DOTALL,
        )
        sixteen_command = re.search(
            r"### 16:00.*?```(?:bash)?\n(?P<command>.*?)```",
            text,
            re.DOTALL,
        )
        self.assertIsNotNone(ten_command)
        self.assertIsNotNone(sixteen_command)
        self.assertNotIn("--publish-daily", ten_command.group("command"))
        self.assertIn("--publish-daily", sixteen_command.group("command"))

    def test_workflow_uses_manifest_run_id_and_exact_cli_routes(self) -> None:
        text = body(UNIFIED_SKILL)
        for command in ("scan", "ingest", "enrich", "report"):
            self.assertIn(
                f"python3 unified-game-radar/scripts/game_radar.py {command}",
                text,
            )
        self.assertIn("one-line JSON manifest", text)
        self.assertIn("outstanding_tasks", text)
        self.assertIn("exact `run_id`", text)
        self.assertIn("one evidence object per enrichment candidate", text)
        self.assertNotIn("import-steamdb", text)

    def test_zero_demand_and_single_spike_are_hard_nonpositive_gates(self) -> None:
        text = body(UNIFIED_SKILL).casefold()
        self.assertIn("zero demand", text)
        self.assertIn("single spike", text)
        self.assertIn("airlinia", text)
        self.assertIn("meltspell", text)
        self.assertIn("geoslice", text)
        self.assertRegex(text, r"zero demand[^\n]*(?:skip|跳过)")
        self.assertRegex(text, r"single spike[^\n]*(?:watch|观察)")
        self.assertIn("cannot", text)
        self.assertIn("worth doing", text)

    def test_external_platform_writes_require_separate_authorization(self) -> None:
        text = body(UNIFIED_SKILL)
        self.assertIn("web-game-data", text)
        self.assertIn("Feishu", text)
        self.assertIn("separate explicit authorization", text)
        self.assertNotIn("立即用 message", text)

    def test_old_skills_are_manual_compatibility_routes_without_schedules(self) -> None:
        html5 = body(HTML5_SKILL)
        steam = body(STEAM_SKILL)
        for text, platform in ((html5, "itch"), (steam, "steam")):
            self.assertIn("deprecated", text.casefold())
            self.assertIn("manual-only", text.casefold())
            self.assertIn("unified-game-radar", text)
            self.assertIn(f"--platform {platform}", text)
            self.assertNotRegex(text, r"\b[0-5]?\d\s+[0-2]?\d(?:,[0-2]?\d)*\s+\*\s+\*\s+\*")
            self.assertNotIn("--publish-daily", text)
        self.assertNotIn("推送 Feishu", html5)

    def test_readme_catalogs_unified_radar_as_the_canonical_game_monitor(self) -> None:
        text = body(README)
        self.assertIn("unified-game-radar", text)
        self.assertIn("itch.io、Steam、Roblox", text)
        self.assertIn("统一候选榜", text)


if __name__ == "__main__":
    unittest.main()
